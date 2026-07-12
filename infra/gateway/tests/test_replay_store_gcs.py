"""Unit tests for the GCS replay blob-store backend (CTO-152).

These use an in-memory fake that mimics the ``google-cloud-storage`` client surface we touch
(``client.bucket(name).blob(key).upload_from_string(...)`` / ``.download_as_bytes()``), so no
network access, real bucket, or credentials are required and ``google-cloud-storage`` need not be
installed to run the suite.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gateway.config import Settings
from gateway.replay_sampler import ReplaySamplePayload
from gateway.replay_store import (
    GCSReplayBlobStore,
    InMemoryReplayBlobStore,
    build_replay_object_key,
    persist_sample,
)

UTC = timezone.utc


# --- Fake GCS client -------------------------------------------------------------------------

class _FakeBlob:
    def __init__(self, store: dict[str, bytes], key: str) -> None:
        self._store = store
        self._key = key

    def upload_from_string(self, body: bytes, content_type: str = "application/json") -> None:
        self._store[self._key] = body

    def download_as_bytes(self) -> bytes:
        return self._store[self._key]


class _FakeBucket:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self._store, key)


class _FakeClient:
    """Stands in for ``google.cloud.storage.Client``. Backed by one dict per client instance."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.buckets_requested: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets_requested.append(name)
        return _FakeBucket(self.objects, name)


def _payload() -> ReplaySamplePayload:
    return ReplaySamplePayload(
        sample_id=uuid4(),
        trace_id="trace-abc",
        feature_tag="checkout",
        real_provider="openai",
        real_model="gpt-4o",
        input_tokens=120,
        output_tokens=45,
        scrubbed_json=b'{"messages": [{"role": "user", "content": "hi"}]}',
        pii_scrubbed=True,
    )


# --- Round-trip ------------------------------------------------------------------------------

def test_persisted_sample_round_trips_through_gcs() -> None:
    client = _FakeClient()
    store = GCSReplayBlobStore(bucket="tally-replay", client=client)
    payload = _payload()
    captured_at = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)

    row = persist_sample(
        blob_store=store,
        tenant_id="tenant-1",
        payload=payload,
        captured_at=captured_at,
    )

    # Keyed identically to the in-memory/S3 path.
    expected_key = build_replay_object_key("tenant-1", payload.sample_id, captured_at)
    assert row.s3_object_key == expected_key
    # Write-then-read returns the same envelope bytes.
    assert store.get_bytes(expected_key) == payload.scrubbed_json


def test_gcs_key_matches_in_memory_backend() -> None:
    client = _FakeClient()
    gcs = GCSReplayBlobStore(bucket="b", client=client)
    mem = InMemoryReplayBlobStore()
    payload = _payload()
    captured_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    gcs_row = persist_sample(
        blob_store=gcs, tenant_id="t", payload=payload, captured_at=captured_at
    )
    mem_row = persist_sample(
        blob_store=mem, tenant_id="t", payload=payload, captured_at=captured_at
    )
    assert gcs_row.s3_object_key == mem_row.s3_object_key
    assert gcs.get_bytes(gcs_row.s3_object_key) == mem.get_bytes(mem_row.s3_object_key)


def test_content_type_passed_and_bucket_used() -> None:
    client = _FakeClient()
    store = GCSReplayBlobStore(bucket="my-bucket", client=client)
    store.put_bytes("tenants/t/x.json", b"{}", content_type="application/json")
    assert client.objects["tenants/t/x.json"] == b"{}"
    assert "my-bucket" in client.buckets_requested


def test_empty_bucket_rejected() -> None:
    with pytest.raises(ValueError):
        GCSReplayBlobStore(bucket="", client=_FakeClient())


# --- Backend selection via settings ----------------------------------------------------------

def test_backend_selection_defaults_to_memory() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="memory")
    store = _build_replay_blob_store(settings)
    assert isinstance(store, InMemoryReplayBlobStore)


def test_backend_selection_picks_gcs(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.app import _build_replay_blob_store

    # Avoid needing the real client: swap the class so construction doesn't touch google-cloud.
    captured: dict[str, str] = {}

    class _StubGCS:
        def __init__(self, bucket: str) -> None:
            captured["bucket"] = bucket

    monkeypatch.setattr("gateway.app.GCSReplayBlobStore", _StubGCS)
    settings = Settings(replay_blob_backend="gcs", replay_gcs_bucket="tally-replay-prod")
    store = _build_replay_blob_store(settings)
    assert isinstance(store, _StubGCS)
    assert captured["bucket"] == "tally-replay-prod"


def test_gcs_backend_requires_bucket() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="gcs", replay_gcs_bucket="")
    with pytest.raises(ValueError):
        _build_replay_blob_store(settings)


def test_unknown_backend_rejected() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="s4")
    with pytest.raises(ValueError):
        _build_replay_blob_store(settings)


# --- Lazy import -----------------------------------------------------------------------------

def test_replay_store_imports_without_google_cloud_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing replay_store must not require google-cloud-storage; only constructing the store
    (without an injected client) does. Simulate the package being absent and re-import."""
    # Block any google.cloud.storage import.
    for name in list(sys.modules):
        if name == "google" or name.startswith("google."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = importlib.import_module

    def _blocking_import(name: str, *args, **kwargs):
        if name == "google.cloud.storage" or name.startswith("google.cloud"):
            raise ModuleNotFoundError("No module named 'google'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _blocking_import)

    # Module (re)import works fine even with google-cloud-storage unavailable.
    mod = importlib.reload(importlib.import_module("gateway.replay_store"))
    assert hasattr(mod, "GCSReplayBlobStore")

    # And an injected client sidesteps the lazy import entirely, so the store is still usable.
    store = mod.GCSReplayBlobStore(bucket="b", client=_FakeClient())
    store.put_bytes("k", b"v")
    assert store.get_bytes("k") == b"v"
