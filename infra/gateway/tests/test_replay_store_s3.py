"""Unit tests for the S3 replay blob-store backend (CTO-158).

These use an in-memory fake that mimics the ``boto3`` S3 client surface we touch
(``client.put_object`` / ``client.get_object`` / ``client.head_object``), so no network access,
real bucket, or credentials are required and ``boto3`` need not be installed to run the suite.
"""

from __future__ import annotations

import importlib
import io
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gateway.config import Settings
from gateway.replay_sampler import ReplaySamplePayload
from gateway.replay_store import (
    InMemoryReplayBlobStore,
    S3ReplayBlobStore,
    build_replay_object_key,
    persist_sample,
)

UTC = timezone.utc


# --- Fake boto3 S3 client --------------------------------------------------------------------

class _FakeClientError(Exception):
    """Stand-in for ``botocore.exceptions.ClientError`` — carries a ``response`` dict."""

    def __init__(self, code: str, status: int = 404) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _FakeS3Client:
    """Backed by one dict per client instance. Records puts/heads for assertions."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.buckets_used: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.buckets_used.append(Bucket)
        self.objects[Key] = Body
        self.content_types[Key] = ContentType

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.buckets_used.append(Bucket)
        if Key not in self.objects:
            raise _FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.buckets_used.append(Bucket)
        if Key not in self.objects:
            raise _FakeClientError("404")
        return {"ContentLength": len(self.objects[Key])}


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

def test_persisted_sample_round_trips_through_s3() -> None:
    client = _FakeS3Client()
    store = S3ReplayBlobStore(bucket="tally-replay", client=client)
    payload = _payload()
    captured_at = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)

    row = persist_sample(
        blob_store=store,
        tenant_id="tenant-1",
        payload=payload,
        captured_at=captured_at,
    )

    # Keyed identically to the in-memory/GCS path.
    expected_key = build_replay_object_key("tenant-1", payload.sample_id, captured_at)
    assert row.s3_object_key == expected_key
    # Write-then-read returns the same envelope bytes.
    assert store.get_bytes(expected_key) == payload.scrubbed_json


def test_s3_key_matches_in_memory_backend() -> None:
    client = _FakeS3Client()
    s3 = S3ReplayBlobStore(bucket="b", client=client)
    mem = InMemoryReplayBlobStore()
    payload = _payload()
    captured_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    s3_row = persist_sample(blob_store=s3, tenant_id="t", payload=payload, captured_at=captured_at)
    mem_row = persist_sample(
        blob_store=mem, tenant_id="t", payload=payload, captured_at=captured_at
    )
    assert s3_row.s3_object_key == mem_row.s3_object_key
    assert s3.get_bytes(s3_row.s3_object_key) == mem.get_bytes(mem_row.s3_object_key)


def test_content_type_passed_and_bucket_used() -> None:
    client = _FakeS3Client()
    store = S3ReplayBlobStore(bucket="my-bucket", client=client)
    store.put_bytes("tenants/t/x.json", b"{}", content_type="application/json")
    assert client.objects["tenants/t/x.json"] == b"{}"
    assert client.content_types["tenants/t/x.json"] == "application/json"
    assert "my-bucket" in client.buckets_used


# --- exists() --------------------------------------------------------------------------------

def test_exists_true_after_put_and_false_when_absent() -> None:
    client = _FakeS3Client()
    store = S3ReplayBlobStore(bucket="b", client=client)
    assert store.exists("tenants/t/missing.json") is False
    store.put_bytes("tenants/t/present.json", b"{}")
    assert store.exists("tenants/t/present.json") is True


def test_exists_reraises_non_404_errors() -> None:
    class _BoomClient(_FakeS3Client):
        def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            raise _FakeClientError("AccessDenied", status=403)

    store = S3ReplayBlobStore(bucket="b", client=_BoomClient())
    with pytest.raises(_FakeClientError):
        store.exists("tenants/t/x.json")


# --- Prefix ----------------------------------------------------------------------------------

def test_prefix_is_applied_to_stored_key_but_not_index_key() -> None:
    client = _FakeS3Client()
    store = S3ReplayBlobStore(bucket="b", prefix="env/staging/", client=client)
    store.put_bytes("tenants/t/x.json", b"{}")
    # Prefix is prepended (normalised, no leading/trailing slash duplication) on the way to S3.
    assert "env/staging/tenants/t/x.json" in client.objects
    assert "tenants/t/x.json" not in client.objects
    # Round-trip through the same prefixed store still resolves.
    assert store.get_bytes("tenants/t/x.json") == b"{}"
    assert store.exists("tenants/t/x.json") is True


def test_empty_bucket_rejected() -> None:
    with pytest.raises(ValueError):
        S3ReplayBlobStore(bucket="", client=_FakeS3Client())


# --- Backend selection via settings ----------------------------------------------------------

def test_backend_selection_defaults_to_memory() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="memory")
    store = _build_replay_blob_store(settings)
    assert isinstance(store, InMemoryReplayBlobStore)


def test_backend_selection_picks_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.app import _build_replay_blob_store

    # Avoid needing the real client: swap the class so construction doesn't touch boto3.
    captured: dict[str, object] = {}

    class _StubS3:
        def __init__(self, bucket: str, *, prefix: str = "", region: str | None = None) -> None:
            captured["bucket"] = bucket
            captured["prefix"] = prefix
            captured["region"] = region

    monkeypatch.setattr("gateway.app.S3ReplayBlobStore", _StubS3)
    settings = Settings(
        replay_blob_backend="s3",
        replay_s3_bucket="tally-replay-prod",
        replay_s3_prefix="env/prod",
        replay_s3_region="us-east-1",
    )
    store = _build_replay_blob_store(settings)
    assert isinstance(store, _StubS3)
    assert captured == {
        "bucket": "tally-replay-prod",
        "prefix": "env/prod",
        "region": "us-east-1",
    }


def test_s3_empty_region_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway.app import _build_replay_blob_store

    captured: dict[str, object] = {}

    class _StubS3:
        def __init__(self, bucket: str, *, prefix: str = "", region: str | None = None) -> None:
            captured["region"] = region

    monkeypatch.setattr("gateway.app.S3ReplayBlobStore", _StubS3)
    settings = Settings(replay_blob_backend="s3", replay_s3_bucket="b")
    _build_replay_blob_store(settings)
    # Empty string region -> None so boto3 resolves from the AWS default chain.
    assert captured["region"] is None


def test_s3_backend_requires_bucket() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="s3", replay_s3_bucket="")
    with pytest.raises(ValueError):
        _build_replay_blob_store(settings)


def test_unknown_backend_rejected() -> None:
    from gateway.app import _build_replay_blob_store

    settings = Settings(replay_blob_backend="s4")
    with pytest.raises(ValueError):
        _build_replay_blob_store(settings)


# --- Lazy import -----------------------------------------------------------------------------

def test_replay_store_imports_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing replay_store must not require boto3; only constructing the store (without an
    injected client) does. Simulate the package being absent and re-import."""
    # Block any boto3 import.
    for name in list(sys.modules):
        if name == "boto3" or name.startswith("boto3."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = importlib.import_module

    def _blocking_import(name: str, *args, **kwargs):
        if name == "boto3" or name.startswith("boto3."):
            raise ModuleNotFoundError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _blocking_import)

    # Module (re)import works fine even with boto3 unavailable.
    mod = importlib.reload(importlib.import_module("gateway.replay_store"))
    assert hasattr(mod, "S3ReplayBlobStore")

    # And an injected client sidesteps the lazy import entirely, so the store is still usable.
    store = mod.S3ReplayBlobStore(bucket="b", client=_FakeS3Client())
    store.put_bytes("k", b"v")
    assert store.get_bytes("k") == b"v"
