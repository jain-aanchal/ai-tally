# SPDX-License-Identifier: Apache-2.0
"""Retrieve, ask and propose (CTO-261 section 3 steps 2 to 4, section 6).

Every line of code this module puts in a diff comes from the shared recipe catalog through
:mod:`onboarding_mcp.generate`. Nothing is hand-written here, which is the whole point of
section 2 decision 2: an unrecognised stack yields a reported gap, never a hallucinated
``record_*`` call. The bot is the delivery form; the catalog is the source of truth, and
it is the same catalog the dashboard form and the MCP server read (section 4.1).

The account-identity question (section 6) is asked, never inferred: with no answer the
middleware is not generated, the account layer stays unattributed, and the gap plus the
question ride in the PR body so the reviewer answers it there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onboarding_mcp.catalog import RecipeCatalog, get_catalog

# The one honesty shape for "I do not know". Imported rather than re-declared so the bot
# and the MCP server report an unknown identically (section 6, CLAUDE.md).
from onboarding_mcp.generate import _gap as gap
from onboarding_mcp.generate import (
    generate_middleware,
    generate_startup,
    instrument_call_site,
)

from .repo_scan import (
    CallSite,
    detect,
    find_app_object_site,
    find_call_sites,
    find_startup_site,
)

ACCOUNT_QUESTION = "How does your app know which customer a request belongs to?"

# Candidate resolvers a scan can surface. These are shown to the developer as options to
# confirm, never adopted: picking one of these on the bot's own authority is exactly the
# guess section 6 forbids.
_CANDIDATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("request header", re.compile(r"headers(?:\.get\(|\[)\s*[\"']([A-Za-z0-9-]+)[\"']")),
    ("auth dependency", re.compile(r"Depends\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")),
    (
        "request attribute",
        re.compile(r"\brequest\.(?:state\.)?([a-z_]*(?:tenant|account|org)[a-z_]*)"),
    ),
    ("user attribute", re.compile(r"\b(?:user|current_user)\.([a-z_]*(?:id|tenant|org)[a-z_]*)")),
)


@dataclass(frozen=True)
class ProposedEdit:
    """One edit in the proposed diff.

    Carries the generated code and where it goes. It deliberately does not carry the
    source line it was derived from: the working clone is the only place the developer's
    source exists during a run (section 9).
    """

    path: str
    line_no: int
    kind: str
    recipe_id: str
    code: str
    imports_to_add: list[str]
    placement: str
    indent: str = ""
    holes_to_fill: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    """What the run proposes: edits, the gaps it refuses to fill, and the question to answer."""

    detection: dict[str, Any]
    edits: list[ProposedEdit] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def layers_wired(self) -> list[str]:
        return sorted({e.kind for e in self.edits})

    @property
    def holes(self) -> list[str]:
        """Every ``<FILL:...>`` the reviewer must complete, as ``path:line hole``."""
        return [
            f"{e.path}:{e.line_no} {hole}" for e in self.edits for hole in e.holes_to_fill
        ]


def account_candidates(repo_dir: Path) -> list[dict[str, str]]:
    """Surface candidate account resolvers for the developer to confirm (section 6).

    Returns options with the kind and the matched token only, never the surrounding source
    line, so presenting the question does not ship the developer's code out of the clone.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    from .repo_scan import python_files  # local import keeps the scan surface in one module

    for path in python_files(repo_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        for kind, pattern in _CANDIDATE_PATTERNS:
            for match in pattern.finditer(text):
                key = (kind, match.group(1))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "kind": kind,
                        "token": match.group(1),
                        "status": "candidate, unconfirmed",
                    }
                )
    return out


def account_question(repo_dir: Path) -> dict[str, Any]:
    """The one question the bot must never answer for the developer (section 6)."""
    return {
        "id": "account_identity",
        "question": ACCOUNT_QUESTION,
        "why": (
            "Only your app knows which customer a request serves. The bot presents what it "
            "found and never picks one: an unanswered question leaves the account layer "
            "UNATTRIBUTED rather than attributed to a guess."
        ),
        "candidates": account_candidates(repo_dir),
        "how_to_answer": (
            "Re-run with the resolver expression, for example "
            "request.headers.get(\"X-Customer-Id\"), or answer in a PR comment."
        ),
    }


def build_proposal(
    repo_dir: Path,
    *,
    account_source: str | None,
    feature_tag: str | None = None,
    catalog: RecipeCatalog | None = None,
    max_call_sites: int = 25,
) -> Proposal:
    """Run detect -> retrieve -> ask -> propose over a working clone (section 3)."""
    cat = catalog or get_catalog()
    detection = detect(repo_dir, catalog=cat)
    proposal = Proposal(detection=detection)

    # Detection's own gaps (a vector DB or a web framework with no recipe) pass through
    # unchanged: the bot reports them, it does not fill them.
    for reason in detection.get("gaps", []):
        proposal.gaps.append(gap(reason, source="detect_stack"))

    _propose_startup(proposal, repo_dir, feature_tag, cat)
    _propose_middleware(proposal, repo_dir, account_source, feature_tag, cat)
    _propose_call_sites(proposal, repo_dir, cat, max_call_sites)
    return proposal


def _propose_startup(
    proposal: Proposal, repo_dir: Path, feature_tag: str | None, cat: RecipeCatalog
) -> None:
    """tally.init() at startup. Without it the other edits run in an unconnected process."""
    generated = generate_startup(feature_tag, catalog=cat)
    if generated.get("gap"):
        proposal.gaps.append(generated)
        return
    site = find_startup_site(repo_dir)
    if site is None:
        proposal.gaps.append(
            gap(
                "no recognisable startup site (no app factory, app object or __main__ guard); "
                "tally.init() was not placed rather than dropped into an arbitrary file",
                recipe_id=generated["recipe_id"],
            )
        )
        return
    proposal.edits.append(
        ProposedEdit(
            path=site.path,
            line_no=site.line_no,
            kind="startup",
            recipe_id=generated["recipe_id"],
            code=generated["code"],
            imports_to_add=list(generated["imports_to_add"]),
            placement=generated["placement"],
            indent=site.indent,
        )
    )


def _propose_middleware(
    proposal: Proposal,
    repo_dir: Path,
    account_source: str | None,
    feature_tag: str | None,
    cat: RecipeCatalog,
) -> None:
    """Account / feature middleware, bound to the answer or reported as a gap (section 6)."""
    frameworks = proposal.detection.get("web_frameworks") or []
    if not frameworks:
        return
    if not (account_source or "").strip():
        # generate_middleware owns the unanswered-question gap shape; call it with the empty
        # answer so the bot reports the identical reason the MCP server would.
        proposal.gaps.append(generate_middleware(frameworks[0], "", catalog=cat))
        proposal.questions.append(account_question(repo_dir))
        return

    for framework in frameworks:
        generated = generate_middleware(framework, account_source, feature_tag, catalog=cat)
        if generated.get("gap"):
            proposal.gaps.append(generated)
            continue
        site = find_app_object_site(repo_dir, framework)
        if site is None:
            # Django has no app object, and a FastAPI app built inside a factory is not a
            # module-scope anchor either. Report the generated middleware for the reviewer
            # to place rather than inserting it where it would not run.
            proposal.gaps.append(
                gap(
                    f"middleware for {framework} was generated but there is no module-level "
                    f"app object to attach it to; place it by hand (the code is included)",
                    recipe_id=generated["recipe_id"],
                    code=generated["code"],
                    imports_to_add=list(generated["imports_to_add"]),
                )
            )
            continue
        proposal.edits.append(
            ProposedEdit(
                path=site.path,
                line_no=site.line_no,
                kind="account",
                recipe_id=generated["recipe_id"],
                code=generated["code"],
                imports_to_add=list(generated["imports_to_add"]),
                placement=generated["placement"],
                indent="",
            )
        )


def _propose_call_sites(
    proposal: Proposal, repo_dir: Path, cat: RecipeCatalog, max_call_sites: int
) -> None:
    """The layer-specific record_* edits, each adapted to its real call site."""
    matched = list(proposal.detection.get("matched_recipes", []))
    sites: list[CallSite] = find_call_sites(repo_dir, matched, catalog=cat, limit=max_call_sites)
    for site in sites:
        recipe = cat.get(site.recipe_id)
        if site.source_line.startswith(("return ", "yield ")):
            # An after_call record cannot follow a return: it would be dead code that
            # silently never fires. Report it so the developer restructures the line, rather
            # than shipping an edit that looks wired and meters nothing.
            proposal.gaps.append(
                gap(
                    f"{site.path}:{site.line_no} returns the call's result directly, so the "
                    f"record_* edit cannot be placed after it; assign the result to a name "
                    f"first and re-run",
                    recipe_id=site.recipe_id,
                )
            )
            continue
        generated = instrument_call_site(site.source_line, site.recipe_id, catalog=cat)
        if generated.get("gap"):
            proposal.gaps.append(generated)
            continue
        proposal.edits.append(
            ProposedEdit(
                path=site.path,
                line_no=site.line_no,
                kind=recipe.kind if recipe else "unknown",
                recipe_id=generated["recipe_id"],
                code=generated["code"],
                imports_to_add=list(generated["imports_to_add"]),
                placement=generated["placement"],
                indent=site.indent,
                holes_to_fill=list(generated["holes_to_fill"]),
            )
        )
