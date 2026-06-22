"""ClickHouse + object-store glue for replay samples and runs (CTO-113).

Two responsibilities:

* Write scrubbed sample payloads to object storage (S3 / MinIO in prod, in-memory in dev/test)
  with the deterministic key format
  ``tenants/{tenant_id}/replay_samples/{yyyy/mm/dd}/{sample_id}.json``, then insert an index row
  into ClickHouse ``replay_samples``.
* Query samples back out (for the executor) and record run outcomes in ``replay_runs``.

We reuse the SDK's :class:`tally.object_storage.InMemoryObjectStore` for tests and the structural
contract; a real S3/MinIO client gets dropped in here behind the same Protocol when infra lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from gateway.replay_sampler import ReplaySamplePayload

UTC = timezone.utc


def build_replay_object_key(tenant_id: str, sample_id: UUID, captured_at: datetime) -> str:
    """``tenants/{tenant_id}/replay_samples/{yyyy/mm/dd}/{sample_id}.json`` — deterministic, sortable."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    d = captured_at.astimezone(UTC)
    return (
        f"tenants/{tenant_id}/replay_samples/"
        f"{d.year:04d}/{d.month:02d}/{d.day:02d}/{sample_id}.json"
    )


class ReplayBlobStore(Protocol):
    """Minimal contract — put/get raw bytes at a key. Decoupled from the SDK's ObjectRef shape so
    we can plug in MinIO, S3, or a tmp-dir fake without converting category enums."""

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/json") -> None: ...

    def get_bytes(self, key: str) -> bytes: ...


@dataclass(slots=True)
class InMemoryReplayBlobStore:
    """Dict-backed blob store — used by tests and by the local dev gateway."""

    _objects: dict[str, bytes]

    def __init__(self) -> None:
        self._objects = {}

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        self._objects[key] = body

    def get_bytes(self, key: str) -> bytes:
        return self._objects[key]

    def __len__(self) -> int:
        return len(self._objects)


# --- ClickHouse-side index rows --------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReplaySampleRow:
    tenant_id: str
    sample_id: UUID
    trace_id: str
    feature_tag: str
    real_provider: str
    real_model: str
    input_tokens: int
    output_tokens: int
    captured_at: datetime
    s3_object_key: str
    pii_scrubbed: bool
    context_fidelity: str = "resolved-context"

    def as_clickhouse_row(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            str(self.sample_id),
            self.trace_id,
            self.feature_tag,
            self.real_provider,
            self.real_model,
            self.input_tokens,
            self.output_tokens,
            self.captured_at,
            self.s3_object_key,
            1 if self.pii_scrubbed else 0,
            self.context_fidelity,
        )


REPLAY_SAMPLE_COLS = (
    "TenantId", "SampleId", "TraceId", "FeatureTag", "RealProvider", "RealModel",
    "InputTokens", "OutputTokens", "CapturedAt", "S3ObjectKey", "PIIScrubbed",
    "ContextFidelity",
)

REPLAY_RUN_COLS = (
    "TenantId", "RunId", "SampleId", "CandidateProvider", "CandidateModel",
    "InputTokens", "OutputTokens", "CostMicroUsd", "LatencyMs", "ErrorMsg",
    "RanAt", "ContextFidelity", "ResponseText", "FinishReason",
)


@dataclass(frozen=True, slots=True)
class ReplayRunRow:
    tenant_id: str
    run_id: UUID
    sample_id: UUID
    candidate_provider: str
    candidate_model: str
    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    latency_ms: int
    error_msg: str
    ran_at: datetime
    context_fidelity: str = "resolved-context"
    # --- Candidate response body (CTO-125) ---------------------------------------------------
    # PII CARVE-OUT: ``response_text`` is the verbatim text the candidate model produced. This is
    # a message body — exactly the kind of payload the span-side PII guard (mapping.py
    # ``_is_body_key``) refuses to persist into telemetry. Replay is a *separate, opt-in* path:
    # a tenant must explicitly enable ``tenant_replay_config`` (default OFF), the body lives in
    # the replay store under its own retention TTL (``retention_days``) and access tier, and it is
    # never written to spans/business_events. We persist it here so the pairwise LLM judge grades
    # the candidate's ACTUAL output rather than an envelope re-render. The span-side "counts only,
    # never bodies" invariant is untouched by this field.
    response_text: str = ""
    finish_reason: str = ""

    def as_clickhouse_row(self) -> tuple[object, ...]:
        return (
            self.tenant_id,
            str(self.run_id),
            str(self.sample_id),
            self.candidate_provider,
            self.candidate_model,
            self.input_tokens,
            self.output_tokens,
            self.cost_micro_usd,
            self.latency_ms,
            self.error_msg,
            self.ran_at,
            self.context_fidelity,
            # Candidate response body — see PII carve-out note on the dataclass above.
            self.response_text,
            self.finish_reason,
        )


def persist_sample(
    *,
    blob_store: ReplayBlobStore,
    tenant_id: str,
    payload: ReplaySamplePayload,
    captured_at: datetime,
) -> ReplaySampleRow:
    """Write the scrubbed JSON to object storage and return the matching index row."""
    key = build_replay_object_key(tenant_id, payload.sample_id, captured_at)
    blob_store.put_bytes(key, payload.scrubbed_json, content_type="application/json")
    return ReplaySampleRow(
        tenant_id=tenant_id,
        sample_id=payload.sample_id,
        trace_id=payload.trace_id,
        feature_tag=payload.feature_tag,
        real_provider=payload.real_provider,
        real_model=payload.real_model,
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        captured_at=captured_at,
        s3_object_key=key,
        pii_scrubbed=payload.pii_scrubbed,
    )
