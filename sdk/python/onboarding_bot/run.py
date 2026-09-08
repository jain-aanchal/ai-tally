# SPDX-License-Identifier: Apache-2.0
"""The headless run: clone, propose, branch, PR, forget (CTO-261 sections 3, 4.3, 9).

This is the hosted PR bot's entrypoint. It runs the section 3 loop server-side with no
interactive session: detect the stack, retrieve recipes from the shared catalog, ask the
account question (in the PR body, where the developer answers it), and propose a diff.

Retention is a ``finally``, not a convention. The clone lives in a temporary directory the
run deletes on every exit path including a raised refusal, and :class:`RunResult` carries
paths, counts and generated code only, never the developer's source (section 9).

A GitHub App remains the future hardening path (section 11 Q5): it would replace the
operator-supplied token with a short-lived installation token and per-repo grants managed
in GitHub rather than in an environment variable. Everything else here (the loop, the
guards, the reviewed-PR shape) is unchanged by that swap.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_TOKEN_ENV, BotConfig
from .git_ops import GitRunner
from .github_pr import Transport, open_pull_request
from .guards import assert_push_target_allowed
from .patch import apply_proposal
from .propose import Proposal, build_proposal

PR_TITLE = "Wire ai-tally: init, account attribution and per-layer cost records"


@dataclass
class RunResult:
    """What a run reports. Contains no repo source (section 9)."""

    repo: str
    base_branch: str
    branch: str | None = None
    pr_url: str | None = None
    files_changed: list[str] = field(default_factory=list)
    layers_wired: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    holes_to_fill: list[str] = field(default_factory=list)
    detection: dict[str, Any] = field(default_factory=dict)
    clone_removed: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_bot(
    config: BotConfig,
    *,
    transport: Transport | None = None,
    dry_run: bool = False,
    clone_override: Path | None = None,
) -> RunResult:
    """Run the loop end to end.

    ``clone_override`` points the run at an already-present working tree instead of cloning,
    which is how the tests exercise the whole loop without a network. ``dry_run`` proposes
    and stops: no branch, no push, no PR.
    """
    workdir = Path(tempfile.mkdtemp(prefix="tally-onboarding-"))
    result = RunResult(repo=config.repo, base_branch="")
    try:
        runner = GitRunner(config, workdir)
        if clone_override is not None:
            clone_dir = clone_override
        else:
            clone_dir = runner.clone(workdir / "repo")

        base_branch = runner.default_branch(clone_dir)
        result.base_branch = base_branch
        branch = config.branch_name()
        # Checked before any work: a run that could only end in a refused push should refuse
        # up front rather than after cloning and editing.
        assert_push_target_allowed(branch, base_branch)

        proposal = build_proposal(
            clone_dir,
            account_source=config.account_source,
            feature_tag=config.feature_tag,
            max_call_sites=config.max_call_sites,
        )
        result.detection = proposal.detection
        result.gaps = list(proposal.gaps)
        result.questions = list(proposal.questions)
        result.holes_to_fill = proposal.holes

        if not proposal.edits:
            # Nothing the catalog covers. An empty PR would claim work that did not happen.
            result.note = (
                "no recipe-backed edit applied to this repo; opened no PR. The gaps below say "
                "why, rather than a diff that guesses."
            )
            return result

        result.layers_wired = proposal.layers_wired
        if dry_run:
            result.note = "dry run: proposed the diff, created no branch and opened no PR."
            return result

        runner.create_branch(clone_dir, branch, base_branch)
        result.branch = branch
        result.files_changed = apply_proposal(clone_dir, proposal)
        runner.commit_all(clone_dir, commit_message(proposal))
        runner.push_branch(clone_dir, branch, base_branch)

        pr = open_pull_request(
            config,
            head=branch,
            base=base_branch,
            title=PR_TITLE,
            body=pr_body(proposal, config),
            transport=transport,
        )
        result.pr_url = pr.get("html_url")
        return result
    finally:
        # The clone is the only copy of the developer's source this component ever holds, and
        # it goes on every exit path, including a refusal raised mid-run (section 9).
        shutil.rmtree(workdir, ignore_errors=True)
        result.clone_removed = not workdir.exists()


def commit_message(proposal: Proposal) -> str:
    layers = ", ".join(proposal.layers_wired) or "none"
    return (
        f"chore: wire ai-tally instrumentation ({layers})\n\n"
        "Generated from the ai-tally recipe catalog by the onboarding bot (CTO-261).\n"
        "Every block is marked for review. Nothing here was merged automatically."
    )


def pr_body(proposal: Proposal, config: BotConfig) -> str:
    """The PR description: what was wired, what is a gap, and the question still open.

    Deliberately made of generated code and detection results only. The developer's own
    source does not appear here, so the PR diff stays the single place their code travels
    (section 9).
    """
    detection = proposal.detection
    lines = [
        "## ai-tally onboarding (CTO-261)",
        "",
        "This PR was opened by the ai-tally onboarding bot from the maintained recipe "
        "catalog. Every block is generated from a recipe pinned to the real SDK surface, "
        "never hand-written, and every block is marked in the diff. Review and merge it "
        "yourself: the bot does not merge, and it never pushes to a default branch.",
        "",
        "### Detected stack",
        "",
        f"- LLM providers: {_join(detection.get('llm_providers'))}",
        f"- Web frameworks: {_join(detection.get('web_frameworks'))}",
        f"- Vector DBs: {_join(detection.get('vector_dbs'))}",
        f"- Agent frameworks: {_join(detection.get('agent_frameworks'))}",
        "",
        "### What this PR wires",
        "",
    ]
    if proposal.edits:
        lines.append("| Layer | File | Recipe |")
        lines.append("| --- | --- | --- |")
        for edit in proposal.edits:
            lines.append(f"| {edit.kind} | `{edit.path}:{edit.line_no}` | `{edit.recipe_id}` |")
    else:
        lines.append("Nothing: no recipe matched this repo.")
    lines.append("")

    if proposal.holes:
        lines += [
            "### Holes to fill before merging",
            "",
            "The bot leaves a visible `<FILL:...>` marker wherever it could not derive a "
            "value from the call site. It does not invent one. A block with a hole is "
            "inserted commented out, so the branch still runs: fill the value and "
            "uncomment it.",
            "",
        ]
        lines += [f"- `{hole}`" for hole in proposal.holes]
        lines.append("")

    for question in proposal.questions:
        lines += [
            "### Question the bot will not answer for you",
            "",
            f"**{question['question']}**",
            "",
            question["why"],
            "",
        ]
        candidates = question.get("candidates") or []
        if candidates:
            lines.append("Candidates found in this repo, unconfirmed:")
            lines.append("")
            lines += [f"- {c['kind']}: `{c['token']}`" for c in candidates]
        else:
            lines.append("No candidate resolver surfaced, so there is nothing to confirm yet.")
        lines += ["", question["how_to_answer"], ""]

    if proposal.gaps:
        lines += [
            "### Gaps (reported, not guessed)",
            "",
        ]
        for entry in proposal.gaps:
            lines.append(f"- {entry.get('reason', 'unknown gap')}")
            if entry.get("code"):
                lines += ["", "```python", entry["code"].rstrip(), "```", ""]
        lines.append("")

    lines += [
        "### How this run was authorised",
        "",
        f"A scoped, revocable token supplied by reference in `${config.token_env}`. Revoke it "
        "in your GitHub settings and this bot loses all access immediately. The working clone "
        "was deleted when the run finished; no repo content is retained beyond this PR.",
    ]
    return "\n".join(lines)


def _join(values: Any) -> str:
    items = list(values or [])
    # An empty detection is stated as "none detected", never rendered as a blank that could
    # read as a value (CLAUDE.md: honest under uncertainty).
    return ", ".join(f"`{v}`" for v in items) if items else "none detected"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    parser = argparse.ArgumentParser(
        prog="tally-onboarding-bot",
        description="Open a reviewed ai-tally instrumentation PR against a repo (CTO-261 P2).",
    )
    parser.add_argument("--repo", required=True, help="owner/name of the target repository")
    parser.add_argument(
        "--account-source",
        default=None,
        help=(
            "the confirmed account resolver expression (section 6). Omit it and the bot asks "
            "the question in the PR instead of guessing."
        ),
    )
    parser.add_argument("--feature-tag", default=None)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--branch-suffix", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = BotConfig(
        repo=args.repo,
        token_env=args.token_env,
        account_source=args.account_source,
        feature_tag=args.feature_tag,
        branch_suffix=args.branch_suffix,
    )
    result = run_bot(config, dry_run=args.dry_run)
    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
