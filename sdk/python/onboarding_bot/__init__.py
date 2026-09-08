# SPDX-License-Identifier: Apache-2.0
"""ai-tally hosted repo PR bot (CTO-261 sections 4.3, 12 P2).

The third delivery form, and the highest trust ask: given a scoped, revocable token the
operator supplies, it clones a repo server-side, runs the section 3 loop (detect the stack,
retrieve recipes, ask the account question, propose a diff), pushes a NEW branch, and opens
a reviewed pull request.

Built as a headless bot rather than a GitHub App, per the architecture decision on this
ticket. A GitHub App stays the hardening path (section 11 Q5) and would change only how the
credential is minted, not the loop or the refusals.

Two properties hold the whole component together:

  * It generates nothing itself. Every emitted line comes from the shared recipe catalog
    through :mod:`onboarding_mcp`, the same catalog the MCP server and the dashboard form
    read (section 4.1), so a stack with no recipe is a reported gap and never an invented
    ``record_*`` call.
  * The section 9 posture is code, not prose: :mod:`onboarding_bot.guards` refuses a push to
    a default branch, refuses every git subcommand and GitHub endpoint outside the narrow
    set a reviewed PR needs (merge is on neither list), and redacts the token from anything
    the component emits. The working clone is deleted in a ``finally`` and by a SIGTERM /
    SIGINT handler, so a stopped hosted run leaves no clone behind either.
"""

from .config import DEFAULT_TOKEN_ENV, BotConfig, resolve_token
from .guards import SecurityViolation
from .propose import ACCOUNT_QUESTION, Proposal, ProposedEdit, account_question, build_proposal
from .run import RunFailed, RunResult, pr_body, run_bot

__all__ = [
    "BotConfig",
    "DEFAULT_TOKEN_ENV",
    "resolve_token",
    "SecurityViolation",
    "Proposal",
    "ProposedEdit",
    "ACCOUNT_QUESTION",
    "account_question",
    "build_proposal",
    "RunResult",
    "RunFailed",
    "run_bot",
    "pr_body",
]
