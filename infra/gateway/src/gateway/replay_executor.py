"""Replay executor — run captured samples against candidate models (CTO-113).

For each ``(sample, candidate_model)`` the executor:

1. Loads the scrubbed envelope from object storage.
2. Issues the candidate-model request (real provider call in prod; injectable mock in tests).
3. Captures tokens / latency / error.
4. Computes authoritative cost from the SDK price catalog (CTO-106 expanded coverage).
5. Writes a :class:`ReplayRunRow` to ClickHouse ``replay_runs``.

Two hard safety rails:

* **Per-tenant daily budget cap.** Before each candidate call we sum today's ``CostMicroUsd`` on
  the tenant's ``replay_runs`` rows. If projected next-call cost would push the day over the cap
  (``tenant_replay_config.daily_budget_usd``), the call is skipped with ``excluded_budget=True``.
  A bug in replay must never burn $10k overnight.
* **Per-tenant concurrency limit.** No more than ``MAX_CONCURRENT`` candidate calls run at once
  per tenant; the rest queue.

Retries: 1 retry on transient (5xx, network) errors; 0 on 4xx — replaying a 400 a second time
just costs money.

v1 only supports the ``"resolved-context"`` fidelity tier (no live RAG, no live tool execution).
The :class:`ReplayResult` and ClickHouse row carry that tier explicitly so when we later add
``"live-retrieval"`` and ``"live-tool-execution"`` tiers the historical rows stay correctly tagged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol
from uuid import UUID, uuid4

from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd

from gateway.replay_store import ReplayBlobStore, ReplayRunRow

UTC = timezone.utc
logger = logging.getLogger("tally.gateway.replay_executor")

MAX_CONCURRENT_PER_TENANT = 5
MAX_RETRIES_TRANSIENT = 1


# --- Candidate-model client contract --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CandidateCall:
    """What the executor passes to the provider client. Free-form so different providers can
    interpret it — text/chat/etc."""

    provider: str
    model: str
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class CandidateResponse:
    """What the provider client returns. ``status_code`` is the HTTP code (0 == network error)."""

    input_tokens: int
    output_tokens: int
    response_text: str = ""
    status_code: int = 200
    error_msg: str = ""


class CandidateClient(Protocol):
    """Anything callable with ``(call) -> CandidateResponse``. The prod implementation routes to
    the right provider SDK; tests inject a mock that returns deterministic token counts."""

    async def __call__(self, call: CandidateCall) -> CandidateResponse: ...


# --- Result / outcome -------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReplayResult:
    sample_id: UUID
    candidate_provider: str
    candidate_model: str
    excluded_budget: bool = False
    error_msg: str = ""
    row: ReplayRunRow | None = None

    @property
    def succeeded(self) -> bool:
        return self.row is not None and not self.error_msg


# --- ClickHouse-side spend lookup -------------------------------------------------------------

class TodaysSpendLookup(Protocol):
    def __call__(self, tenant_id: str) -> int:
        """Return today's already-burnt replay spend for ``tenant_id``, in micro-USD."""


# --- Executor ---------------------------------------------------------------------------------

@dataclass
class ReplayExecutor:
    catalog: PriceCatalog
    blob_store: ReplayBlobStore
    client: CandidateClient
    todays_spend_micro_usd: TodaysSpendLookup
    # Sink — anything callable that accepts a finished ReplayRunRow. The gateway wires this to
    # the ClickHouse writer; tests pass `list.append`.
    sink: Callable[[ReplayRunRow], None]
    # Per-tenant concurrency limiter. One semaphore per tenant, lazily created.
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def _semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(tenant_id)
        if sem is None:
            sem = asyncio.Semaphore(MAX_CONCURRENT_PER_TENANT)
            self._semaphores[tenant_id] = sem
        return sem

    async def replay_sample(
        self,
        *,
        tenant_id: str,
        sample_id: UUID,
        object_key: str,
        candidate_provider: str,
        candidate_model: str,
        daily_budget_usd: Decimal,
        # Estimated cost in micro-USD for the upcoming candidate call — used for the pre-flight
        # budget check. Callers compute this from the sample's known input tokens + an output
        # token estimate (default 1x input tokens, i.e. a 50/50 chat shape).
        estimated_call_cost_micro_usd: int = 0,
        # Optional transform applied to the loaded envelope before the candidate call — used by
        # the body-driven what-if estimate (CTO-128) to apply a system_prompt_override. Pure;
        # must return a (possibly new) envelope dict and never mutate the input.
        envelope_transform: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> ReplayResult:
        """Replay one sample against one candidate. Honors the daily budget cap and concurrency limit."""

        budget_cap_micro_usd = int(Decimal(daily_budget_usd) * Decimal(1_000_000))
        already_spent = self.todays_spend_micro_usd(tenant_id)
        if already_spent + estimated_call_cost_micro_usd > budget_cap_micro_usd:
            logger.info(
                "replay: budget cap hit for tenant=%s (spent=%d cap=%d projected=%d)",
                tenant_id, already_spent, budget_cap_micro_usd, estimated_call_cost_micro_usd,
            )
            return ReplayResult(
                sample_id=sample_id,
                candidate_provider=candidate_provider,
                candidate_model=candidate_model,
                excluded_budget=True,
            )

        async with self._semaphore(tenant_id):
            envelope_bytes = self.blob_store.get_bytes(object_key)
            envelope = json.loads(envelope_bytes.decode("utf-8"))
            if envelope_transform is not None:
                envelope = envelope_transform(envelope)
            call = CandidateCall(
                provider=candidate_provider, model=candidate_model, envelope=envelope
            )
            response, latency_ms = await self._call_with_retry(call)

        cost_micro_usd, _version = compute_cost_micro_usd(
            self.catalog,
            candidate_provider,
            candidate_model,
            Usage(
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            ),
        )
        row = ReplayRunRow(
            tenant_id=tenant_id,
            run_id=uuid4(),
            sample_id=sample_id,
            candidate_provider=candidate_provider,
            candidate_model=candidate_model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_micro_usd=cost_micro_usd,
            latency_ms=latency_ms,
            error_msg=response.error_msg,
            ran_at=datetime.now(UTC),
        )
        self.sink(row)
        return ReplayResult(
            sample_id=sample_id,
            candidate_provider=candidate_provider,
            candidate_model=candidate_model,
            error_msg=response.error_msg,
            row=row,
        )

    async def _call_with_retry(
        self, call: CandidateCall
    ) -> tuple[CandidateResponse, int]:
        """Try once; 1 retry on transient (>=500 or status=0) errors; 0 on 4xx."""
        attempts = 0
        last: CandidateResponse | None = None
        start = time.monotonic()
        while True:
            attempts += 1
            try:
                last = await self.client(call)
            except Exception as exc:  # noqa: BLE001 — surface as a synthetic 0 status
                last = CandidateResponse(
                    input_tokens=0, output_tokens=0,
                    status_code=0, error_msg=f"network: {exc}",
                )
            transient = last.status_code == 0 or last.status_code >= 500
            if not transient or attempts > MAX_RETRIES_TRANSIENT:
                break
        latency_ms = int((time.monotonic() - start) * 1000)
        return last, latency_ms
