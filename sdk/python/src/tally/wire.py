"""Wire envelope + idempotency — the SDK↔ingest batch contract (transport-agnostic).

Implements CTO-32.

Defines the :class:`BatchRequest` / :class:`BatchResponse` shapes and a JSON codec. The protobuf
wire format is a later infra ticket; this pure-Python layer lets us build and test the envelope,
idempotency, and dedup semantics now.

Idempotency: a batch carries a client-generated ``batch_id`` (UUIDv7, time-ordered). The gateway
dedupes on ``(tenant_id, batch_id)`` for 24h — a replayed batch returns the original response
without re-processing. Within a batch, spans dedupe on ``(trace_id, span_id)``, business events on
``business_event_id``, identity links on ``(identity_a, identity_b, source)``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum

# --- UUIDv7 (time-ordered) -----------------------------------------------------------------------


def uuid7() -> str:
    """Generate a UUIDv7 (48-bit big-endian ms timestamp + random). Time-ordered hex string.

    Python's stdlib lacks uuid7 before 3.14, so we build one per RFC 9562.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = os.urandom(10)
    b = bytearray(ms.to_bytes(6, "big") + rand)
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(b)))


# --- messages ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sampling:
    head_sample_rate: float = 1.0
    sampling_strategy: str = "deterministic_trace"


@dataclass(frozen=True, slots=True)
class BusinessEvent:
    business_event_id: str
    event_name: str
    user_id_hash: str
    occurred_at_ns: int
    value_amount_micro: int | None = None
    value_currency: str = "USD"
    value_type: str = "monetary"
    source: str = "sdk"


@dataclass(frozen=True, slots=True)
class IdentityLink:
    identity_a: str
    identity_a_type: str
    identity_b: str
    identity_b_type: str
    observed_at_ns: int
    source: str = "sdk"
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class ClientHealth:
    dropped_span_count: int = 0
    context_drop_count: int = 0
    internal_error_count: int = 0
    buffer_high_water: int = 0


@dataclass(slots=True)
class BatchRequest:
    tenant_id: str
    sdk_version: str
    resource_spans: list[dict[str, object]] = field(default_factory=list)
    business_events: list[BusinessEvent] = field(default_factory=list)
    identity_links: list[IdentityLink] = field(default_factory=list)
    sampling: Sampling = field(default_factory=Sampling)
    client_health: ClientHealth = field(default_factory=ClientHealth)
    batch_id: str = field(default_factory=uuid7)
    client_send_ts_ns: int = field(default_factory=lambda: time.time_ns())

    def deduplicated(self) -> BatchRequest:
        """Return a copy with intra-batch duplicates removed (spans, events, identity links)."""
        seen_spans: set[tuple[object, object]] = set()
        spans: list[dict[str, object]] = []
        for s in self.resource_spans:
            key = (s.get("TraceId") or s.get("trace_id"), s.get("SpanId") or s.get("span_id"))
            if key in seen_spans:
                continue
            seen_spans.add(key)
            spans.append(s)

        seen_ev: set[str] = set()
        events: list[BusinessEvent] = []
        for e in self.business_events:
            if e.business_event_id in seen_ev:
                continue
            seen_ev.add(e.business_event_id)
            events.append(e)

        seen_links: set[tuple[str, str, str]] = set()
        links: list[IdentityLink] = []
        for ln in self.identity_links:
            key2 = (ln.identity_a, ln.identity_b, ln.source)
            if key2 in seen_links:
                continue
            seen_links.add(key2)
            links.append(ln)

        return BatchRequest(
            tenant_id=self.tenant_id,
            sdk_version=self.sdk_version,
            resource_spans=spans,
            business_events=events,
            identity_links=links,
            sampling=self.sampling,
            client_health=self.client_health,
            batch_id=self.batch_id,
            client_send_ts_ns=self.client_send_ts_ns,
        )


class Status(str, Enum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class PartialError:
    item_id: str
    code: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class BatchResponse:
    batch_id: str
    status: Status = Status.ACCEPTED
    partial_errors: list[PartialError] = field(default_factory=list)
    accepted_spans: int = 0


# --- JSON codec ----------------------------------------------------------------------------------


def encode_request(req: BatchRequest) -> str:
    payload = {
        "tenant_id": req.tenant_id,
        "sdk_version": req.sdk_version,
        "batch_id": req.batch_id,
        "client_send_ts_ns": req.client_send_ts_ns,
        "sampling": asdict(req.sampling),
        "client_health": asdict(req.client_health),
        "resource_spans": req.resource_spans,
        "business_events": [asdict(e) for e in req.business_events],
        "identity_links": [asdict(x) for x in req.identity_links],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def decode_request(blob: str) -> BatchRequest:
    d = json.loads(blob)
    return BatchRequest(
        tenant_id=d["tenant_id"],
        sdk_version=d["sdk_version"],
        batch_id=d["batch_id"],
        client_send_ts_ns=d["client_send_ts_ns"],
        sampling=Sampling(**d.get("sampling", {})),
        client_health=ClientHealth(**d.get("client_health", {})),
        resource_spans=d.get("resource_spans", []),
        business_events=[BusinessEvent(**e) for e in d.get("business_events", [])],
        identity_links=[IdentityLink(**x) for x in d.get("identity_links", [])],
    )


# --- idempotency ---------------------------------------------------------------------------------


class IdempotencyCache:
    """Server-side dedup keyed on (tenant_id, batch_id) with a TTL (default 24h).

    ``check_or_store`` returns the cached response for a replayed batch, else stores + returns None
    (caller then processes and records the response via :meth:`record`).
    """

    def __init__(self, ttl_seconds: float = 24 * 3600, now: object = None) -> None:
        self.ttl = ttl_seconds
        self._now = now or time.time
        self._store: dict[tuple[str, str], tuple[float, BatchResponse]] = {}

    def _purge(self, t: float) -> None:
        expired = [k for k, (ts, _) in self._store.items() if t - ts > self.ttl]
        for k in expired:
            del self._store[k]

    def check_or_store(self, req: BatchRequest) -> BatchResponse | None:
        t = self._now()
        self._purge(t)
        key = (req.tenant_id, req.batch_id)
        hit = self._store.get(key)
        if hit is not None:
            return hit[1]
        # reserve the slot with a provisional ACCEPTED; updated by record()
        self._store[key] = (t, BatchResponse(batch_id=req.batch_id))
        return None

    def record(self, req: BatchRequest, response: BatchResponse) -> None:
        self._store[(req.tenant_id, req.batch_id)] = (self._now(), response)
