# SPDX-License-Identifier: Apache-2.0
"""Bot configuration and the token-by-reference rule (CTO-261 sections 4.3, 9).

The operator supplies a scoped, revocable GitHub token. This module never holds its
value: :class:`BotConfig` holds the NAME of the environment variable that carries it,
and :func:`resolve_token` reads it at the moment git or the GitHub API needs it. That is
the CLAUDE.md "credentials by reference" rule applied to the one credential this
component touches, and it is why a serialized ``BotConfig`` (a log line, a job record)
cannot contain the token.

Supplying and revoking the token is documented in ``sdk/python/README.md``; the short
version is a fine-grained personal access token scoped to the single target repository
with Contents: read and write plus Pull requests: read and write, and revoked from the
same settings page when the run is done.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .guards import SecurityViolation

DEFAULT_TOKEN_ENV = "TALLY_ONBOARDING_GITHUB_TOKEN"
DEFAULT_BRANCH_PREFIX = "tally/onboarding"


@dataclass(frozen=True)
class BotConfig:
    """One run's inputs. Contains a token reference, never a token."""

    repo: str
    """``owner/name`` of the target repository."""

    token_env: str = DEFAULT_TOKEN_ENV
    """Name of the environment variable holding the scoped token."""

    api_base: str = "https://api.github.com"
    clone_base: str = "https://github.com"

    account_source: str | None = None
    """The developer's answer to the section 6 question, verbatim. ``None`` means unanswered,
    which yields a reported gap and an unattributed account layer, never a guessed resolver."""

    feature_tag: str | None = None
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    branch_suffix: str = ""
    """Appended to the generated branch name; a run id in production, fixed in tests."""

    labels: list[str] = field(default_factory=list)
    max_call_sites: int = 25
    """Cap on instrumented call sites so one run stays a reviewable diff, not a rewrite."""

    def branch_name(self) -> str:
        suffix = self.branch_suffix or "wire-tally"
        return f"{self.branch_prefix}/{suffix}"

    @property
    def owner_and_name(self) -> tuple[str, str]:
        parts = self.repo.split("/")
        if len(parts) != 2 or not all(parts):
            raise SecurityViolation(f"repo must be 'owner/name', got {self.repo!r}")
        return parts[0], parts[1]


def resolve_token(config: BotConfig) -> str:
    """Read the scoped token from the environment at the point of use.

    Raises rather than degrading to an unauthenticated run: a silent fallback would push
    nothing and report success, which is exactly the fabricated outcome CLAUDE.md forbids.
    """
    token = os.environ.get(config.token_env, "")
    if not token:
        raise SecurityViolation(
            f"no GitHub token in ${config.token_env}. Supply a scoped, revocable token by "
            f"reference (environment or secret manager); the bot never reads one from a file "
            f"in the repo and never accepts one inline."
        )
    return token
