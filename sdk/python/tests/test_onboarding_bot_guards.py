# SPDX-License-Identifier: Apache-2.0
"""The section 9 posture, asserted rather than described (CTO-261).

These are the tests that make "never pushes to a default branch" and "never merges" real
properties of the component: if a future edit reaches for a merge or a default-branch push,
one of these fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from onboarding_bot import github_pr, guards
from onboarding_bot.config import BotConfig, resolve_token
from onboarding_bot.git_ops import GitRunner
from onboarding_bot.guards import SecurityViolation

TOKEN_ENV = "TALLY_TEST_GITHUB_TOKEN"


def _config(**kwargs) -> BotConfig:
    return BotConfig(repo="acme/widgets", token_env=TOKEN_ENV, **kwargs)


# --- never push to a default branch ---------------------------------------- #


def test_push_to_the_repos_default_branch_is_refused():
    with pytest.raises(SecurityViolation) as exc:
        guards.assert_push_target_allowed("release", default_branch="release")
    assert "default branch" in str(exc.value)


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "develop", "MAIN"])
def test_conventionally_protected_branches_are_refused_even_when_head_says_otherwise(branch):
    # The remote could report an unusual HEAD; these names are refused regardless.
    with pytest.raises(SecurityViolation):
        guards.assert_push_target_allowed(branch, default_branch="something-else")


def test_a_new_branch_is_allowed():
    guards.assert_push_target_allowed("tally/onboarding/wire-tally", default_branch="main")


def test_empty_and_option_shaped_branch_names_are_refused():
    for bad in ("", "   ", "--delete"):
        with pytest.raises(SecurityViolation):
            guards.assert_push_target_allowed(bad, default_branch="main")


def test_git_runner_refuses_to_push_the_default_branch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "ghp_not_a_real_token")
    runner = GitRunner(_config(), tmp_path)
    with pytest.raises(SecurityViolation):
        runner.push_branch(tmp_path, "main", default_branch="main")
    # The refusal happens before git is spawned, so nothing was attempted.
    assert runner.commands == []


def test_create_branch_refuses_the_default_branch(tmp_path: Path):
    runner = GitRunner(_config(), tmp_path)
    with pytest.raises(SecurityViolation):
        runner.create_branch(tmp_path, "main", default_branch="main")
    assert runner.commands == []


def test_force_push_flags_are_refused():
    for flag in ("-f", "--force", "--force-with-lease"):
        with pytest.raises(SecurityViolation):
            guards.assert_push_flags_allowed([flag])


# --- never merge ------------------------------------------------------------ #


@pytest.mark.parametrize("subcommand", ["merge", "rebase", "cherry-pick", "reset", "filter-branch"])
def test_history_joining_git_subcommands_are_refused(subcommand):
    with pytest.raises(SecurityViolation) as exc:
        guards.assert_git_subcommand_allowed([subcommand, "origin/main"])
    assert "never merges" in str(exc.value)


def test_git_global_options_cannot_smuggle_a_subcommand_past_the_allowlist():
    with pytest.raises(SecurityViolation):
        guards.assert_git_subcommand_allowed(["-c", "alias.x=!git merge", "x"])


def test_the_git_runner_applies_the_allowlist(tmp_path: Path):
    runner = GitRunner(_config(), tmp_path)
    with pytest.raises(SecurityViolation):
        runner.run("merge", "origin/main")
    assert runner.commands == []


def test_the_merge_endpoint_is_not_reachable():
    with pytest.raises(SecurityViolation):
        guards.assert_endpoint_allowed("PUT", "/repos/acme/widgets/pulls/7/merge")
    with pytest.raises(SecurityViolation):
        guards.assert_endpoint_allowed("POST", "/repos/acme/widgets/merges")
    # Opening a PR is the one write the bot performs.
    guards.assert_endpoint_allowed("POST", "/repos/acme/widgets/pulls")


def test_the_pr_module_exposes_no_merge_function():
    # A merge helper cannot be added quietly: it would have to fail the allowlist above and
    # this name check at the same time.
    assert [name for name in dir(github_pr) if "merge" in name.lower()] == []


def test_opening_a_pr_onto_the_same_branch_is_refused(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "ghp_not_a_real_token")
    with pytest.raises(SecurityViolation):
        github_pr.open_pull_request(
            _config(), head="main", base="main", title="t", body="b", transport=_no_network
        )


def _no_network(method, url, payload, headers):  # pragma: no cover - must never be reached
    raise AssertionError(f"the test transport was called: {method} {url}")


# --- credentials by reference ----------------------------------------------- #


def test_the_config_holds_a_token_reference_not_a_token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    config = _config()
    assert "ghp_secret_value" not in repr(config)
    assert resolve_token(config) == "ghp_secret_value"


def test_a_missing_token_refuses_rather_than_running_unauthenticated(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(SecurityViolation):
        resolve_token(_config())


def test_the_token_never_appears_in_the_command_audit_trail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    repo = _init_repo(tmp_path / "repo")
    runner = GitRunner(_config(), tmp_path)
    runner.run("status", "--short", cwd=repo)
    flat = " ".join(arg for cmd in runner.commands for arg in cmd)
    assert "ghp_secret_value" not in flat


def test_the_askpass_helper_holds_no_secret_only_the_env_var_name(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    runner = GitRunner(_config(), tmp_path)
    runner._env(with_credentials=True)
    helper = (tmp_path / "askpass.sh").read_text()
    assert "ghp_secret_value" not in helper
    assert f"${TOKEN_ENV}" in helper


def test_redact_strips_the_secret_from_anything_surfaced():
    assert "ghp_x" not in guards.redact("fatal: auth failed for ghp_x", "ghp_x")
    assert guards.redact("clean", None) == "clean"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path
