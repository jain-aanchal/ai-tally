# SPDX-License-Identifier: Apache-2.0
"""Object storage (S3) addressing + offload policy (CTO-28 / spec §5.3).

Large or auxiliary blobs don't belong inline in ClickHouse — they bloat the hot store and slow
every scan. Three categories live in S3 instead, referenced from a span by a small pointer string:

* **resolved-context** — the fully-resolved prompt/context body for replay, offloaded when it
  exceeds the inline threshold (stored in ``otel_spans.ResolvedContextRef``).
* **cold-span** — archived raw spans past the cold tier (CTO-29 drops them from ClickHouse).
* **raw-cdp-payload** — the original webhook body behind a business event (CTO-68), kept for audit.

This module owns the **addressing scheme** (per-tenant + per-region prefixing so a bucket is never
shared across isolation boundaries), the **inline-vs-offload decision** (>64 KiB → S3), the
**pointer model** (``ObjectRef``), and a per-category **retention policy**. Server-side encryption
is mandatory: the store refuses to construct a ref without an SSE mode. The actual AWS client and
bucket provisioning are infra; ``InMemoryObjectStore`` gives dev/test a real round-trip with no S3.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

UTC = timezone.utc

# Payloads larger than this don't belong inline in ClickHouse; offload to S3 and keep a pointer.
INLINE_THRESHOLD_BYTES = 64 * 1024  # 64 KiB (spec §5.3)

# Default server-side encryption mode. KMS-managed keys per spec §14 (never plaintext at rest).
DEFAULT_SSE = "aws:kms"


class ObjectCategory(str, Enum):
    RESOLVED_CONTEXT = "resolved-context"
    COLD_SPAN = "cold-span"
    RAW_CDP_PAYLOAD = "raw-cdp-payload"


# Configurable retention per category (days). None = retain indefinitely.
DEFAULT_RETENTION_DAYS: Mapping[ObjectCategory, int | None] = {
    ObjectCategory.RESOLVED_CONTEXT: 30,
    ObjectCategory.COLD_SPAN: 90,
    ObjectCategory.RAW_CDP_PAYLOAD: 30,
}


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_object_key(
    tenant_id: str,
    category: ObjectCategory,
    *,
    content_sha256: str,
    observed_at: datetime,
    region: str,
) -> str:
    """Per-region, per-tenant, content-addressed key.

    ``{region}/tenant={tenant}/{category}/dt=YYYY-MM-DD/{sha256}`` — region first so a bucket maps
    to one region (data-residency), tenant prefix for isolation, date partition for lifecycle
    rules, content hash for dedup (identical bodies collapse to one object).
    """
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    if not region:
        raise ValueError("region must be non-empty")
    day = _as_utc(observed_at).strftime("%Y-%m-%d")
    return f"{region}/tenant={tenant_id}/{category.value}/dt={day}/{content_sha256}"


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A pointer to an S3 object — this is what gets stored inline in ClickHouse, not the blob."""

    bucket: str
    key: str
    region: str
    category: ObjectCategory
    size_bytes: int
    content_sha256: str
    content_type: str = "application/octet-stream"
    sse: str = DEFAULT_SSE

    def __post_init__(self) -> None:
        if not self.bucket or not self.key or not self.region:
            raise ValueError("bucket, key, and region must be non-empty")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("size_bytes must be an int")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if not self.sse:
            raise ValueError("sse (server-side encryption) is mandatory")

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "region": self.region,
            "category": self.category.value,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
            "content_type": self.content_type,
            "sse": self.sse,
            "uri": self.uri,
        }


@runtime_checkable
class ObjectStore(Protocol):
    """Write/read/delete blobs, returning a pointer. The ingest path stores only the pointer."""

    def put(
        self,
        tenant_id: str,
        category: ObjectCategory,
        payload: bytes,
        *,
        observed_at: datetime | None = None,
        content_type: str = "application/octet-stream",
    ) -> ObjectRef: ...

    def get(self, ref: ObjectRef) -> bytes: ...

    def delete(self, ref: ObjectRef) -> bool: ...


@dataclass(slots=True)
class InMemoryObjectStore:
    """In-memory ObjectStore for dev/test — real put/get/delete round-trip, no S3 dependency.

    Content-addressed and idempotent: writing the same bytes twice yields the same key and object.
    """

    bucket: str = "tally-objects"
    region: str = "us-east-1"
    sse: str = DEFAULT_SSE
    _objects: dict[str, bytes] = field(default_factory=dict)

    def put(
        self,
        tenant_id: str,
        category: ObjectCategory,
        payload: bytes,
        *,
        observed_at: datetime | None = None,
        content_type: str = "application/octet-stream",
    ) -> ObjectRef:
        if not self.sse:
            raise ValueError("server-side encryption must be configured before writing")
        digest = sha256_hex(payload)
        key = build_object_key(
            tenant_id,
            category,
            content_sha256=digest,
            observed_at=observed_at or datetime.now(UTC),
            region=self.region,
        )
        self._objects[key] = payload
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            region=self.region,
            category=category,
            size_bytes=len(payload),
            content_sha256=digest,
            content_type=content_type,
            sse=self.sse,
        )

    def get(self, ref: ObjectRef) -> bytes:
        return self._objects[ref.key]

    def delete(self, ref: ObjectRef) -> bool:
        return self._objects.pop(ref.key, None) is not None

    def object_count(self) -> int:
        return len(self._objects)


@dataclass(frozen=True, slots=True)
class PayloadPlacement:
    """Result of the inline-vs-offload decision: exactly one of ``inline``/``ref`` is set."""

    inline: bytes | None
    ref: ObjectRef | None

    @property
    def is_offloaded(self) -> bool:
        return self.ref is not None

    def pointer(self) -> str:
        """The string stored in the span column: the S3 URI when offloaded, else empty."""
        return self.ref.uri if self.ref is not None else ""


@dataclass(slots=True)
class PayloadOffloader:
    """Decides whether a payload stays inline or goes to S3, and resolves it back on read."""

    store: ObjectStore
    threshold_bytes: int = INLINE_THRESHOLD_BYTES

    def place(
        self,
        tenant_id: str,
        category: ObjectCategory,
        payload: bytes | str,
        *,
        observed_at: datetime | None = None,
        content_type: str = "application/octet-stream",
    ) -> PayloadPlacement:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(raw) <= self.threshold_bytes:
            return PayloadPlacement(inline=raw, ref=None)
        ref = self.store.put(
            tenant_id, category, raw, observed_at=observed_at, content_type=content_type
        )
        return PayloadPlacement(inline=None, ref=ref)

    def resolve(self, placement: PayloadPlacement) -> bytes:
        """Round-trip read: inline bytes if small, otherwise fetch from the store."""
        if placement.ref is not None:
            return self.store.get(placement.ref)
        return placement.inline or b""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Per-category retention (days); None means retain indefinitely."""

    days_by_category: Mapping[ObjectCategory, int | None] = field(
        default_factory=lambda: dict(DEFAULT_RETENTION_DAYS)
    )

    def days_for(self, category: ObjectCategory) -> int | None:
        return self.days_by_category.get(category)

    def expires_at(self, ref: ObjectRef, written_at: datetime) -> datetime | None:
        """When the object should be lifecycle-deleted, or None if retained indefinitely."""
        days = self.days_for(ref.category)
        if days is None:
            return None
        return _as_utc(written_at) + timedelta(days=days)

    def is_expired(self, ref: ObjectRef, written_at: datetime, as_of: datetime) -> bool:
        deadline = self.expires_at(ref, written_at)
        if deadline is None:
            return False
        return _as_utc(as_of) >= deadline


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
