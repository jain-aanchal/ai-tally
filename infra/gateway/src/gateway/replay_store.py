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

# CTO-241: ReplayBodyMissing is defined in gateway.replay_errors (a module nothing reloads) so its
# class identity stays stable across an importlib.reload of this module. The optional-dependency
# tests reload gateway.replay_store; if the exception were defined here, a reload would rebind it to
# a fresh class the executor's ``except`` clause would stop catching. Re-exported so callers keep
# importing it from gateway.replay_store alongside the stores.
from gateway.replay_errors import ReplayBodyMissing

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
        # CTO-241: a bare ``self._objects[key]`` raised a plain KeyError that bubbled out of
        # /v1/replay as a 500 after a restart wiped this in-process dict. Translate the miss into
        # the typed, non-fatal ReplayBodyMissing so the executor can skip the sample instead.
        try:
            return self._objects[key]
        except KeyError:
            raise ReplayBodyMissing(key) from None

    def __len__(self) -> int:
        return len(self._objects)


class GCSReplayBlobStore:
    """Google Cloud Storage backend for replay sample blobs (CTO-152).

    Satisfies the same :class:`ReplayBlobStore` protocol as :class:`InMemoryReplayBlobStore`
    (and the S3/MinIO path): objects are keyed with the identical
    ``tenants/{tenant_id}/replay_samples/{yyyy/mm/dd}/{sample_id}.json`` layout produced by
    :func:`build_replay_object_key`, so the ClickHouse index rows and the executor read path are
    unchanged regardless of which backend is wired.

    **PII scrub** — the pre-storage PII scrub from CTO-113 runs upstream in the sampler /
    ``persist_sample`` ingest path, before any ``put_bytes`` call. This backend only moves already
    scrubbed bytes, so it inherits the scrub unchanged; the CTO-125 candidate-response retention
    carve-out likewise applies identically here.

    **Auth** — no raw key material is passed. The ``google-cloud-storage`` client is constructed
    with Application Default Credentials (ADC), so it transparently picks up Workload Identity in
    GKE / GCE metadata credentials / a ``GOOGLE_APPLICATION_CREDENTIALS`` file / ``gcloud`` user
    creds, in that resolution order.

    **Retention** — object lifecycle (TTL / deletion) is expected to be enforced by a GCS bucket
    lifecycle policy provisioned out-of-band (e.g. Terraform), mirroring the replay
    ``retention_days`` knob. This backend does NOT create or manage lifecycle rules (out of scope
    for CTO-152).

    The ``google-cloud-storage`` dependency is OPTIONAL (``[gcs]`` extra) and imported lazily at
    construction — importing this module never requires the package; only instantiating this class
    does. That keeps the base gateway install slim and lets it boot without GCS when unused.
    """

    __slots__ = ("_bucket_name", "_bucket", "_client")

    def __init__(self, bucket: str, *, client: object | None = None) -> None:
        if not bucket:
            raise ValueError("bucket must be non-empty")
        if client is None:
            # Lazy import: keep google-cloud-storage optional. Constructing the client with no
            # explicit credentials makes it resolve Application Default Credentials (ADC) /
            # Workload Identity automatically — we never handle a raw key here.
            from google.cloud import storage  # type: ignore[import-not-found]

            client = storage.Client()
        self._client = client
        self._bucket_name = bucket
        self._bucket = client.bucket(bucket)

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(body, content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        # CTO-241: mirror the in-memory/S3 path — a missing blob is the typed, non-fatal
        # ReplayBodyMissing, not a raw google-cloud NotFound that would 500 /v1/replay.
        try:
            return self._bucket.blob(key).download_as_bytes()
        except Exception as exc:  # noqa: BLE001 — normalise google-cloud NotFound (404) to a miss.
            if _is_gcs_not_found(exc):
                raise ReplayBodyMissing(key) from exc
            raise


class S3ReplayBlobStore:
    """AWS S3 (or S3-compatible, e.g. MinIO) backend for replay sample blobs (CTO-158).

    Mirror of :class:`GCSReplayBlobStore` — satisfies the same :class:`ReplayBlobStore` protocol
    as :class:`InMemoryReplayBlobStore` and the GCS path: objects are keyed with the identical
    ``tenants/{tenant_id}/replay_samples/{yyyy/mm/dd}/{sample_id}.json`` layout produced by
    :func:`build_replay_object_key`, so the ClickHouse index rows and the executor read path are
    unchanged regardless of which backend is wired. An optional ``prefix`` is prepended to every
    key (e.g. to share a bucket across environments) without touching the stored index key — the
    prefix is a storage-layout detail applied on the way in/out only.

    **PII scrub** — the pre-storage PII scrub from CTO-113 runs upstream in the sampler /
    ``persist_sample`` ingest path, before any ``put_bytes`` call. This backend only moves already
    scrubbed bytes, so it inherits the scrub unchanged; the CTO-125 candidate-response retention
    carve-out likewise applies identically here.

    **Auth** — no raw key material is passed. The ``boto3`` S3 client is constructed with the AWS
    default credential chain, so it transparently picks up an IAM role / IRSA (EKS) / EC2 instance
    profile / ``AWS_*`` environment credentials / a shared credentials file, in boto3's normal
    resolution order. We never handle an access key or secret here.

    **Retention** — object lifecycle (TTL / expiration) is expected to be enforced by an S3 bucket
    lifecycle policy provisioned out-of-band (e.g. Terraform), mirroring the replay
    ``retention_days`` knob. This backend does NOT create or manage lifecycle rules (out of scope
    for CTO-158).

    The ``boto3`` dependency is OPTIONAL (``[s3]`` extra) and imported lazily at construction —
    importing this module never requires the package; only instantiating this class does. That
    keeps the base gateway install slim and lets it boot without boto3 when unused.
    """

    __slots__ = ("_bucket", "_prefix", "_client")

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: object | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket must be non-empty")
        if client is None:
            # Lazy import: keep boto3 optional. Constructing the client with no explicit
            # credentials makes it resolve the AWS default credential chain (IAM role / IRSA /
            # instance profile / env / shared file) automatically — we never handle a raw key here.
            import boto3  # type: ignore[import-not-found]

            # CTO-241: ``endpoint_url`` points the client at an S3-compatible service (MinIO in the
            # local docker-compose stack) so replay bodies survive a gateway restart instead of
            # living only in an in-process dict. Left None for real AWS, boto3 resolves the regional
            # AWS endpoint itself. boto3 also honours AWS_ENDPOINT_URL_S3 from the environment when
            # this arg is None, so the compose env can set it either way.
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url or None)
        self._client = client
        self._bucket = bucket
        # Normalise prefix to a bare, slash-terminated segment (or empty) so key joins are clean.
        self._prefix = prefix.strip("/")

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._full_key(key),
            Body=body,
            ContentType=content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        # CTO-241: a body that was indexed but never landed (or aged out of the bucket) must be a
        # typed, non-fatal miss like the in-memory path, not a raw botocore 404 that 500s /v1/replay.
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
        except Exception as exc:  # noqa: BLE001 — normalise botocore ClientError (404/NoSuchKey).
            if _is_s3_not_found(exc):
                raise ReplayBodyMissing(key) from exc
            raise
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        """True if an object lives at ``key``. Uses ``head_object``; a 404/NoSuchKey means absent."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._full_key(key))
        except Exception as exc:  # noqa: BLE001 — normalise botocore ClientError (404) to False.
            if _is_s3_not_found(exc):
                return False
            raise
        return True


def _is_s3_not_found(exc: BaseException) -> bool:
    """Return True for a boto3/botocore 'object not found' error (404 / NoSuchKey / NotFound).

    Kept structural (duck-typed on ``response``) so tests can raise a lightweight stand-in and so
    the module never needs botocore imported to interpret the error."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            return True
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404:
            return True
    return False


def _is_gcs_not_found(exc: BaseException) -> bool:
    """Return True for a google-cloud 'blob not found' error (HTTP 404 / NotFound).

    Kept structural (duck-typed on ``code`` / class name) so the module never needs
    ``google-cloud-storage`` imported to interpret the error and tests can raise a stand-in."""
    if getattr(exc, "code", None) == 404:
        return True
    return type(exc).__name__ == "NotFound"


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
