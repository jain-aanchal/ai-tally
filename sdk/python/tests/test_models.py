# SPDX-License-Identifier: Apache-2.0
"""Tests for ``tally.models`` — auto-discovery of provider model lineups.

No live network is touched: ``fetch_*_models`` is exercised via ``monkeypatch`` over
``urllib.request.urlopen`` with fixture JSON that mimics the real ``/v1/models`` shape.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tally import models as M

FIXTURES = Path(__file__).parent / "fixtures"


def test_classify_family_truth_table() -> None:
    # The truth table the resolvers depend on. Adding a row here is the right way
    # to register a new provider naming convention.
    cases = {
        "gpt-4o-mini": "mini",
        "claude-sonnet-4-5": "sonnet",
        "claude-opus-4-8": "opus",
        "claude-3-5-haiku-20241022": "haiku",
        "claude-haiku-4-5": "haiku",
        "gpt-4o": "flagship",
        "text-embedding-3-large": "embedding",
        "gpt-5": "flagship",
        "ft:gpt-3.5-turbo:acme": "other",
        "gemini-2.5-flash": "flash",
        "gemini-3-flash": "flash",
        "gemini-2.5-pro": "pro",
        # Non-chat OpenAI SKUs must NOT land in a chat family — they'd 404 on
        # chat-completions if resolveLatest handed them over.
        "gpt-4o-mini-tts": "other",
        "gpt-4o-mini-transcribe": "other",
        "gpt-4o-mini-realtime-preview": "other",
        "gpt-4o-audio-preview": "other",
        "gpt-4o-realtime-preview": "other",
        "gpt-4o-search-preview": "other",
        "gpt-3.5-turbo-instruct": "other",
        "dall-e-3": "other",
        "whisper-1": "other",
        "omni-moderation-latest": "other",
        # o-series reasoning minis are chat-capable — they stay in "mini".
        "o3-mini": "mini",
    }
    for model_id, expected in cases.items():
        assert M.classify_family(model_id) == expected, model_id


def test_latest_openai_mini_skips_non_chat_skus() -> None:
    # Regression (chatbot demo 404 "not a chat model"): OpenAI's /v1/models returns
    # non-chat SKUs carrying "mini" (e.g. gpt-4o-mini-tts). Even when one is NEWER than
    # the real chat mini, latest_openai("mini", …) must return the chat model, because
    # the non-chat SKUs are classified out of the "mini" family entirely.
    older = datetime(2024, 7, 18, tzinfo=timezone.utc)
    newer = datetime(2025, 3, 20, tzinfo=timezone.utc)
    lineup = [
        _make("openai", "gpt-4o-mini", created=older),          # real chat mini
        _make("openai", "gpt-4o-mini-tts", created=newer),      # non-chat, but newer
        _make("openai", "gpt-4o-mini-realtime-preview", created=newer),
    ]
    picked = M.latest_openai("mini", lineup)
    assert picked is not None
    assert picked.id == "gpt-4o-mini"


def _make(
    provider: str,
    model_id: str,
    *,
    created: datetime | None,
    deprecated=None,
) -> M.ModelInfo:
    return M.ModelInfo(
        provider=provider,
        id=model_id,
        family=M.classify_family(model_id),
        created_at=created,
        deprecated_at=deprecated,
    )


def test_latest_prefers_undated_alias_over_dated_snapshot() -> None:
    # Provider returns both the moving alias ("claude-sonnet-4-5") and the pinned snapshot
    # ("claude-sonnet-4-5-20251015"). The alias is what docs and SDKs recommend, so the
    # resolver must prefer it even when the snapshot has the same created_at.
    same_ts = datetime(2025, 10, 15, tzinfo=timezone.utc)
    lineup = [
        _make("anthropic", "claude-sonnet-4-5-20251015", created=same_ts),
        _make("anthropic", "claude-sonnet-4-5", created=same_ts),
    ]
    pick = M.latest_anthropic("sonnet", lineup)
    assert pick is not None
    assert pick.id == "claude-sonnet-4-5"


def test_latest_skips_deprecated() -> None:
    now = datetime.now(tz=timezone.utc)
    lineup = [
        # Deprecated, newer created_at — must be skipped despite being newer.
        _make("anthropic", "claude-haiku-3-5", created=now, deprecated=now - timedelta(days=1)),
        _make("anthropic", "claude-haiku-4-5", created=now - timedelta(days=30)),
    ]
    pick = M.latest_anthropic("haiku", lineup)
    assert pick is not None
    assert pick.id == "claude-haiku-4-5"


def test_latest_returns_none_for_unknown_family() -> None:
    lineup = [_make("openai", "gpt-4o", created=None)]
    assert M.latest_openai("haiku", lineup) is None


def test_cache_round_trip(tmp_path: Path) -> None:
    cache = tmp_path / "models.json"
    original = [
        _make("openai", "gpt-4o", created=datetime(2024, 5, 1, tzinfo=timezone.utc)),
        _make(
            "anthropic",
            "claude-sonnet-4-5",
            created=datetime(2025, 10, 15, tzinfo=timezone.utc),
        ),
    ]
    M.save_cache(original, cache)
    loaded = M.load_cached(cache)
    assert loaded == original


def test_cache_staleness_returns_none(tmp_path: Path) -> None:
    cache = tmp_path / "models.json"
    M.save_cache([_make("openai", "gpt-4o", created=None)], cache)
    # Force the file's mtime to look older than the TTL. The TTL check uses mtime,
    # not any timestamp embedded in the JSON, so this is the right knob.
    old = time.time() - (M.CACHE_TTL_SECONDS + 60)
    os.utime(cache, (old, old))
    assert M.load_cached(cache) is None


def test_cache_missing_returns_none(tmp_path: Path) -> None:
    assert M.load_cached(tmp_path / "absent.json") is None


def test_pinned_models_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # TALLY_PINNED_MODELS lets a CI run hardcode a known-good list and bypass the network
    # entirely. discover_models should pick it up before consulting any cache.
    pinned = tmp_path / "pinned.json"
    pinned_models = [_make("openai", "gpt-4o", created=None)]
    M.save_cache(pinned_models, pinned)

    other_cache = tmp_path / "should_not_be_touched.json"
    M.save_cache([_make("anthropic", "claude-opus-4-8", created=None)], other_cache)

    monkeypatch.setenv("TALLY_PINNED_MODELS", str(pinned))
    monkeypatch.delenv("TALLY_MODELS_REFRESH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = M.discover_models(cache_path=other_cache)
    assert [m.id for m in result] == ["gpt-4o"]


def _fake_urlopen_factory(payloads_by_url: dict[str, dict]):
    """Returns a fake urlopen that dispatches on request URL and yields fixture JSON."""

    class _Resp:
        def __init__(self, body: bytes):
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for prefix, payload in payloads_by_url.items():
            if url.startswith(prefix):
                return _Resp(json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"unexpected URL {url}")

    return fake_urlopen


def test_fetch_anthropic_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "anthropic_models.json").read_text())
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"https://api.anthropic.com/v1/models": payload}),
    )
    out = M.fetch_anthropic_models("fake-key")
    ids = {m.id for m in out}
    assert "claude-sonnet-4-5" in ids
    assert "claude-haiku-4-5" in ids
    # The deprecated row must carry its deprecated_at through so latest() can skip it.
    dep = next(m for m in out if m.id == "claude-3-5-haiku-20241022")
    assert dep.deprecated_at is not None
    # latest_anthropic("haiku") with this fixture must NOT return the retired 3.5 model.
    pick = M.latest_anthropic("haiku", out)
    assert pick is not None and pick.id == "claude-haiku-4-5"


def test_fetch_openai_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "openai_models.json").read_text())
    monkeypatch.setattr(
        M.urllib.request,
        "urlopen",
        _fake_urlopen_factory({"https://api.openai.com/v1/models": payload}),
    )
    out = M.fetch_openai_models("fake-key")
    assert {m.id for m in out} == {"gpt-4o", "gpt-4o-mini", "gpt-5", "text-embedding-3-large"}
    mini = M.latest_openai("mini", out)
    assert mini is not None and mini.id == "gpt-4o-mini"


def test_discover_falls_back_to_stale_cache_on_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Write a stale cache (older than the TTL) and have the live fetch raise. The
    # gateway must still boot — that's the whole point of fail-soft discovery.
    cache = tmp_path / "models.json"
    M.save_cache([_make("anthropic", "claude-sonnet-4-5", created=None)], cache)
    old = time.time() - (M.CACHE_TTL_SECONDS + 60)
    os.utime(cache, (old, old))

    def boom(req, timeout=None):  # noqa: ARG001
        raise TimeoutError("network is down")

    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.delenv("TALLY_MODELS_REFRESH", raising=False)

    result = M.discover_models(cache_path=cache)
    assert [m.id for m in result] == ["claude-sonnet-4-5"]


def test_discover_returns_empty_when_no_cache_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.delenv("TALLY_MODELS_REFRESH", raising=False)
    result = M.discover_models(cache_path=tmp_path / "absent.json")
    assert result == []
