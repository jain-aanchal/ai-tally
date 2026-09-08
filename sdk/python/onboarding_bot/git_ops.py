# SPDX-License-Identifier: Apache-2.0
"""The only place the bot touches git (CTO-261 sections 4.3, 9).

Every git invocation goes through :meth:`GitRunner.run`, which applies the section 9
allowlist before spawning anything, so the refusals in :mod:`onboarding_bot.guards` cannot
be bypassed by a caller that builds its own command.

Token handling: the token never appears in argv (visible in ``ps``) and never in
``.git/config`` (persisted in the clone). It is passed to git through ``GIT_ASKPASS``,
pointing at a small helper that reads the operator's environment variable at prompt time.
The clone URL carries the username only, so git asks for a password and nothing else.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BotConfig, resolve_token
from .guards import (
    SecurityViolation,
    assert_git_subcommand_allowed,
    assert_push_flags_allowed,
    assert_push_target_allowed,
    redact,
)

# Written into the run's working directory, not the clone, so it is removed with the rest
# of the run and never lands inside the repo the PR touches. It holds no secret: it reads
# the operator's env var by name at the moment git prompts.
BOT_NAME = "ai-tally onboarding bot"
BOT_EMAIL = "onboarding-bot@ai-tally.invalid"

_ASKPASS_TEMPLATE = """#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# CTO-261 section 9: hands git the scoped token from the environment so it never appears
# in argv or in .git/config. Answers the username prompt with the GitHub bot user.
case "$1" in
  Username*) printf '%s' 'x-access-token' ;;
  *) printf '%s' "${token_env}" ;;
esac
"""


@dataclass
class GitResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class GitRunner:
    """Runs git in one working directory under the section 9 allowlist."""

    def __init__(self, config: BotConfig, workdir: Path):
        self.config = config
        self.workdir = workdir
        self.commands: list[list[str]] = []
        """Argv of every git command actually run, redacted. The audit trail tests read."""
        self._askpass: Path | None = None

    # -- environment -------------------------------------------------------- #

    def _askpass_path(self) -> Path:
        if self._askpass is None:
            path = self.workdir / "askpass.sh"
            path.write_text(_ASKPASS_TEMPLATE.replace("${token_env}", f"${self.config.token_env}"))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            self._askpass = path
        return self._askpass

    def _env(self, *, with_credentials: bool) -> dict[str, str]:
        env = dict(os.environ)
        # No interactive prompt can ever appear: a hosted run has no terminal, and a hang
        # waiting on one would look like a stall rather than a missing credential.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        # Authorship rides in the environment rather than in `git -c` or a written config,
        # so the run leaves no identity behind on the host and the subcommand allowlist
        # stays a plain first-argument check.
        env["GIT_AUTHOR_NAME"] = BOT_NAME
        env["GIT_AUTHOR_EMAIL"] = BOT_EMAIL
        env["GIT_COMMITTER_NAME"] = BOT_NAME
        env["GIT_COMMITTER_EMAIL"] = BOT_EMAIL
        if with_credentials:
            # resolve_token raises when the operator has revoked or never supplied the
            # token, which is the intended failure: no push, no PR, a clear error.
            resolve_token(self.config)
            env["GIT_ASKPASS"] = str(self._askpass_path())
        return env

    # -- command surface ---------------------------------------------------- #

    def run(self, *args: str, cwd: Path | None = None, with_credentials: bool = False) -> GitResult:
        argv = list(args)
        assert_git_subcommand_allowed(argv)
        token = os.environ.get(self.config.token_env) or None
        self.commands.append([redact(a, token) for a in argv])
        proc = subprocess.run(  # noqa: S603 - argv is a fixed allowlisted git command
            ["git", *argv],
            cwd=str(cwd or self.workdir),
            env=self._env(with_credentials=with_credentials),
            capture_output=True,
            text=True,
        )
        result = GitResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=redact(proc.stdout, token),
            stderr=redact(proc.stderr, token),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(result.argv)} failed: {result.stderr.strip()}")
        return result

    # -- the operations the loop needs -------------------------------------- #

    def clone(self, dest: Path, *, depth: int = 1) -> Path:
        """Shallow-clone the target repo into ``dest``.

        Shallow because the bot reads the current tree and nothing else; history it does not
        need is source it should not hold (section 9: retain no more than the run requires).
        """
        owner, name = self.config.owner_and_name
        url = f"{self.config.clone_base}/{owner}/{name}.git"
        self.run("clone", "--depth", str(depth), url, str(dest), with_credentials=True)
        return dest

    def default_branch(self, repo_dir: Path) -> str:
        """Resolve the clone's default branch, the branch a push is refused against."""
        result = self.run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            raise SecurityViolation(
                "could not resolve the repo's default branch; refusing to push without "
                "knowing what the protected branch is (section 9)"
            )
        return branch

    def create_branch(self, repo_dir: Path, branch: str, default_branch: str) -> None:
        """Create the new working branch, refusing anything that resolves to the default."""
        assert_push_target_allowed(branch, default_branch)
        self.run("checkout", "-b", branch, cwd=repo_dir)

    def commit_all(self, repo_dir: Path, message: str) -> None:
        self.run("add", "-A", cwd=repo_dir)
        self.run("commit", "-m", message, cwd=repo_dir)

    def push_branch(self, repo_dir: Path, branch: str, default_branch: str) -> None:
        """Push the new branch. The single push path in the component, and it is guarded."""
        assert_push_target_allowed(branch, default_branch)
        assert_push_flags_allowed([])
        self.run("push", "origin", f"{branch}:{branch}", cwd=repo_dir, with_credentials=True)
