# SPDX-License-Identifier: Apache-2.0
"""End to end: branch, commit, push, PR, and the refusals around them (CTO-261 P2).

The run is exercised against a real local git repository with a real bare remote, and a
stub GitHub transport, so the branch and PR behaviour is asserted for real without a
network. What the run must never do (touch the default branch, merge, retain the clone) is
asserted on the resulting remote and on the recorded command trail.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from onboarding_bot.config import BotConfig
from onboarding_bot.guards import SecurityViolation
from onboarding_bot.run import RunResult, pr_body, run_bot

TOKEN_ENV = "TALLY_TEST_GITHUB_TOKEN"
DEFAULT_BRANCH = "main"

APP_SOURCE = '''\
"""Sample service."""
import os

from fastapi import FastAPI
from pinecone import Pinecone

app = FastAPI()
index = Pinecone(api_key=os.environ["PINECONE_KEY"]).Index("docs")


@app.post("/ask")
def ask(question: str):
    hits = index.query(vector=[0.1], top_k=3)
    return hits
'''


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return proc.stdout


def _git_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    return env


@pytest.fixture()
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone on ``main`` plus a bare remote to push to."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    work = tmp_path / "work"
    (work / "app").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    _git("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}", cwd=work)
    (work / "requirements.txt").write_text("fastapi\nopenai\npinecone\n")
    (work / "app" / "main.py").write_text(APP_SOURCE)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "initial", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-q", "origin", DEFAULT_BRANCH, cwd=work)
    return work, remote


def _config(**kwargs) -> BotConfig:
    base = {
        "repo": "acme/widgets",
        "token_env": TOKEN_ENV,
        "branch_suffix": "run-1",
        "account_source": 'request.headers.get("X-Customer-Id")',
        "feature_tag": "ask",
    }
    base.update(kwargs)
    return BotConfig(**base)


class _StubGitHub:
    """Records what the bot asks GitHub to do. Fails loudly on anything unexpected."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, payload, headers):
        self.calls.append((method, url, payload or {}))
        assert headers["Authorization"].startswith("Bearer ")
        return {"html_url": "https://github.com/acme/widgets/pull/1", "number": 1}


def test_a_run_opens_a_pr_from_a_new_branch(repo_with_remote, monkeypatch):
    work, remote = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    github = _StubGitHub()

    result = run_bot(_config(), transport=github, clone_override=work)

    assert isinstance(result, RunResult)
    assert result.base_branch == DEFAULT_BRANCH
    assert result.branch == "tally/onboarding/run-1"
    assert result.pr_url == "https://github.com/acme/widgets/pull/1"
    assert result.files_changed == ["app/main.py"]
    assert {"startup", "account", "vector"} <= set(result.layers_wired)

    # Exactly one GitHub write, and it is "open a pull request".
    assert len(github.calls) == 1
    method, url, payload = github.calls[0]
    assert (method, url.endswith("/repos/acme/widgets/pulls")) == ("POST", True)
    assert payload["head"] == "tally/onboarding/run-1"
    assert payload["base"] == DEFAULT_BRANCH


def test_the_default_branch_is_untouched_on_the_remote(repo_with_remote, monkeypatch):
    work, remote = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    before = _git("rev-parse", DEFAULT_BRANCH, cwd=remote).strip()

    run_bot(_config(), transport=_StubGitHub(), clone_override=work)

    after = _git("rev-parse", DEFAULT_BRANCH, cwd=remote).strip()
    assert after == before, "the bot must never move the default branch"
    refs = _git("for-each-ref", "--format=%(refname)", cwd=remote).splitlines()
    assert sorted(refs) == [
        f"refs/heads/{DEFAULT_BRANCH}",
        "refs/heads/tally/onboarding/run-1",
    ]


def test_the_run_never_invokes_a_merge(repo_with_remote, monkeypatch):
    work, _ = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    github = _StubGitHub()

    run_bot(_config(), transport=github, clone_override=work)

    # No merge on either surface: not a git subcommand, not a GitHub call.
    assert all("merge" not in url for _, url, _ in github.calls)
    log = _git("log", "--oneline", "--merges", cwd=work).strip()
    assert log == "", "the bot created no merge commit"


def test_a_branch_that_resolves_to_the_default_is_refused_before_any_work(
    repo_with_remote, monkeypatch
):
    work, remote = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    before = _git("rev-parse", DEFAULT_BRANCH, cwd=remote).strip()

    class _DefaultBranchConfig(BotConfig):
        def branch_name(self) -> str:  # a misconfigured run that aims at the default branch
            return DEFAULT_BRANCH

    config = _DefaultBranchConfig(
        repo="acme/widgets", token_env=TOKEN_ENV, account_source="x", branch_suffix="run-1"
    )
    with pytest.raises(SecurityViolation):
        run_bot(config, transport=_StubGitHub(), clone_override=work)

    assert _git("rev-parse", DEFAULT_BRANCH, cwd=remote).strip() == before
    assert _git("status", "--porcelain", cwd=work).strip() == "", "no edit was written"


def test_the_working_directory_is_removed_even_when_the_run_refuses(
    repo_with_remote, monkeypatch, tmp_path
):
    work, _ = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")

    # Record every working directory the run creates so the cleanup can be asserted on the
    # real path rather than on the run's own say-so.
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _recording_mkdtemp)

    result = run_bot(_config(), transport=_StubGitHub(), clone_override=work)
    assert result.clone_removed is True

    class _DefaultBranchConfig(BotConfig):
        def branch_name(self) -> str:
            return DEFAULT_BRANCH

    with pytest.raises(SecurityViolation):
        run_bot(
            _DefaultBranchConfig(repo="acme/widgets", token_env=TOKEN_ENV, account_source="x"),
            transport=_StubGitHub(),
            clone_override=work,
        )

    assert len(created) == 2
    assert [p for p in created if p.exists()] == [], "no clone survives the run (section 9)"


def test_the_result_and_pr_body_carry_no_repo_source_and_no_token(repo_with_remote, monkeypatch):
    work, _ = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    github = _StubGitHub()

    result = run_bot(_config(), transport=github, clone_override=work)
    body = github.calls[0][2]["body"]
    serialized = json.dumps(result.to_dict())

    for blob in (body, serialized):
        assert "ghp_secret_value" not in blob
        assert "PINECONE_KEY" not in blob
        assert "def ask(question" not in blob
    # The diff carries the code; the body describes it.
    assert "tally.record_vector_call" not in body


def test_an_unanswered_account_question_reaches_the_pr_body(repo_with_remote, monkeypatch):
    work, _ = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    github = _StubGitHub()

    result = run_bot(_config(account_source=None), transport=github, clone_override=work)

    body = github.calls[0][2]["body"]
    assert "How does your app know which customer a request belongs to?" in body
    assert "candidate" in body.lower()
    assert any("unanswered" in g["reason"] for g in result.gaps)
    assert "account" not in result.layers_wired


def test_a_dry_run_creates_no_branch_and_opens_no_pr(repo_with_remote, monkeypatch):
    work, remote = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    github = _StubGitHub()

    result = run_bot(_config(), transport=github, clone_override=work, dry_run=True)

    assert result.branch is None and result.pr_url is None
    assert github.calls == []
    refs = _git("for-each-ref", "--format=%(refname)", cwd=remote).splitlines()
    assert refs == [f"refs/heads/{DEFAULT_BRANCH}"]


def test_the_pr_body_states_how_the_run_was_authorised(repo_with_remote, monkeypatch):
    work, _ = repo_with_remote
    monkeypatch.setenv(TOKEN_ENV, "ghp_secret_value")
    from onboarding_bot.propose import build_proposal

    config = _config()
    body = pr_body(build_proposal(work, account_source=config.account_source), config)
    assert f"${TOKEN_ENV}" in body
    assert "Revoke it" in body
