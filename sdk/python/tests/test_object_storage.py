# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.object_storage (CTO-28): S3 addressing, offload decision, retention."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tally.object_storage import (
    DEFAULT_SSE,
    INLINE_THRESHOLD_BYTES,
    InMemoryObjectStore,
    ObjectCategory,
    ObjectRef,
    PayloadOffloader,
    RetentionPolicy,
    build_object_key,
    sha256_hex,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Key scheme
# --------------------------------------------------------------------------- #
def test_build_object_key_layout():
    key = build_object_key(
        "t1",
        ObjectCategory.RESOLVED_CONTEXT,
        content_sha256="abc",
        observed_at=NOW,
        region="eu-west-1",
    )
    assert key == "eu-west-1/tenant=t1/resolved-context/dt=2026-05-01/abc"


def test_build_object_key_rejects_empty_tenant_or_region():
    with pytest.raises(ValueError):
        build_object_key(
            "", ObjectCategory.COLD_SPAN, content_sha256="x", observed_at=NOW, region="r"
        )
    with pytest.raises(ValueError):
        build_object_key(
            "t1", ObjectCategory.COLD_SPAN, content_sha256="x", observed_at=NOW, region=""
        )


def test_build_object_key_coerces_naive_datetime():
    key = build_object_key(
        "t1",
        ObjectCategory.COLD_SPAN,
        content_sha256="z",
        observed_at=datetime(2026, 1, 2),
        region="r",
    )
    assert "dt=2026-01-02" in key


# --------------------------------------------------------------------------- #
# ObjectRef
# --------------------------------------------------------------------------- #
def test_object_ref_uri_and_as_dict():
    ref = ObjectRef("b", "k/x", "us-east-1", ObjectCategory.COLD_SPAN, 10, "sha")
    assert ref.uri == "s3://b/k/x"
    assert ref.sse == DEFAULT_SSE
    d = ref.as_dict()
    assert d["uri"] == "s3://b/k/x"
    assert d["category"] == "cold-span"


def test_object_ref_validation():
    with pytest.raises(ValueError):
        ObjectRef("", "k", "r", ObjectCategory.COLD_SPAN, 1, "s")
    with pytest.raises(ValueError):
        ObjectRef("b", "k", "r", ObjectCategory.COLD_SPAN, -1, "s")
    with pytest.raises(ValueError):
        ObjectRef("b", "k", "r", ObjectCategory.COLD_SPAN, True, "s")  # bool not int
    with pytest.raises(ValueError):
        ObjectRef("b", "k", "r", ObjectCategory.COLD_SPAN, 1, "sha", sse="")  # SSE mandatory


# --------------------------------------------------------------------------- #
# InMemoryObjectStore round-trip
# --------------------------------------------------------------------------- #
def test_store_put_get_roundtrip():
    store = InMemoryObjectStore(region="eu-central-1")
    payload = b"hello world"
    ref = store.put("t1", ObjectCategory.RAW_CDP_PAYLOAD, payload, observed_at=NOW)
    assert ref.region == "eu-central-1"
    assert ref.size_bytes == len(payload)
    assert ref.content_sha256 == sha256_hex(payload)
    assert store.get(ref) == payload
    assert ref.uri.startswith("s3://tally-objects/eu-central-1/tenant=t1/")


def test_store_is_content_addressed_idempotent():
    store = InMemoryObjectStore()
    r1 = store.put("t1", ObjectCategory.COLD_SPAN, b"same", observed_at=NOW)
    r2 = store.put("t1", ObjectCategory.COLD_SPAN, b"same", observed_at=NOW)
    assert r1.key == r2.key
    assert store.object_count() == 1


def test_store_delete():
    store = InMemoryObjectStore()
    ref = store.put("t1", ObjectCategory.COLD_SPAN, b"x", observed_at=NOW)
    assert store.delete(ref) is True
    assert store.delete(ref) is False
    with pytest.raises(KeyError):
        store.get(ref)


def test_store_refuses_without_sse():
    store = InMemoryObjectStore(sse="")
    with pytest.raises(ValueError):
        store.put("t1", ObjectCategory.COLD_SPAN, b"x", observed_at=NOW)


# --------------------------------------------------------------------------- #
# Inline-vs-offload decision
# --------------------------------------------------------------------------- #
def test_small_payload_stays_inline():
    off = PayloadOffloader(InMemoryObjectStore())
    placement = off.place("t1", ObjectCategory.RESOLVED_CONTEXT, b"tiny", observed_at=NOW)
    assert placement.is_offloaded is False
    assert placement.pointer() == ""
    assert off.resolve(placement) == b"tiny"
    assert off.store.object_count() == 0


def test_large_payload_offloaded_and_resolves():
    store = InMemoryObjectStore()
    off = PayloadOffloader(store)
    big = b"x" * (INLINE_THRESHOLD_BYTES + 1)
    placement = off.place("t1", ObjectCategory.RESOLVED_CONTEXT, big, observed_at=NOW)
    assert placement.is_offloaded is True
    assert placement.pointer().startswith("s3://")
    assert off.resolve(placement) == big
    assert store.object_count() == 1


def test_threshold_boundary_is_inclusive_inline():
    off = PayloadOffloader(InMemoryObjectStore())
    exactly = b"x" * INLINE_THRESHOLD_BYTES
    placement = off.place("t1", ObjectCategory.RESOLVED_CONTEXT, exactly, observed_at=NOW)
    assert placement.is_offloaded is False  # <= threshold stays inline


def test_place_accepts_str_payload():
    store = InMemoryObjectStore()
    off = PayloadOffloader(store, threshold_bytes=4)
    placement = off.place("t1", ObjectCategory.RAW_CDP_PAYLOAD, "hello", observed_at=NOW)
    assert placement.is_offloaded is True
    assert off.resolve(placement) == b"hello"


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_retention_expires_at_per_category():
    policy = RetentionPolicy()
    ref = ObjectRef("b", "k", "r", ObjectCategory.RESOLVED_CONTEXT, 1, "s")
    written = datetime(2026, 1, 1, tzinfo=UTC)
    assert policy.days_for(ObjectCategory.RESOLVED_CONTEXT) == 30
    assert policy.expires_at(ref, written) == datetime(2026, 1, 31, tzinfo=UTC)


def test_retention_is_expired():
    policy = RetentionPolicy()
    ref = ObjectRef("b", "k", "r", ObjectCategory.RAW_CDP_PAYLOAD, 1, "s")
    written = datetime(2026, 1, 1, tzinfo=UTC)
    assert policy.is_expired(ref, written, datetime(2026, 1, 15, tzinfo=UTC)) is False
    assert policy.is_expired(ref, written, datetime(2026, 2, 1, tzinfo=UTC)) is True


def test_retention_indefinite_when_none():
    policy = RetentionPolicy(days_by_category={ObjectCategory.COLD_SPAN: None})
    ref = ObjectRef("b", "k", "r", ObjectCategory.COLD_SPAN, 1, "s")
    written = datetime(2026, 1, 1, tzinfo=UTC)
    assert policy.expires_at(ref, written) is None
    assert policy.is_expired(ref, written, datetime(2030, 1, 1, tzinfo=UTC)) is False


def test_retention_is_configurable():
    policy = RetentionPolicy(days_by_category={ObjectCategory.RESOLVED_CONTEXT: 7})
    ref = ObjectRef("b", "k", "r", ObjectCategory.RESOLVED_CONTEXT, 1, "s")
    written = datetime(2026, 1, 1, tzinfo=UTC)
    assert policy.expires_at(ref, written) == datetime(2026, 1, 8, tzinfo=UTC)
