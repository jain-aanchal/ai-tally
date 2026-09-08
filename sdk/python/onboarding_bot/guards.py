# SPDX-License-Identifier: Apache-2.0
"""The security posture, enforced in code (CTO-261 section 9).

Section 9 is not a promise in a doc, it is a set of refusals this module owns and every
other module in the bot routes through:

  * The bot never pushes to a default (or otherwise protected) branch. It pushes the new
    branch it created and nothing else, so a human always reviews.
  * The bot never merges. Rather than remembering not to call ``git merge`` and the
    merge endpoint, both surfaces are allowlisted here: an unlisted git subcommand and an
    unlisted GitHub endpoint are refused, so a merge cannot be reached even by a future
    edit that forgets the rule.
  * The token is held by reference (an environment variable name) and redacted from every
    string this component emits, so it cannot reach a log, a PR body, or an exception.

Everything raises :class:`SecurityViolation`, which is deliberately not an
``OSError``/``ValueError`` subclass: a caller that swallows subprocess or parse errors
still cannot swallow a refusal by accident.
"""

from __future__ import annotations

import re

# Git subcommands the bot is allowed to run. This is an allowlist rather than a
# "merge" denylist because the point is that no code path reaches a history-rewriting or
# history-joining command at all: merge, rebase, cherry-pick, reset and their friends are
# absent, so they refuse without anyone having to enumerate them (section 9).
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "clone",
        "checkout",
        "switch",
        "add",
        "commit",
        "push",
        "rev-parse",
        "symbolic-ref",
        "remote",
        "config",
        "status",
        "diff",
        "ls-remote",
    }
)

# Branch names refused as a push target regardless of what the caller passes. The repo's
# real default branch is resolved at run time and refused on top of this list; these are
# the conventional names, refused even when a remote misreports its HEAD.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "default"})

# GitHub REST paths the bot may call. Reading a repo and opening a pull request only.
# The merge endpoints (PUT /pulls/{n}/merge, POST /merges) are not here and cannot be
# added by a caller, which is what makes "never merges" a property and not a habit.
_ALLOWED_ENDPOINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/repos/[^/]+/[^/]+$")),
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/pulls$")),
)

# GitHub's own owner / repository character set. Anything else (a `?`, a `#`, a `/`) makes
# the path the allowlist approves differ from the path urllib actually requests: `?ref=x`
# in the name turns POST /repos/o/n%3Fref%3Dx/pulls into POST /repos/o/n with a query
# string, so the allowlist would have approved a request that was never sent (CTO-261
# section 9).
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Environment variable names, as the shell defines them. The name is interpolated into the
# GIT_ASKPASS helper, so an unconstrained name is a shell expression in a module whose whole
# thesis is allowlisting (CTO-261 section 9).
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecurityViolation(Exception):
    """A refused action under the section 9 posture. Never caught internally."""


def assert_repo_part_allowed(part: str, *, field: str) -> None:
    """Refuse an owner or repository name that is not what GitHub would accept.

    The endpoint allowlist matches on the path the bot builds. A name carrying a query or
    a path separator makes that path a different request than the one urllib sends, so the
    only safe check is on the parts before the path exists.
    """
    if not _REPO_PART_RE.match(part) or part in (".", ".."):
        raise SecurityViolation(
            f"refusing repository {field} {part!r}: only letters, digits, '.', '_' and '-' "
            f"are accepted, so the path checked against the allowlist is the path requested "
            f"(section 9)"
        )


def assert_token_env_name_allowed(name: str) -> None:
    """Refuse a token environment variable name that is not a bare shell identifier.

    The name is written into the GIT_ASKPASS helper as ``"$NAME"``. Anything that can close
    that quote runs as a command when git prompts, which is why the one operator-supplied
    string that reaches a shell is allowlisted like every other surface here (section 9).
    """
    if not _ENV_NAME_RE.match(name):
        raise SecurityViolation(
            f"refusing token environment variable name {name!r}: it must match "
            f"[A-Za-z_][A-Za-z0-9_]* so it cannot carry shell syntax into the credential "
            f"helper (section 9)"
        )


def normalize_branch(name: str) -> str:
    """Strip a ``refs/heads/`` namespace so two spellings of one branch compare equal.

    ``refs/heads/main`` and ``main`` are the same branch to git. Comparing the raw strings
    would let the namespaced spelling walk past the default-branch refusal, so the refusal
    compares normalized names (section 9 hardening).
    """
    stripped = name.strip()
    while stripped.startswith("refs/heads/"):
        stripped = stripped[len("refs/heads/") :]
    return stripped


def assert_git_subcommand_allowed(argv: list[str]) -> None:
    """Refuse any git invocation whose subcommand is not on the allowlist.

    The subcommand must be the first argument. Global options before it (``-c
    alias.x=!merge``, ``--exec-path``) would let a caller smuggle behaviour past the
    allowlist, so they are refused outright rather than parsed.
    """
    if not argv:
        raise SecurityViolation("refusing a git invocation with no subcommand")
    subcommand = argv[0]
    if subcommand.startswith("-"):
        raise SecurityViolation(
            f"refusing git global option {subcommand!r} before the subcommand; the bot runs "
            f"plain allowlisted subcommands only (section 9)"
        )
    if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
        raise SecurityViolation(
            f"git {subcommand!r} is not on the onboarding bot's allowlist "
            f"(section 9: the bot proposes a reviewed PR, it never merges or rewrites history)"
        )


def assert_push_target_allowed(branch: str, default_branch: str) -> None:
    """Refuse a push to the repo's default branch or to any conventionally protected name.

    Called by the git wrapper before every push, so there is exactly one place a push
    target is decided and it is this one (section 9: never a direct push to a default
    branch).
    """
    name = normalize_branch(branch)
    if not name:
        raise SecurityViolation("refusing a push with an empty branch name")
    if name.startswith("-"):
        raise SecurityViolation(f"refusing a push to option-shaped branch name {branch!r}")
    if name == normalize_branch(default_branch):
        raise SecurityViolation(
            f"refusing to push to the default branch {default_branch!r}: the onboarding bot "
            f"opens a reviewed PR from a new branch and never writes to a default branch "
            f"(section 9)"
        )
    if name.lower() in PROTECTED_BRANCHES:
        raise SecurityViolation(
            f"refusing to push to protected branch {branch!r}: the onboarding bot pushes only "
            f"the new branch it created (section 9)"
        )


def assert_push_flags_allowed(flags: list[str]) -> None:
    """Refuse a force push. A reviewed PR never needs to overwrite existing history."""
    for flag in flags:
        if flag in ("-f", "--force") or flag.startswith("--force"):
            raise SecurityViolation(f"refusing force push flag {flag!r} (section 9)")


def assert_endpoint_allowed(method: str, path: str) -> None:
    """Refuse any GitHub call outside read-the-repo and open-a-pull-request."""
    for allowed_method, pattern in _ALLOWED_ENDPOINTS:
        if method.upper() == allowed_method and pattern.match(path):
            return
    raise SecurityViolation(
        f"{method.upper()} {path} is not on the onboarding bot's GitHub allowlist; the bot "
        f"opens a pull request and never merges one (section 9)"
    )


def redact(text: str, *secrets: str | None) -> str:
    """Strip supplied secrets from a string before it is logged, raised, or returned.

    Credentials are held by reference (section 9, CLAUDE.md), but a token still passes
    through memory on its way to git. Every string this component surfaces goes through
    here first so a token cannot ride out in a command echo or an exception message.
    """
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***redacted***")
    return cleaned
