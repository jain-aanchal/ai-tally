# SPDX-License-Identifier: Apache-2.0
"""Boot-time model discovery wiring (CTO-109).

Asserts the gateway's lifespan populates ``app.state.models`` from ``tally.models``,
falls back to the cache when live fetch raises, and still boots cleanly when both
the live call and the cache are unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import app
from tally import models as M


def _make(provider: str, model_id: str) -> M.ModelInfo:
    return M.ModelInfo(
        provider=provider,
        id=model_id,
        family=M.classify_family(model_id),
        created_at=datetime(2025, 10, 15, tzinfo=timezone.utc),
        deprecated_at=None,
    )


def test_lifespan_populates_state_models_from_pinned(tmp_path: Path, monkeypatch) -> None:
    # Use TALLY_PINNED_MODELS so discovery is hermetic — no network, no cache hunt
    # in the real .tally/ directory the developer may have on disk.
    pinned = tmp_path / "pinned.json"
    M.save_cache(
        [_make("openai", "gpt-4o-mini"), _make("anthropic", "claude-sonnet-4-5")],
        pinned,
    )
    monkeypatch.setenv("TALLY_PINNED_MODELS", str(pinned))

    with TestClient(app) as client:
        # /healthz forces lifespan startup to run.
        assert client.get("/healthz").status_code == 200
        ids = sorted(m.id for m in app.state.models)
        assert ids == ["claude-sonnet-4-5", "gpt-4o-mini"]


def test_lifespan_falls_back_to_cache_when_fetch_raises(
    tmp_path: Path, monkeypatch
) -> None:
    # Seed a stale cache (older than the TTL) and have the live fetcher blow up.
    # discover_models() must surface the stale entries rather than crash the boot.
    cache = tmp_path / "models.json"
    M.save_cache([_make("anthropic", "claude-haiku-4-5")], cache)
    import os
    import time as _time

    old = _time.time() - (M.CACHE_TTL_SECONDS + 60)
    os.utime(cache, (old, old))

    def boom(req, timeout=None):  # noqa: ARG001
        raise TimeoutError("network down")

    monkeypatch.setattr(M.urllib.request, "urlopen", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.delenv("TALLY_MODELS_REFRESH", raising=False)

    # Point discover_models at our temp cache by monkeypatching the lifespan's call.
    real_discover = M.discover_models
    monkeypatch.setattr(
        "gateway.app.discover_models",
        lambda: real_discover(cache_path=cache),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert [m.id for m in app.state.models] == ["claude-haiku-4-5"]


def test_lifespan_boots_with_empty_list_when_no_cache_and_no_network(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALLY_PINNED_MODELS", raising=False)
    monkeypatch.delenv("TALLY_MODELS_REFRESH", raising=False)

    real_discover = M.discover_models
    absent = tmp_path / "absent.json"
    monkeypatch.setattr(
        "gateway.app.discover_models",
        lambda: real_discover(cache_path=absent),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.models == []
