# SPDX-License-Identifier: Apache-2.0
"""The headless run: clone, propose, branch, PR, forget (CTO-261 sections 3, 4.3, 9).

This is the hosted PR bot's entrypoint. It runs the section 3 loop server-side with no
interactive session: detect the stack, retrieve recipes from the shared catalog, ask the
account question (in the PR body, where the developer answers it), and propose a diff.

Retention is a ``finally`` plus a signal handler. The clone lives in a temporary directory
the run deletes on every exit path including a raised refusal, and SIGTERM and SIGINT are
handled so a hosted runner that stops the job does not leave the clone behind: a ``finally``
alone does not run when the process is killed. A ``SIGKILL`` still cannot be caught, so the
guarantee is "every exit path this process controls", not "every possible end of the
process". :class:`RunResult` carries paths, counts and generated code only, never the
developer's source (section 9).

A GitHub App remains the future hardening path (section 11 Q5): it would replace the
operator-supplied token with a short-lived installation token and per-repo grants managed
in GitHub rather than in an environment variable. Everything else here (the loop, the
guards, the reviewed-PR shape) is unchanged by that swap.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_TOKEN_ENV, BotConfig
from .git_ops import GitRunner
from .github_pr import Transport, open_pull_request
from .guards import assert_push_target_allowed
from .patch import PatchResult, apply_proposal
from .propose import Proposal, build_proposal

PR_TITLE = "Wire ai-tally: init, account attribution and per-layer cost records"

# Working directories a running bot owns. The signal handler deletes these and nothing else,
# so a stopped job leaves no clone behind (section 9) and touches no path it did not create.
_LIVE_WORKDIRS: set[Path] = set()
_LIVE_LOCK = threading.Lock()


@dataclass
class RunResult:
    """What a run reports. Contains no repo source (section 9)."""

    repo: str
    base_branch: str
    branch: str | None = None
    pr_url: str | None = None
    files_changed: list[str] = field(default_factory=list)
    layers_wired: list[str] = field(default_factory=list)
    """Layers that meter after this diff. A commented-out block is not one of them."""

    layers_inserted_inactive: list[str] = field(default_factory=list)
    """Layers whose block was inserted commented out, pending a hole the reviewer fills."""

    gaps: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    holes_to_fill: list[str] = field(default_factory=list)
    detection: dict[str, Any] = field(default_factory=dict)
    clone_removed: bool = False
    cleanup_error: str | None = None
    """Why the clone could not be removed, when it could not. Never silently swallowed."""

    orphan_branch: str | None = None
    """A branch pushed to the customer's remote with no PR on it, so it can be found."""

    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunFailed(RuntimeError):
    """A run that failed after it had already changed something on the remote.

    Carries the :class:`RunResult` so the caller still learns the branch name. Raising a
    bare error would discard it and leave a branch on the customer's remote that nothing
    recorded (section 9).
    """

    def __init__(self, message: str, result: RunResult):
        super().__init__(message)
        self.result = result


def _remove_workdir(workdir: Path) -> str | None:
    """Delete a run's working directory. Returns why it could not, when it could not."""
    reason: str | None = None
    try:
        shutil.rmtree(workdir)
    except OSError as exc:
        reason = str(exc)
    with _LIVE_LOCK:
        _LIVE_WORKDIRS.discard(workdir)
    if workdir.exists():
        # Reported, not swallowed: a clone that survived the run is the one retention
        # promise this component makes, so a failure to delete has to be visible.
        return reason or f"{workdir} still exists after removal"
    return None


def cleanup_live_workdirs() -> list[Path]:
    """Delete every working directory a run currently owns. Returns what it removed."""
    with _LIVE_LOCK:
        live = list(_LIVE_WORKDIRS)
    for workdir in live:
        shutil.rmtree(workdir, ignore_errors=True)
        with _LIVE_LOCK:
            _LIVE_WORKDIRS.discard(workdir)
    return live


def _cleanup_on_signal(signum: int, _frame: Any) -> None:
    """Delete every live clone, then let the default disposition end the process.

    A ``finally`` does not run when a hosted runner sends SIGTERM, which is exactly when a
    full clone of a customer repo would be left in /tmp (section 9).
    """
    cleanup_live_workdirs()
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


@contextlib.contextmanager
def _workdir() -> Iterator[Path]:
    """A run's temporary directory, registered for signal cleanup while it exists."""
    workdir = Path(tempfile.mkdtemp(prefix="tally-onboarding-"))
    with _LIVE_LOCK:
        _LIVE_WORKDIRS.add(workdir)
    previous: list[tuple[int, Any]] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous.append((signum, signal.signal(signum, _cleanup_on_signal)))
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass
    try:
        yield workdir
    finally:
        for signum, handler in previous:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)


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
    result = RunResult(repo=config.repo, base_branch="")
    with _workdir() as workdir:
        try:
            runner = GitRunner(config, workdir)
            if clone_override is not None:
                clone_dir = clone_override
            else:
                clone_dir = runner.clone(workdir / "repo")

            base_branch = runner.default_branch(clone_dir)
            result.base_branch = base_branch
            branch = config.branch_name()
            # Checked before any work: a run that could only end in a refused push should
            # refuse up front rather than after cloning and editing.
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
                # Nothing the catalog covers. An empty PR would claim work that did not
                # happen.
                result.note = (
                    "no recipe-backed edit applied to this repo; opened no PR. The gaps below "
                    "say why, rather than a diff that guesses."
                )
                return result

            if dry_run:
                result.layers_wired = proposal.layers_wired
                result.layers_inserted_inactive = proposal.layers_inserted_inactive
                result.note = "dry run: proposed the diff, created no branch and opened no PR."
                return result

            if runner.remote_branch_exists(clone_dir, branch):
                # An earlier run got this far. Saying so beats git's bare non-fast-forward
                # error at push time, which reads like a bug rather than a repeat run.
                result.note = (
                    f"branch {branch!r} already exists on the remote, so this run stopped "
                    f"before creating it. Review or delete that branch, or re-run with a "
                    f"different --branch-suffix."
                )
                return result

            runner.create_branch(clone_dir, branch, base_branch)
            patched = apply_proposal(clone_dir, proposal)
            result.gaps.extend(patched.gaps)
            if not patched.applied:
                # Every edit was refused (a file that would not compile, an unplaceable
                # block). Nothing is committed and nothing is pushed: the branch stays local
                # and dies with the clone, and the gaps say why.
                result.note = (
                    "no edit could be applied safely, so nothing was committed and no PR was "
                    "opened. The gaps say which file the bot refused to rewrite."
                )
                return result

            result.branch = branch
            result.files_changed = list(patched.files_changed)
            result.layers_wired = _layers(patched, active=True)
            result.layers_inserted_inactive = _layers(patched, active=False)
            result.holes_to_fill = [
                f"{e.path}:{e.line_no} {hole}" for e in patched.applied for hole in e.holes_to_fill
            ]

            runner.commit_all(clone_dir, commit_message(result))
            runner.push_branch(clone_dir, branch, base_branch)

            try:
                pr = open_pull_request(
                    config,
                    head=branch,
                    base=base_branch,
                    title=PR_TITLE,
                    body=pr_body(proposal, config, result, patched),
                    transport=transport,
                )
            except Exception as exc:
                # The branch is already on the customer's remote. Naming it in the result
                # (and in the raised error) is what makes it recoverable rather than an
                # orphan nobody knows about.
                result.orphan_branch = branch
                result.note = (
                    f"the branch was pushed but the pull request could not be opened, so "
                    f"{branch!r} is on the remote with no PR on it. Open the PR by hand or "
                    f"delete the branch."
                )
                raise RunFailed(f"{result.note} Cause: {exc}", result) from exc
            result.pr_url = pr.get("html_url")
            return result
        finally:
            # The clone is the only copy of the developer's source this component ever holds,
            # and it goes on every exit path, including a refusal raised mid-run (section 9).
            result.cleanup_error = _remove_workdir(workdir)
            result.clone_removed = result.cleanup_error is None


def _layers(patched: PatchResult, *, active: bool) -> list[str]:
    """Layers among the edits that actually landed, split by whether they meter.

    Read off the applied edits rather than the proposal: a block that was refused, or one
    inserted commented out, must not appear as a wired layer anywhere (CLAUDE.md).
    """
    return sorted({e.kind for e in patched.applied if bool(e.holes_to_fill) is not active})


def commit_message(result: RunResult) -> str:
    """The commit subject names the layers that meter, and only those.

    An inserted-but-commented-out block is listed separately as inactive: naming it as
    wired would be the commit claiming work the diff did not do (CLAUDE.md).
    """
    layers = ", ".join(result.layers_wired) or "none active"
    body = [
        f"chore: wire ai-tally instrumentation ({layers})",
        "",
        "Generated from the ai-tally recipe catalog by the onboarding bot (CTO-261).",
        "Every block is marked for review. Nothing here was merged automatically.",
    ]
    if result.layers_inserted_inactive:
        body += [
            "",
            "Inserted but INACTIVE (commented out until a <FILL:...> value is supplied, so "
            "these layers meter nothing yet): " + ", ".join(result.layers_inserted_inactive),
        ]
    return "\n".join(body)


def pr_body(
    proposal: Proposal,
    config: BotConfig,
    result: RunResult | None = None,
    patched: PatchResult | None = None,
) -> str:
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
    # The edits that actually landed, when the patch step has run. A table built from the
    # proposal would list blocks the patcher refused, which is work the diff does not do.
    edits = list(patched.applied) if patched is not None else list(proposal.edits)
    active = [e for e in edits if not e.holes_to_fill]
    inactive = [e for e in edits if e.holes_to_fill]
    if active:
        lines.append("Active: these layers meter as soon as this PR merges.")
        lines.append("")
        lines.append("| Layer | File | Recipe |")
        lines.append("| --- | --- | --- |")
        for edit in active:
            lines.append(f"| {edit.kind} | `{edit.path}:{edit.line_no}` | `{edit.recipe_id}` |")
        lines.append("")
    else:
        lines.append("Nothing is active: no block in this PR meters anything as it stands.")
        lines.append("")
    if inactive:
        lines += [
            "Inserted but INACTIVE: these blocks are commented out until you fill their "
            "`<FILL:...>` values, so they meter nothing yet and this PR does not count them "
            "as wired.",
            "",
            "| Layer | File | Recipe |",
            "| --- | --- | --- |",
        ]
        for edit in inactive:
            lines.append(f"| {edit.kind} | `{edit.path}:{edit.line_no}` | `{edit.recipe_id}` |")
        lines.append("")

    if result is not None and result.orphan_branch:  # pragma: no cover - body reused on retry
        lines += [f"Branch pushed without a PR on an earlier attempt: `{result.orphan_branch}`", ""]

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

    gaps = list(result.gaps) if result is not None else list(proposal.gaps)
    if gaps:
        lines += [
            "### Gaps (reported, not guessed)",
            "",
        ]
        for entry in gaps:
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
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=(
            "name of the environment variable holding the scoped token. A shell identifier: "
            "it is interpolated into the credential helper and is refused otherwise."
        ),
    )
    parser.add_argument(
        "--branch-suffix",
        default="",
        help="branch name suffix. Omitted, a per-run id is generated so two runs never collide.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = BotConfig(
        repo=args.repo,
        token_env=args.token_env,
        account_source=args.account_source,
        feature_tag=args.feature_tag,
        branch_suffix=args.branch_suffix,
    )
    try:
        result = run_bot(config, dry_run=args.dry_run)
    except RunFailed as exc:
        # The run changed the remote before it failed. The result still prints, so the
        # branch it left behind is recoverable rather than lost with the exception.
        json.dump(exc.result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stderr.write(f"{exc}\n")
        return 1
    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
