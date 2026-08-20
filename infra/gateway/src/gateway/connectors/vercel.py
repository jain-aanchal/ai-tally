"""Vercel compute + egress cost connector (CTO-163).

Vercel-hosted AI apps pay Vercel for BOTH Functions compute (invocations + GB-hours) and bandwidth
(egress). CTO-143 built the compute layer (AWS/GCP) and CTO-144 built the egress layer (Vercel /
Cloudflare / AWS). This ticket adds Vercel to the COMPUTE layer and reconciles the egress overlap
with CTO-144 so a Vercel app's full infra cost sits next to its LLM spend on /cost.

Both layers are pulled from the SAME Vercel usage/billing API payload (``items[]``), split by line
item ``type``:

  * **compute** — ``function_invocations`` + ``function_duration`` (GB-hours) line items, summed to
    ONE ``compute`` span/day via the reused :class:`gateway.connectors.compute.ComputeCostConnector`
    (``operation='compute'``, ``GenAiSystem='vercel'``).
  * **egress**  — ``bandwidth`` line items, ONE ``egress`` span/day. This is the SAME data CTO-144's
    :class:`gateway.connectors.egress.VercelBandwidthClient` already ingests.

Egress double-count reconciliation with CTO-144
-----------------------------------------------
CTO-144's egress connector already names Vercel bandwidth as an egress source. Emitting a second
Vercel egress span here would double-count on the /cost Egress column. Two safeguards:

  1. **Gate (primary).** :class:`VercelCostConnector` emits egress ONLY when ``emit_egress=True``
     (``tenant_vercel_config.emit_egress``, default ``false``). Default behaviour: this connector
     owns the compute half; Vercel egress flows solely through CTO-144's ``EgressCostConnector``.
     Set the flag only for a tenant that has NO CTO-144 ``tenant_egress_config`` row for
     ``egress_provider='vercel'`` (so exactly one path emits).
  2. **Same span id (defence in depth).** When it does emit egress, this connector routes through the
     CTO-144 ``EgressCostConnector`` + ``VercelBandwidthClient`` verbatim, so the synthetic span id is
     ``synthetic_span_id(tenant, 'vercel', 'egress', day)`` — IDENTICAL to the id CTO-144 would
     produce. Even if both paths ran for the same day, the base ``span_exists`` guard collapses them
     to one row: no double-count is structurally possible.

Everything else reuses the CTO-143 base verbatim (emitter, idempotency guard, run contract, run
recorder). Structure mirrors the sibling connectors: a PURE parse function unit-tested against a
recorded fixture, and thin fetch-then-parse clients with the HTTP dep imported LAZILY so the gateway
and the whole test suite import without ``requests``. Tests inject a fake fetcher — no network.

Credentials by reference only: ``access_token_ref`` is a Secret Manager reference the prod wrapper
resolves; the raw Vercel token never appears in the DB, this module, or logs. Honest-under-uncertainty:
a failed fetch records a ``failed`` run and emits NO span.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from gateway.connectors.base import (
    BillingClient,
    ConnectorConfig,
    DailyCost,
    RunResult,
)
from gateway.connectors.compute import ComputeCostConnector, _to_micro
from gateway.connectors.egress import (
    EgressConfig,
    EgressCostConnector,
    VercelBandwidthClient,
)

#: Fixed provider label for Vercel synthetic spans (``GenAiSystem``). Matches CTO-144's egress
#: provider so the egress span id lines up exactly (see module docstring, safeguard 2).
VERCEL_PROVIDER = "vercel"

#: Vercel usage line-item ``type`` values that belong to the COMPUTE layer: Function invocations and
#: Function duration (GB-hours). ``'compute'`` is accepted as a generic fallback (some payloads label
#: the Serverless Functions line item simply ``compute``). ``bandwidth`` is DELIBERATELY excluded — it
#: is egress and must never leak into the compute total (that would double-count across layers).
_COMPUTE_TYPES = frozenset({"function_invocations", "function_duration", "compute"})


@dataclass(frozen=True, slots=True)
class VercelConfig(ConnectorConfig):
    """One tenant's Vercel connector config — the loaded ``tenant_vercel_config`` row.

    Extends :class:`ConnectorConfig`: ``cloud_provider`` is pinned to ``'vercel'`` so the reused
    compute/egress connectors key their synthetic spans on the right provider, and ``credentials_ref``
    carries the Secret Manager reference to the Vercel access token (NEVER the raw token).

    ``team_id`` / ``project_id`` are the PUBLIC Vercel identifiers the usage query is scoped to (not
    secrets). ``enabled`` lets a tenant keep the row but pause the connector. ``emit_egress`` is the
    CTO-144 reconciliation gate (default ``False`` — egress via CTO-144's egress connector).
    """

    team_id: str = ""
    project_id: str = ""
    enabled: bool = True
    emit_egress: bool = False
    # tag_filter is inherited but unused for Vercel (the API is already team/project-scoped); keep the
    # default so the ConnectorConfig contract holds.
    tag_filter: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VercelRunResult:
    """Combined outcome of one Vercel connector cycle: the compute run, plus egress if the gate is on.

    ``egress`` is ``None`` when ``emit_egress`` is ``False`` (the default — Vercel egress is owned by
    CTO-144's egress connector, so this connector does not touch it).
    """

    compute: RunResult
    egress: RunResult | None = None


# --- pure response parser -----------------------------------------------------------------------


def parse_vercel_compute(response: dict[str, object]) -> list[DailyCost]:
    """Parse a Vercel usage/billing response into per-day COMPUTE totals (micro-USD).

    Expects ``items[]`` each with a ``date`` (``YYYY-MM-DD``), an ``amount`` (decimal USD string) and
    a ``type``. Only ``type in _COMPUTE_TYPES`` (Function invocations + GB-hours) items are summed —
    ``bandwidth`` items are IGNORED so compute never double-counts the egress layer. Same-day items
    (invocations + duration, possibly several rows) collapse to ONE total; ``$0`` days are dropped.

    Mirrors :func:`gateway.connectors.egress.parse_vercel_usage`, which does the reciprocal split
    (bandwidth only) on the very same payload.
    """
    per_day: dict[date, int] = {}
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in _COMPUTE_TYPES:
            continue
        raw_day = item.get("date")
        if not isinstance(raw_day, str):
            continue
        micro = _to_micro(item.get("amount"))
        if micro <= 0:
            continue
        day = date.fromisoformat(raw_day[:10])
        per_day[day] = per_day.get(day, 0) + micro
    return [DailyCost(day=d, cost_micro_usd=m) for d, m in sorted(per_day.items())]


# --- fetch-then-parse clients (HTTP dep imported lazily) ----------------------------------------


class VercelUsageClient:
    """Fetches the raw Vercel usage/billing payload for a team/project window. Injectable for tests.

    ``http_getter`` is the seam tests use to supply a recorded payload; when absent (prod), ``requests``
    is imported LAZILY and the Vercel access token is resolved from ``config.credentials_ref`` by the
    deployment's Secret Manager wiring (out of scope here — the token never lands in this module).
    """

    def __init__(self, http_getter=None, *, api_base: str = "https://api.vercel.com") -> None:
        self._http_getter = http_getter
        self._api_base = api_base

    def fetch(self, config: ConnectorConfig, start_day: date, end_day: date) -> dict[str, object]:
        if self._http_getter is not None:
            return self._http_getter(config, start_day, end_day)
        # pragma: no cover - exercised only against the live Vercel API
        import requests  # lazy: keep requests off the base install / test path

        team = getattr(config, "team_id", "")
        project = getattr(config, "project_id", "")
        params = {"from": start_day.isoformat(), "to": end_day.isoformat()}
        if team:
            params["teamId"] = team
        if project:
            params["projectId"] = project
        resp = requests.get(
            f"{self._api_base}/v1/usage",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


class VercelComputeClient:
    """:class:`BillingClient` for the COMPUTE layer: fetch the Vercel payload, keep only compute.

    Wraps a :class:`VercelUsageClient` so the same fetched payload shape feeds both layers; this side
    parses the Functions compute line items. Range-filters defensively (the base already asks for the
    window, but the payload may carry neighbours).
    """

    def __init__(self, usage_client: VercelUsageClient) -> None:
        self._usage = usage_client

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        response = self._usage.fetch(config, start_day, end_day)
        costs = parse_vercel_compute(response)
        return [c for c in costs if start_day <= c.day <= end_day]


# --- orchestrator -------------------------------------------------------------------------------


class VercelCostConnector:
    """Runs the Vercel compute connector (always) and the egress connector (gated) for one tenant.

    Compute always flows through the reused :class:`ComputeCostConnector` with a Vercel client. Egress
    is emitted ONLY when ``config.emit_egress`` is set, routed through CTO-144's
    :class:`EgressCostConnector` + :class:`VercelBandwidthClient` so the span id matches CTO-144's
    exactly (see module docstring). Both connectors share the ONE injected store, so idempotency holds
    across layers and across the CTO-144 egress path.

    ``recorder`` (a :class:`gateway.connectors.base.RunRecorder`) is stamped by both sub-runs; the
    ``tenant_vercel_config`` recorder ignores the ``connector_id`` and stamps the single tenant row.
    """

    def __init__(
        self,
        *,
        store,
        usage_client: VercelUsageClient,
        recorder=None,
    ) -> None:
        self._store = store
        self._usage = usage_client
        self._recorder = recorder

    def _compute_connector(self) -> ComputeCostConnector:
        return ComputeCostConnector(
            store=self._store,
            recorder=self._recorder,
            billing_client=VercelComputeClient(self._usage),
        )

    def _egress_connector(self) -> EgressCostConnector:
        # Reuse CTO-144's egress connector + Vercel bandwidth client VERBATIM. The bandwidth client's
        # http_getter is this connector's usage fetch, so egress parses the same payload via CTO-144's
        # parse_vercel_usage — identical rows AND identical synthetic span id to the CTO-144 path.
        return EgressCostConnector(
            store=self._store,
            recorder=self._recorder,
            billing_client=VercelBandwidthClient(http_getter=self._usage.fetch),
        )

    @staticmethod
    def _egress_config(config: VercelConfig) -> EgressConfig:
        """Project the Vercel config onto the CTO-144 egress config shape (provider='vercel')."""
        return EgressConfig(
            tenant_id=config.tenant_id,
            cloud_provider=VERCEL_PROVIDER,
            credentials_ref=config.credentials_ref,
            resource_id=config.team_id,
        )

    def run(self, config: VercelConfig, *, day: date) -> VercelRunResult:
        """Daily entry point: emit the compute span, and the egress span iff the gate is on."""
        return self._run_range(config, start_day=day, end_day=day)

    def run_backfill(
        self, config: VercelConfig, *, start_day: date, end_day: date
    ) -> VercelRunResult:
        """Backfill ``[start_day, end_day]``. Idempotent via the base ``span_exists`` guard."""
        return self._run_range(config, start_day=start_day, end_day=end_day)

    def _run_range(
        self, config: VercelConfig, *, start_day: date, end_day: date
    ) -> VercelRunResult:
        compute = self._compute_connector()._run_range(
            config, start_day=start_day, end_day=end_day
        )
        egress: RunResult | None = None
        if config.emit_egress:
            egress = self._egress_connector()._run_range(
                self._egress_config(config), start_day=start_day, end_day=end_day
            )
        return VercelRunResult(compute=compute, egress=egress)


def build_vercel_usage_client(api_base: str = "https://api.vercel.com") -> VercelUsageClient:
    """Factory: the live usage client for the cron/backfill entrypoints."""
    return VercelUsageClient(api_base=api_base)


def build_vercel_compute_client(usage_client: VercelUsageClient) -> BillingClient:
    """Factory: a compute-layer :class:`BillingClient` backed by ``usage_client``."""
    return VercelComputeClient(usage_client)
