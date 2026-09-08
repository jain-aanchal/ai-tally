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
import secrets
import time
from dataclasses import dataclass, field

from .guards import (
    SecurityViolation,
    assert_repo_part_allowed,
    assert_token_env_name_allowed,
)

DEFAULT_TOKEN_ENV = "TALLY_ONBOARDING_GITHUB_TOKEN"
DEFAULT_BRANCH_PREFIX = "tally/onboarding"


def new_run_id() -> str:
    """A per-run branch suffix: a UTC date plus random hex.

    A fixed suffix makes the second run against a repo collide with the first branch and
    die at push with a bare non-fast-forward error and no PR (CTO-261). A run id is the
    smallest thing that makes two runs two branches; the date keeps the name readable for
    the developer who has to find it.
    """
    return f"{time.strftime('%Y%m%d', time.gmtime())}-{secrets.token_hex(3)}"


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
    """Appended to the generated branch name; a run id in production, fixed in tests.

    Left empty it is filled once, at construction, with :func:`new_run_id`, so two runs
    against the same repo are two branches rather than a push collision."""

    labels: list[str] = field(default_factory=list)
    max_call_sites: int = 25
    """Cap on instrumented call sites so one run stays a reviewable diff, not a rewrite."""

    def __post_init__(self) -> None:
        # Validated here rather than at the point of use so a malformed run refuses before
        # it clones anything, and so no unchecked operator string reaches the credential
        # helper or the endpoint allowlist (CTO-261 section 9).
        assert_token_env_name_allowed(self.token_env)
        _owner, _name = self.owner_and_name
        if not self.branch_suffix:
            object.__setattr__(self, "branch_suffix", new_run_id())

    def branch_name(self) -> str:
        return f"{self.branch_prefix}/{self.branch_suffix}"

    @property
    def owner_and_name(self) -> tuple[str, str]:
        parts = self.repo.split("/")
        if len(parts) != 2 or not all(parts):
            raise SecurityViolation(f"repo must be 'owner/name', got {self.repo!r}")
        owner, name = parts
        assert_repo_part_allowed(owner, field="owner")
        assert_repo_part_allowed(name, field="name")
        return owner, name


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
