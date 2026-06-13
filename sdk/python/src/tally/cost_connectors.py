# SPDX-License-Identifier: Apache-2.0
"""Cost connector framework + v1 connectors.

Implements CTO-63. Spec section 9 W5.

End-to-end cost lives or dies on connector *breadth*: LLM spend is only the visible tip of the
iceberg. The real bill also includes vector DBs, cloud infra, tool/search APIs and hosting. A
pluggable connector framework lets new cost sources be added incrementally without touching the
core, so coverage can grow one provider at a time.

A connector NEVER fetches anything. The caller is responsible for fetching the raw payload from a
provider's billing/usage API (keys, pagination, retries live there); a connector's only job is to
*normalize* an injected raw payload into a sequence of :class:`CostRecord` -- the one cost model
everything maps to. Each record carries (tenant, feature, time) plus an integer micro-USD cost.
This keeps the module pure-logic, offline and deterministic, matching the house style where
dependencies are injected (see ``tally.evals`` judge, ``tally.egress`` Protocol transport,
``tally.replay`` injected model).

Money is integer micro-USD everywhere (see ``tally.schema.usd_to_micro`` / ``micro_to_usd``); rate
math uses :class:`~decimal.Decimal`, never float dollars.

Adding a new cost source = writing a new class that satisfies the :class:`CostConnector` Protocol
and registering it. The :class:`ConnectorRegistry` / :class:`CostIngestRunner` need no change.

Out of scope (clean seams left for follow-ups):
  - Reconciler logic / cloud-billing true-up (CTO-64): this module emits *estimated* normalized
    records; reconciling them against authoritative invoices is the reconciler's job.
  - Cost workflow UI (CTO-65 / CTO-66): :meth:`CostIngestRunner.health` surfaces health/last-sync
    for the UI to render, but no UI lives here.
  - Real network fetching: the caller fetches; connectors only parse injected payloads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd
from tally.schema import DEFAULT_CURRENCY, usd_to_micro

# Feature tag applied when a row carries no usable feature attribution.
DEFAULT_FEATURE_TAG = "unattributed"

# Match the safety-boundary contract (tally.safety): never swallow these.
_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit)


# --- the normalized cost model -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostRecord:
    """The normalized output of every connector -- the one cost model everything maps to.

    A record attributes a cost to ``(tenant_id, feature_tag, occurred_at)`` with an integer
    micro-USD amount. ``occurred_at`` is the event's *own* timestamp (when the spend happened),
    never ingest time, so cost lands in the right time bucket.
    """

    source: str
    tenant_id: str
    feature_tag: str
    occurred_at: datetime
    cost_micro_usd: int
    currency: str = DEFAULT_CURRENCY
    quantity: Decimal | None = None
    unit: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """A plain JSON-friendly dict (Decimal/datetime rendered as strings)."""
        return {
            "source": self.source,
            "tenant_id": self.tenant_id,
            "feature_tag": self.feature_tag,
            "occurred_at": self.occurred_at.isoformat(),
            "cost_micro_usd": self.cost_micro_usd,
            "currency": self.currency,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "unit": self.unit,
            "metadata": dict(self.metadata),
        }


# --- the stable connector interface ------------------------------------------------------------


@runtime_checkable
class CostConnector(Protocol):
    """Pluggable cost source. A connector turns ONE already-fetched raw payload into zero or more
    :class:`CostRecord`s. It must never fetch and should never raise (the runner is the boundary,
    but well-behaved connectors skip junk rows rather than blowing up the batch).

    Adding a source = a new class satisfying this Protocol; the registry/runner are untouched.
    """

    @property
    def name(self) -> str: ...

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]: ...


# --- small defensive helpers -------------------------------------------------------------------


def _rows(raw: object) -> list[object]:
    """Best-effort extraction of an iterable of rows from a raw payload.

    Accepts a list/tuple of rows, or a mapping wrapping them under common keys. Anything else
    yields an empty list (never raises). Strings/bytes are not treated as row iterables.
    """
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        for key in ("rows", "data", "items", "results", "usage", "line_items"):
            inner = raw.get(key)
            if isinstance(inner, (list, tuple)):
                return list(inner)
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _as_mapping(row: object) -> Mapping[str, object] | None:
    return row if isinstance(row, Mapping) else None


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return default


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str, float)):
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001 - malformed money string; skip the row
            return None
    return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _feature(row: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        tag = _str(row.get(key))
        if tag is not None:
            return tag
    return DEFAULT_FEATURE_TAG


def _timestamp(row: Mapping[str, object], *keys: str) -> datetime | None:
    """Parse the event's own timestamp from the first present key. ISO-8601 strings, epoch
    seconds (int/float), or an existing ``datetime``. Returns None (skip) if unparseable.
    """
    for key in keys:
        value = row.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
    return None


# --- v1 connectors -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMProxyConnector:
    """OpenAI/Anthropic LLM spend via the proxy.

    Parses proxy usage rows (provider, model, prompt/completion tokens, feature tag, timestamp)
    and prices them through the injected :class:`~tally.pricing.PriceCatalog` -- reusing the real
    catalog rather than re-deriving rates. A row with no usable catalog price is skipped (no
    record), so an estimated record is never fabricated. Provider is carried in metadata.
    """

    catalog: PriceCatalog
    name: str = "llm_proxy"

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]:
        out: list[CostRecord] = []
        for row in _rows(raw):
            mapping = _as_mapping(row)
            if mapping is None:
                continue
            provider = _str(mapping.get("provider")) or _str(mapping.get("system"))
            model = _str(mapping.get("model")) or _str(mapping.get("response_model"))
            occurred_at = _timestamp(mapping, "occurred_at", "timestamp", "ts", "start_time")
            if provider is None or model is None or occurred_at is None:
                continue
            usage = Usage(
                input_tokens=_int(mapping.get("prompt_tokens", mapping.get("input_tokens"))),
                output_tokens=_int(
                    mapping.get("completion_tokens", mapping.get("output_tokens"))
                ),
                cached_input_tokens=_int(mapping.get("cached_input_tokens")),
            )
            cost_micro, version = compute_cost_micro_usd(
                self.catalog,
                provider,
                model,
                usage,
                at=occurred_at.date(),
                tenant_id=tenant_id,
            )
            if not version:
                # catalog miss -> skip rather than assert a zero/partial cost
                continue
            out.append(
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag=_feature(mapping, "feature_tag", "feature"),
                    occurred_at=occurred_at,
                    cost_micro_usd=cost_micro,
                    quantity=Decimal(usage.input_tokens + usage.output_tokens),
                    unit="tokens",
                    metadata={
                        "provider": provider,
                        "model": model,
                        "price_catalog_version": version,
                    },
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class PineconeConnector:
    """Pinecone vector DB billing rows -> micro-USD via injected Decimal unit rates.

    Rows carry per-index read-units / write-units; rates are USD per unit (Decimal). Cost is the
    sum of (units x rate) across the row, converted at the end with ``usd_to_micro``.
    """

    read_unit_usd: Decimal
    write_unit_usd: Decimal
    name: str = "pinecone"

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]:
        out: list[CostRecord] = []
        for row in _rows(raw):
            mapping = _as_mapping(row)
            if mapping is None:
                continue
            occurred_at = _timestamp(mapping, "occurred_at", "timestamp", "date", "ts")
            if occurred_at is None:
                continue
            read_units = _int(mapping.get("read_units"))
            write_units = _int(mapping.get("write_units"))
            usd = self.read_unit_usd * Decimal(read_units) + self.write_unit_usd * Decimal(
                write_units
            )
            out.append(
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag=_feature(mapping, "feature_tag", "feature", "index"),
                    occurred_at=occurred_at,
                    cost_micro_usd=usd_to_micro(usd),
                    quantity=Decimal(read_units + write_units),
                    unit="units",
                    metadata={
                        "index": _str(mapping.get("index")) or "",
                        "read_units": read_units,
                        "write_units": write_units,
                    },
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class AWSCostExplorerConnector:
    """AWS Cost Explorer ``ResultsByTime`` -> micro-USD.

    Walks the standard Cost Explorer shape: each ``ResultsByTime`` entry has a ``TimePeriod`` and a
    list of ``Groups`` keyed by a tag (we map the resource tag -> feature_tag), each with an
    ``UnblendedCost`` ``{Amount, Unit}`` in dollars. ``usd_to_micro`` converts at the boundary.
    """

    name: str = "aws_cost_explorer"

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]:
        mapping = _as_mapping(raw)
        if mapping is None:
            return []
        results = mapping.get("ResultsByTime")
        if not isinstance(results, (list, tuple)):
            return []
        out: list[CostRecord] = []
        for entry in results:
            entry_map = _as_mapping(entry)
            if entry_map is None:
                continue
            period = _as_mapping(entry_map.get("TimePeriod")) or {}
            occurred_at = _timestamp(period, "Start", "start")
            if occurred_at is None:
                continue
            groups = entry_map.get("Groups")
            if not isinstance(groups, (list, tuple)):
                continue
            for group in groups:
                group_map = _as_mapping(group)
                if group_map is None:
                    continue
                metrics = _as_mapping(group_map.get("Metrics")) or {}
                unblended = _as_mapping(metrics.get("UnblendedCost")) or {}
                amount = _decimal(unblended.get("Amount"))
                if amount is None:
                    continue
                keys = group_map.get("Keys")
                tag = ""
                if isinstance(keys, (list, tuple)) and keys:
                    tag = _str(keys[0]) or ""
                currency = _str(unblended.get("Unit")) or DEFAULT_CURRENCY
                out.append(
                    CostRecord(
                        source=self.name,
                        tenant_id=tenant_id,
                        feature_tag=tag or DEFAULT_FEATURE_TAG,
                        occurred_at=occurred_at,
                        cost_micro_usd=usd_to_micro(amount),
                        currency=currency,
                        metadata={"resource_tag": tag},
                    )
                )
        return out


@dataclass(frozen=True, slots=True)
class TavilyConnector:
    """Tavily tool/search API: per-search-call rows (count x price) -> micro-USD.

    Rows carry a search ``count`` and a per-call USD price (Decimal). A row may override the
    default price via ``price_per_call_usd``; otherwise the injected default applies.
    """

    price_per_call_usd: Decimal
    name: str = "tavily"

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]:
        out: list[CostRecord] = []
        for row in _rows(raw):
            mapping = _as_mapping(row)
            if mapping is None:
                continue
            occurred_at = _timestamp(mapping, "occurred_at", "timestamp", "ts")
            if occurred_at is None:
                continue
            count = _int(mapping.get("count", mapping.get("searches")), default=0)
            price = _decimal(mapping.get("price_per_call_usd")) or self.price_per_call_usd
            usd = price * Decimal(count)
            out.append(
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag=_feature(mapping, "feature_tag", "feature"),
                    occurred_at=occurred_at,
                    cost_micro_usd=usd_to_micro(usd),
                    quantity=Decimal(count),
                    unit="searches",
                    metadata={"count": count},
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class VercelConnector:
    """Vercel hosting usage line items -> micro-USD via injected Decimal rates.

    Each line item has a ``metric`` (e.g. ``bandwidth`` in GB, ``function_invocations`` count) and
    a numeric ``quantity``. The injected ``rates`` map metric -> USD per unit (Decimal). An unknown
    metric (no rate) is skipped rather than priced at zero.
    """

    rates: Mapping[str, Decimal]
    name: str = "vercel"

    def parse(self, raw: object, *, tenant_id: str) -> Sequence[CostRecord]:
        out: list[CostRecord] = []
        for row in _rows(raw):
            mapping = _as_mapping(row)
            if mapping is None:
                continue
            metric = _str(mapping.get("metric"))
            occurred_at = _timestamp(mapping, "occurred_at", "timestamp", "ts", "period_start")
            quantity = _decimal(mapping.get("quantity"))
            if metric is None or occurred_at is None or quantity is None:
                continue
            rate = self.rates.get(metric)
            if rate is None:
                # unknown metric -> skip rather than fabricate a zero-cost record
                continue
            usd = rate * quantity
            out.append(
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag=_feature(mapping, "feature_tag", "feature", "project"),
                    occurred_at=occurred_at,
                    cost_micro_usd=usd_to_micro(usd),
                    quantity=quantity,
                    unit=metric,
                    metadata={"metric": metric, "project": _str(mapping.get("project")) or ""},
                )
            )
        return out


# --- registry + runner + health ----------------------------------------------------------------


class ConnectorRegistry:
    """Register connectors by name. Adding a source needs no change here -- just ``register`` it."""

    def __init__(self) -> None:
        self._connectors: dict[str, CostConnector] = {}

    def register(self, connector: CostConnector) -> CostConnector:
        self._connectors[connector.name] = connector
        return connector

    def get(self, name: str) -> CostConnector | None:
        return self._connectors.get(name)

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def __contains__(self, name: object) -> bool:
        return name in self._connectors


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """Connector health / last-sync, surfaced for the cost workflow UI (CTO-65/66).

    ``ok`` is True when the last run completed with no errors. ``last_sync`` is the wall-clock time
    of the most recent run (None if never run).
    """

    name: str
    last_sync: datetime | None = None
    records_emitted: int = 0
    errors_count: int = 0
    last_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.errors_count == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "records_emitted": self.records_emitted,
            "errors_count": self.errors_count,
            "last_error": self.last_error,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of running a connector over a batch: the records plus the resulting health."""

    records: list[CostRecord]
    health: ConnectorHealth


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def run_connector(
    connector: CostConnector,
    payloads: Sequence[object],
    *,
    tenant_id: str,
    now: datetime | None = None,
) -> IngestResult:
    """Apply ``connector`` to a batch of already-fetched raw ``payloads``.

    NEVER raises if a payload is malformed: the error is counted in health and the batch
    continues (mirrors the never-crash invariant in ``tally.safety``). An empty batch yields an
    empty result and a healthy zero-sync health.
    """
    records: list[CostRecord] = []
    errors = 0
    last_error: str | None = None
    for payload in payloads:
        try:
            parsed = connector.parse(payload, tenant_id=tenant_id)
        except _NEVER_SWALLOW:
            raise
        except BaseException as exc:  # noqa: BLE001 - connector boundary; must never escape
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        for record in parsed or ():
            if isinstance(record, CostRecord):
                records.append(record)
    health = ConnectorHealth(
        name=connector.name,
        last_sync=now or _now(),
        records_emitted=len(records),
        errors_count=errors,
        last_error=last_error,
    )
    return IngestResult(records=records, health=health)


class CostIngestRunner:
    """Drives a registry of connectors over batches of injected payloads, collecting normalized
    :class:`CostRecord`s and per-connector :class:`ConnectorHealth`.

    Stateless except for the latest health per connector (surfaced via :meth:`health`). Never
    raises on malformed input.
    """

    def __init__(self, registry: ConnectorRegistry | None = None) -> None:
        self.registry = registry or ConnectorRegistry()
        self._health: dict[str, ConnectorHealth] = {}

    def register(self, connector: CostConnector) -> CostConnector:
        return self.registry.register(connector)

    def run(
        self,
        name: str,
        payloads: Sequence[object],
        *,
        tenant_id: str,
        now: datetime | None = None,
    ) -> IngestResult:
        """Run a single named connector over a batch. Records its health. Unknown name yields an
        empty result with an error-flagged health (never raises)."""
        connector = self.registry.get(name)
        if connector is None:
            health = ConnectorHealth(
                name=name,
                last_sync=now or _now(),
                records_emitted=0,
                errors_count=1,
                last_error=f"unknown connector: {name!r}",
            )
            self._health[name] = health
            return IngestResult(records=[], health=health)
        result = run_connector(connector, payloads, tenant_id=tenant_id, now=now)
        self._health[name] = result.health
        return result

    def run_all(
        self,
        batches: Mapping[str, Sequence[object]],
        *,
        tenant_id: str,
        now: datetime | None = None,
    ) -> list[CostRecord]:
        """Run several connectors, one batch each (keyed by connector name). Returns all records;
        health is recorded per connector and available via :meth:`health`."""
        records: list[CostRecord] = []
        for name, payloads in batches.items():
            records.extend(self.run(name, payloads, tenant_id=tenant_id, now=now).records)
        return records

    def health(self, name: str | None = None) -> ConnectorHealth | dict[str, ConnectorHealth]:
        """Surface connector health / last-sync. With a name, that connector's latest health (a
        never-run connector reports a healthy zero-sync). Without, a copy of all recorded health."""
        if name is not None:
            return self._health.get(name, ConnectorHealth(name=name))
        return dict(self._health)
