# SPDX-License-Identifier: Apache-2.0
"""Durable (tenant_id, batch_id) ingest idempotency (CTO-245).

WHY. :class:`tally.wire.IdempotencyCache` is a dict inside one process. The record of a batch died
with the worker, so a client retrying across a gateway restart, deploy, crash or scale-out was
accepted twice and its spans written twice. ``otel_spans`` had no dedup either, so the second copy
stayed forever and inflated every cost sum by exactly the replayed spend. See
``db/postgres/0032_ingest_batch_idempotency.sql`` for the full rationale and the state machine.

THE SHAPE. :class:`BatchIdempotency` is what the ingest path talks to. It keeps the in-process cache
as a fast path in front of a durable Postgres store, but the DURABLE LAYER IS THE SOURCE OF TRUTH:
the cache can only answer "yes, replayed" from a record this process itself created, and a cache
miss always falls through to Postgres. That ordering is what makes the fast path safe. Getting it
backwards (durable first, cache as a fallback) would be pointless, and letting a cache miss mean
"new" is the bug being fixed.

WHAT HAPPENS WHEN POSTGRES IS UNREACHABLE, and why. The claim raises
:class:`IdempotencyStoreUnavailable` and the gateway answers 503 ``RETRY``. It does NOT accept the
batch. That is deliberate and it is the honest choice of the two available:

  * Accepting on a failed check silently re-admits the exact duplicate this module exists to
    prevent, and the damage is invisible: nothing in the data says which dollars were counted twice,
    so no later query can undo it. Wrong money that looks like right money is the worst outcome
    this codebase has.
  * Refusing is visible and recoverable. The client holds the batch, honors ``retry_after_ms`` and
    resends; the SDK egress loop already treats a retryable response that way, so no telemetry is
    lost, only delayed. The gateway is loudly broken instead of quietly wrong.

Recording an outcome is the mirror image and fails the other way: once the spans are written, a
failure to persist the receipt must not turn an accepted batch into an error the client will retry
(that retry is what would duplicate). So :meth:`record` logs and swallows. The consequence, stated
plainly: that batch's claim stays ``in_flight`` until its lease expires, and a retry inside the
lease is answered retryable rather than with the original response. Delayed, never doubled.

STARTUP PROBE. The durable layer is enabled only if the table is reachable when the gateway boots
(see :func:`build_batch_idempotency`). A gateway that cannot see Postgres at boot runs with the
in-process cache alone, which is the pre-CTO-245 behaviour, and says so at WARNING level rather than
implying a guarantee it is not providing. Any deployment that needs the guarantee must set
``TALLY_IDEMPOTENCY_DURABLE_REQUIRED=true``, which turns that degradation into a refusal to start.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

import psycopg

from tally.wire import BatchRequest, BatchResponse, IdempotencyCache, PartialError, ServerHints, Status

from gateway.config import Settings

logger = logging.getLogger("tally.gateway.batch_idempotency")

#: How long a claim may sit ``in_flight`` before another worker may reclaim it, in seconds.
#:
#: This bounds how long a worker that died between claiming a batch and recording its outcome can
#: block a legitimate retry of that batch. Five minutes is comfortably above the slowest realistic
#: ingest request (validation, enrichment and a ClickHouse insert of at most ``max_batch_size``
#: spans, all of which are sub-second in practice) and comfortably below any human's patience for a
#: stuck client. It is NOT the idempotency window: that is ``Settings.idempotency_ttl_s`` and it
#: governs the ``complete`` records.
IN_FLIGHT_LEASE_S = 300


class IdempotencyStoreUnavailable(RuntimeError):
    """The durable store could not answer. The caller MUST refuse the batch, never accept it.

    Raised only from the claim path. See the module docstring for why refusing beats accepting.
    """


class DurableIdempotencyStore(Protocol):
    """What the ingest path needs from a durable store, so tests can drive it without Postgres."""

    def claim(self, tenant_id: str, batch_id: str, ttl_seconds: float) -> BatchResponse | None: ...

    def record(self, tenant_id: str, batch_id: str, response: BatchResponse) -> None: ...


class PostgresIdempotencyStore:
    """Postgres-backed claim/record over ``ingest_batch_idempotency`` (migration 0031).

    Connection per call and tenant-scoped SQL, matching
    :class:`gateway.ingest_cursors.IngestCursorStore` and every other control-plane store here.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def claim(self, tenant_id: str, batch_id: str, ttl_seconds: float) -> BatchResponse | None:
        """Try to take ownership of ``(tenant_id, batch_id)``.

        Returns ``None`` when this caller won the claim and must process the batch. Returns a
        :class:`BatchResponse` when it did not: either the stored outcome of the first attempt
        (status as recorded, a true replay) or a retryable response when another worker holds the
        batch and the outcome is not known yet.

        The claim is ONE statement, which is what makes the concurrent case correct. Two workers
        racing the same batch_id both run this insert; the primary key means exactly one gets a
        ``RETURNING`` row and therefore exactly one processes the batch.

        Raises :class:`IdempotencyStoreUnavailable` if Postgres cannot answer.
        """
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingest_batch_idempotency
                        (tenant_id, batch_id, state, response, first_seen_at, updated_at)
                    VALUES (%s, %s, 'in_flight', NULL, now(), now())
                    ON CONFLICT (tenant_id, batch_id) DO UPDATE
                       SET state         = 'in_flight',
                           response      = NULL,
                           first_seen_at = now(),
                           updated_at    = now()
                     WHERE ingest_batch_idempotency.first_seen_at
                             < now() - make_interval(secs => %s)
                        OR (ingest_batch_idempotency.state = 'in_flight'
                            AND ingest_batch_idempotency.updated_at
                                  < now() - make_interval(secs => %s))
                    RETURNING state
                    """,
                    (tenant_id, batch_id, float(ttl_seconds), float(IN_FLIGHT_LEASE_S)),
                )
                claimed = cur.fetchone()
                if claimed is not None:
                    conn.commit()
                    return None
                # The conflicting row is live and belongs to someone else. Read it to find out
                # whether that someone has finished.
                cur.execute(
                    """
                    SELECT state, response FROM ingest_batch_idempotency
                    WHERE tenant_id = %s AND batch_id = %s
                    """,
                    (tenant_id, batch_id),
                )
                row = cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise IdempotencyStoreUnavailable(str(exc)) from exc

        if row is None:
            # The row vanished between the two statements (a prune landing on an aged-out record).
            # Not knowing is not an excuse to double-write, so this is retryable, not accepted.
            return _in_flight_response(batch_id)
        state, response = row
        if state == "complete" and response is not None:
            return _decode_response(batch_id, response)
        return _in_flight_response(batch_id)

    def record(self, tenant_id: str, batch_id: str, response: BatchResponse) -> None:
        """Persist the outcome so a later replay is answered from it.

        Best-effort BY DESIGN: this runs after the spans are already written, so raising here would
        turn a successful ingest into an error the client retries, and that retry is the duplicate.
        A failure leaves the claim ``in_flight`` until its lease expires (see
        :data:`IN_FLIGHT_LEASE_S`), which delays a replay's answer but never doubles a write.
        """
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_batch_idempotency
                       SET state = 'complete', response = %s, updated_at = now()
                     WHERE tenant_id = %s AND batch_id = %s
                    """,
                    (json.dumps(_encode_response(response)), tenant_id, batch_id),
                )
                conn.commit()
        except psycopg.Error:
            logger.exception(
                "could not record idempotency outcome for batch %s; the claim stays in_flight "
                "until its lease expires and a replay inside that window is answered retryable",
                batch_id,
            )

    def prune(self, ttl_seconds: float) -> int:
        """Delete receipts older than the idempotency window. Returns rows removed.

        Not on the hot path: a DELETE per ingest request would put a write and its vacuum debt in
        front of every batch. Called from the gateway's shutdown/maintenance seam instead, and safe
        to run at any cadence because a record past the window is one a replay can no longer match.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ingest_batch_idempotency "
                "WHERE first_seen_at < now() - make_interval(secs => %s)",
                (float(ttl_seconds),),
            )
            deleted = cur.rowcount
            conn.commit()
        return deleted


class BatchIdempotency:
    """The ingest path's idempotency gate: in-process fast path, durable source of truth.

    Drop-in for :class:`tally.wire.IdempotencyCache` (same ``check_or_store`` / ``record`` surface),
    so a test that swaps the bare cache in still works and the pipeline needs no branching.
    """

    def __init__(
        self,
        cache: IdempotencyCache,
        durable: DurableIdempotencyStore | None,
        *,
        ttl_seconds: float,
    ) -> None:
        self._cache = cache
        self._durable = durable
        self._ttl = ttl_seconds

    @property
    def durable_enabled(self) -> bool:
        return self._durable is not None

    def check_or_store(self, req: BatchRequest) -> BatchResponse | None:
        """``None`` means "process this batch"; a response means "do not, return this instead".

        Order matters. The cache is consulted first ONLY for a hit, because a hit is a record this
        very process created and is therefore at least as fresh as Postgres. A miss proves nothing
        (it is exactly what a restart looks like) and always falls through to the durable claim.
        """
        if self._durable is None:
            return self._cache.check_or_store(req)

        hit = self._cache.peek(req.tenant_id, req.batch_id)
        if hit is not None:
            return hit
        claimed = self._durable.claim(req.tenant_id, req.batch_id, self._ttl)
        if claimed is not None:
            return claimed
        # Won the durable claim: mirror the reservation into the cache so a same-process replay is
        # answered without a round trip.
        self._cache.check_or_store(req)
        return None

    def record(self, req: BatchRequest, response: BatchResponse) -> None:
        self._cache.record(req, response)
        if self._durable is not None:
            self._durable.record(req.tenant_id, req.batch_id, response)


def build_batch_idempotency(settings: Settings) -> BatchIdempotency:
    """Wire the gate at boot, probing whether the durable layer is actually usable.

    The probe is a single cheap read of the migration-0031 table. It exists so the gateway can state
    which mode it is in rather than discovering it on the first duplicate: a deployment that has not
    applied the migration, or has no Postgres, gets the pre-CTO-245 in-process-only behaviour and a
    WARNING that says so in those words. ``idempotency_durable_required`` turns that into a startup
    failure, which is what a production deployment should set.
    """
    cache = IdempotencyCache(ttl_seconds=settings.idempotency_ttl_s)
    if not settings.idempotency_durable:
        logger.warning(
            "durable batch idempotency is DISABLED by configuration; a batch replayed across a "
            "gateway restart will be accepted twice and its spend counted twice"
        )
        return BatchIdempotency(cache, None, ttl_seconds=settings.idempotency_ttl_s)

    store = PostgresIdempotencyStore(settings)
    try:
        with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ingest_batch_idempotency LIMIT 1")
            cur.fetchone()
    except psycopg.Error as exc:
        if settings.idempotency_durable_required:
            raise RuntimeError(
                "durable batch idempotency is required but ingest_batch_idempotency is "
                f"unreachable ({exc}); apply db/postgres/0032_ingest_batch_idempotency.sql"
            ) from exc
        logger.warning(
            "durable batch idempotency UNAVAILABLE (%s); falling back to the in-process cache "
            "alone, which does not survive a restart, so a batch replayed across one will be "
            "accepted twice. Apply db/postgres/0032_ingest_batch_idempotency.sql, and set "
            "TALLY_IDEMPOTENCY_DURABLE_REQUIRED=true to make this a startup failure instead",
            exc,
        )
        return BatchIdempotency(cache, None, ttl_seconds=settings.idempotency_ttl_s)

    logger.info("durable batch idempotency enabled (ingest_batch_idempotency)")
    return BatchIdempotency(cache, store, ttl_seconds=settings.idempotency_ttl_s)


def _in_flight_response(batch_id: str) -> BatchResponse:
    """The answer to "another worker holds this batch and we do not know how it went".

    RETRY, not ACCEPTED: claiming acceptance for spans we have not seen land would be a fabricated
    success, and processing it ourselves would be the double write. ``retry_after_ms`` is short
    because the holder is normally milliseconds from recording its outcome.
    """
    return BatchResponse(
        batch_id=batch_id,
        status=Status.RETRY,
        server_hints=ServerHints(retry_after_ms=1000),
    )


def _encode_response(response: BatchResponse) -> dict[str, object]:
    return {
        "batch_id": response.batch_id,
        "status": response.status.value,
        "accepted_spans": response.accepted_spans,
        "partial_errors": [
            {"item_id": e.item_id, "code": e.code, "message": e.message}
            for e in response.partial_errors
        ],
    }


def _decode_response(batch_id: str, payload: object) -> BatchResponse:
    """Rebuild a stored receipt. A receipt we cannot parse is answered retryable, never accepted."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return _in_flight_response(batch_id)
    if not isinstance(payload, dict):
        return _in_flight_response(batch_id)
    try:
        status = Status(str(payload.get("status", Status.ACCEPTED.value)))
    except ValueError:
        return _in_flight_response(batch_id)
    raw_errors = payload.get("partial_errors")
    errors = [
        PartialError(
            item_id=str(e.get("item_id", "")),
            code=str(e.get("code", "")),
            message=str(e.get("message", "")),
        )
        for e in raw_errors
        if isinstance(e, dict)
    ] if isinstance(raw_errors, list) else []
    accepted = payload.get("accepted_spans")
    return BatchResponse(
        batch_id=str(payload.get("batch_id") or batch_id),
        status=status,
        accepted_spans=accepted if isinstance(accepted, int) else 0,
        partial_errors=errors,
    )


__all__ = [
    "IN_FLIGHT_LEASE_S",
    "BatchIdempotency",
    "DurableIdempotencyStore",
    "IdempotencyStoreUnavailable",
    "PostgresIdempotencyStore",
    "build_batch_idempotency",
]
