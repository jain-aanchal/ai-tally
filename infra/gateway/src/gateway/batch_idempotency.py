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
(that retry is what would duplicate). So :meth:`record` retries the receipt write and then logs and
swallows. See :meth:`PostgresIdempotencyStore.record` for exactly what is and is not guaranteed when
every attempt fails; the short version is that past the in-flight lease the ClickHouse
ReplacingMergeTree backstop is what contains the residual, not this table.

ONLY A TERMINAL OUTCOME IS EVER RECORDED (CTO-389). ACCEPTED, PARTIAL and REJECTED are answers about
the batch and are stored. RETRY is not an answer: it says a dependency failed and the batch's fate is
unknown. Writing one here froze a transient ClickHouse blip into the batch's permanent receipt, so
every retry for the rest of the idempotency window (24h by default) was replayed that 503 without
touching storage, and the spans were lost while the client had already been metered for them. A
retryable outcome therefore RELEASES the claim (:meth:`BatchIdempotency.release`) so the next attempt
re-runs the write, and :meth:`BatchIdempotency.record` routes a RETRY response there rather than
trusting every call site to remember.

STARTUP PROBE. The durable layer is enabled only if the table is reachable when the gateway boots
(see :func:`build_batch_idempotency`). A gateway that cannot see Postgres at boot runs with the
in-process cache alone, which is the pre-CTO-245 behaviour, and says so at WARNING level rather than
implying a guarantee it is not providing. Any deployment that needs the guarantee must set
``TALLY_IDEMPOTENCY_DURABLE_REQUIRED=true``, which turns that degradation into a refusal to start.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
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

#: How many times :meth:`PostgresIdempotencyStore.record` tries to persist a receipt before giving up.
#:
#: CTO-389. A lost receipt is not harmless: the claim stays ``in_flight``, and once the lease above
#: expires a retry reclaims the batch and writes its spans a second time. Retrying here narrows that
#: to "Postgres was unreachable for the whole attempt" instead of "one connection was dropped".
#: Bounded and immediate, with no sleep between attempts, because an ingest response is held open for
#: the duration and a slow receipt is itself a way to lose the batch.
RECEIPT_WRITE_ATTEMPTS = 3


class IdempotencyStoreUnavailable(RuntimeError):
    """The durable store could not answer. The caller MUST refuse the batch, never accept it.

    Raised only from the claim path. See the module docstring for why refusing beats accepting.
    """


class DurableIdempotencyStore(Protocol):
    """What the ingest path needs from a durable store, so tests can drive it without Postgres."""

    def claim(self, tenant_id: str, batch_id: str, ttl_seconds: float) -> BatchResponse | None: ...

    def record(self, tenant_id: str, batch_id: str, response: BatchResponse) -> None: ...

    def release(self, tenant_id: str, batch_id: str) -> None: ...


class PostgresIdempotencyStore:
    """Postgres-backed claim/record over ``ingest_batch_idempotency`` (migration 0032).

    Connection per call and tenant-scoped SQL, matching
    :class:`gateway.ingest_cursors.IngestCursorStore` and every other control-plane store here.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn
        # CTO-389 review: the claim TOKEN for every batch this process currently holds, keyed by
        # (tenant, batch) and valued by the row's ``updated_at`` at the moment the claim was won.
        # ``release`` deletes only a row still carrying its token, which is what stops a worker that
        # stalled past IN_FLIGHT_LEASE_S from deleting a claim someone else has since taken. Guarded
        # by a lock because the ingest path is threaded (FastAPI runs sync handlers on a threadpool).
        self._claims: dict[tuple[str, str], datetime] = {}
        self._claims_lock = threading.Lock()

    def _remember_claim(self, tenant_id: str, batch_id: str, token: datetime) -> None:
        with self._claims_lock:
            self._claims[(tenant_id, batch_id)] = token

    def _forget_claim(self, tenant_id: str, batch_id: str) -> datetime | None:
        """Hand back (and drop) the token for a claim this process holds, if it holds one."""
        with self._claims_lock:
            return self._claims.pop((tenant_id, batch_id), None)

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
                    RETURNING updated_at
                    """,
                    (tenant_id, batch_id, float(ttl_seconds), float(IN_FLIGHT_LEASE_S)),
                )
                claimed = cur.fetchone()
                if claimed is not None:
                    conn.commit()
                    # CTO-389 review: remember WHICH claim we won. ``ON CONFLICT DO UPDATE`` above
                    # stamps a fresh ``updated_at`` on every successful (re)claim, so this value
                    # identifies this generation of the claim and nobody else's. See :meth:`release`.
                    self._remember_claim(tenant_id, batch_id, claimed[0])
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

        Only ever called with a TERMINAL outcome (ACCEPTED / PARTIAL / REJECTED). A retryable
        outcome goes to :meth:`release` instead; see the module docstring for why recording one was
        the CTO-389 bug.

        Never raises: this runs after the spans are already written, so raising would turn a
        successful ingest into an error the client retries, and that retry is the duplicate. The
        write is attempted :data:`RECEIPT_WRITE_ATTEMPTS` times and then logged and swallowed.

        WHAT IS AND IS NOT GUARANTEED WHEN EVERY ATTEMPT FAILS. This docstring used to say a failure
        here "delays a replay's answer but never doubles a write". That was false, and stating a
        guarantee the code does not provide is worse than the gap itself, so here is the real one.
        The claim stays ``in_flight``. A replay arriving inside :data:`IN_FLIGHT_LEASE_S` is answered
        retryable, which is the delay. But once the lease elapses the claim becomes reclaimable, and
        a retry after that point re-runs the write, so the spans ARE written twice. Postgres cannot
        prevent that, because the only evidence the first write landed is the receipt that just
        failed to store.

        What actually contains the residual is downstream and deliberate: ``otel_spans`` is a
        ReplacingMergeTree keyed on span identity (``db/clickhouse/otel_spans.sql``), so a
        byte-identical replay collapses at merge time. The honest statement of this method's own
        guarantee is therefore: a replay is delayed for the lease, and past the lease correctness
        rests on the ClickHouse backstop, not on this table.
        """
        payload = json.dumps(_encode_response(response))
        last_error: psycopg.Error | None = None
        for attempt in range(1, RECEIPT_WRITE_ATTEMPTS + 1):
            try:
                with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE ingest_batch_idempotency
                           SET state = 'complete', response = %s, updated_at = now()
                         WHERE tenant_id = %s AND batch_id = %s
                        """,
                        (payload, tenant_id, batch_id),
                    )
                    conn.commit()
                # The claim is now a receipt, so this process no longer holds a releasable claim on
                # it. Dropping the token here keeps a later stray release from deleting a real
                # answer (CTO-389 review).
                self._forget_claim(tenant_id, batch_id)
                return
            except psycopg.Error as exc:
                last_error = exc
                logger.warning(
                    "receipt write %d/%d failed for batch %s: %s",
                    attempt,
                    RECEIPT_WRITE_ATTEMPTS,
                    batch_id,
                    exc,
                )
        logger.error(
            "could not record idempotency outcome for batch %s after %d attempts; the claim stays "
            "in_flight, a replay inside the lease is answered retryable, and a retry after the "
            "lease will re-write these spans and rely on the otel_spans ReplacingMergeTree backstop",
            batch_id,
            RECEIPT_WRITE_ATTEMPTS,
            exc_info=last_error,
        )

    def release(self, tenant_id: str, batch_id: str) -> None:
        """Give up a claim whose attempt ended retryably, so the next attempt re-runs the write.

        CTO-389. The row is DELETEd rather than promoted to ``complete``, because the outcome is
        genuinely unknown and the only honest record of "we do not know" is no record at all.
        Leaving it ``in_flight`` would also be honest, but it would make the client wait out the
        whole lease for a failure we already know about, so deleting is the kinder of the two.

        Best-effort, and safe when it fails: a row left behind is an ``in_flight`` claim that the
        lease reclaims. That is a delay, never an acceptance, and never a stored wrong answer.

        SCOPED TO THE CLAIM WE ACTUALLY HOLD (CTO-389 review). The DELETE carries the claim token
        (the ``updated_at`` :meth:`claim` returned) as well as the key. Without it the delete was
        "whatever in_flight row happens to be here now", and :meth:`claim` resets the row to a fresh
        lease via ``ON CONFLICT DO UPDATE``: a worker that stalled past :data:`IN_FLIGHT_LEASE_S`
        would then delete a claim another worker had since taken, leaving both free to write the
        same spans. With the token a stale release matches no row and does nothing, which is exactly
        right, because a stalled worker has no claim left to give up.

        Holding no token means we cannot prove the row is ours, so nothing is deleted. That costs
        the lease, which is a delay and never a double write.
        """
        token = self._forget_claim(tenant_id, batch_id)
        if token is None:
            logger.warning(
                "no claim token held for batch %s; leaving the row alone rather than deleting a "
                "claim that may now belong to another worker. It stays in_flight until its lease "
                "expires, which delays the client's retry but accepts nothing",
                batch_id,
            )
            return
        try:
            with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ingest_batch_idempotency "
                    "WHERE tenant_id = %s AND batch_id = %s AND state = 'in_flight' "
                    "  AND updated_at = %s",
                    (tenant_id, batch_id, token),
                )
                conn.commit()
        except psycopg.Error:
            logger.exception(
                "could not release the idempotency claim for batch %s; it stays in_flight until "
                "its lease expires, which delays the client's retry but accepts nothing",
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
        """Store a TERMINAL outcome as this batch's answer.

        A RETRY response is routed to :meth:`release` instead of being stored (CTO-389). The gate
        enforces that here rather than trusting each ingest call site to remember, because the cost
        of forgetting once is a transient failure frozen into the batch's answer for the whole
        idempotency window, with the spans lost and the client already metered for them.
        """
        if response.status is Status.RETRY:
            self.release(req)
            return
        self._cache.record(req, response)
        if self._durable is not None:
            self._durable.record(req.tenant_id, req.batch_id, response)

    def release(self, req: BatchRequest) -> None:
        """Give up the claim on a batch whose attempt ended retryably (CTO-389).

        Both layers, and the cache first. The cache is the one that can answer a later attempt
        without consulting Postgres, so a stale reservation left there would shadow a perfectly good
        durable state and keep answering the old provisional entry.
        """
        self._cache.release(req)
        if self._durable is not None:
            self._durable.release(req.tenant_id, req.batch_id)


def build_batch_idempotency(settings: Settings) -> BatchIdempotency:
    """Wire the gate at boot, probing whether the durable layer is actually usable.

    The probe is a single cheap read of the migration-0032 table. It exists so the gateway can state
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
    "RECEIPT_WRITE_ATTEMPTS",
    "BatchIdempotency",
    "DurableIdempotencyStore",
    "IdempotencyStoreUnavailable",
    "PostgresIdempotencyStore",
    "build_batch_idempotency",
]
