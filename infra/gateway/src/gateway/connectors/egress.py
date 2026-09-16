"""Egress cost-layer connector: Vercel / Cloudflare / AWS bandwidth (CTO-144).

Same connector shape as compute (CTO-143): pull a tenant's daily *egress* (bandwidth-out) spend and
hand it to :class:`gateway.connectors.base.CloudBillingConnector`, which lands ONE synthetic
``egress`` span per provider per day. Egress is a smaller-scope variant: it REUSES the CTO-143 base
verbatim (emitter, idempotency guard, run contract, run recorder); only the per-provider fetch
differs, and that is the single abstract method the base already exposes.

Three providers for v1, each behind an INJECTABLE ``BillingClient`` so no test touches the network:

  * **Vercel**:     bandwidth line items from the Vercel billing/usage API (dollar amounts already).
  * **Cloudflare**:  bytes-out from the Cloudflare GraphQL analytics aggregates for the configured
    zone(s), converted to USD at the tenant's configured ``usd_per_gb`` rate.
  * **AWS**:        ``get_cost_and_usage`` filtered to the ``DataTransfer-Out-Bytes`` usage type
    (dollar amounts already). Reuses compute's boto3 client pattern and AWS response parser.

Multiple egress providers per tenant SUM cleanly with NO double-counting: each provider is a separate
control-plane row and a separate run, so every synthetic span is keyed on a DISTINCT provider via
``synthetic_span_id(tenant, provider, 'egress', day)``. Three providers on the same day → three
distinct span ids → the /cost Egress column sums them once each. Re-running is idempotent (the base's
``span_exists`` guard).

Honest-under-uncertainty: a failed fetch (or a Cloudflare zone with no configured ``usd_per_gb``
rate; we will not invent a price for bytes) records a ``failed`` run and emits NO span.

Structure mirrors compute: pure parse functions unit-tested against recorded fixtures, thin
SDK-touching clients that fetch-then-parse with the vendor SDK/HTTP imported LAZILY.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from tally.schema import usd_to_micro

from gateway.connectors.base import (
    BillingClient,
    CloudBillingConnector,
    ConnectorConfig,
    DailyCost,
)

# Reuse compute's AWS Cost Explorer response parser verbatim: an egress query is the same
# get_cost_and_usage shape, only the Filter (usage type) differs.
from gateway.connectors.compute import _to_micro, aws_client_for, parse_aws_cost_response
from gateway.connectors.credentials import CredentialResolutionError, TenantCredentials

#: ``(config) -> token``: resolves a tenant's API token from its Secrets Manager reference (CTO-381).
TokenProvider = Callable[[ConnectorConfig], str]
#: ``(method, url, *, headers, params, json, timeout) -> dict``: the HTTP seam, raising on failure.
Transport = Callable[..., Any]


class ConnectorFetchError(RuntimeError):
    """A provider API call failed. The message carries a status code only, never a body or token."""


def _requests_transport(
    method: str, url: str, *, headers, params=None, json=None, timeout: float = 30
):  # pragma: no cover - exercised only against live provider APIs
    import requests  # lazy: keep requests off the base install / test path

    resp = requests.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def bearer_request(
    label: str,
    method: str,
    url: str,
    *,
    config: ConnectorConfig,
    token_provider: TokenProvider | None,
    transport: Transport | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
) -> Any:
    """One authenticated provider call for a tenant (CTO-381).

    Before CTO-381 the live Vercel and Cloudflare calls sent no Authorization header at all: the
    token reference was never resolved. The token is resolved here, used for this one request and
    dropped. A failure is re-raised as :class:`ConnectorFetchError` carrying only the HTTP status
    (or exception type): provider error text and HTTP-library messages can echo request headers,
    and whatever this raises is logged and routed to stored run errors.
    """
    if token_provider is None:
        raise CredentialResolutionError("no credential resolver is wired for this connector")
    token = token_provider(config)
    try:
        return (transport or _requests_transport)(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - never let the provider's error text through
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        raise ConnectorFetchError(f"{label} request failed ({detail})") from None
    finally:
        del token

# One GiB in bytes, the unit ``usd_per_gb`` is quoted in.
_BYTES_PER_GB = Decimal(1024 * 1024 * 1024)

# AWS usage type that isolates bytes-out egress. AWS bills DataTransfer-Out-Bytes across regions;
# scoping the Cost Explorer Filter to it keeps compute spend out of the egress layer.
AWS_EGRESS_USAGE_TYPE = "DataTransfer-Out-Bytes"

EGRESS_PROVIDERS = ("vercel", "cloudflare", "aws")


@dataclass(frozen=True, slots=True)
class EgressConfig(ConnectorConfig):
    """One tenant+provider egress config. Extends :class:`ConnectorConfig` with the per-provider
    resource id and the Cloudflare byte→USD rate.

    ``cloud_provider`` carries the egress provider (``'vercel' | 'cloudflare' | 'aws'``); the base
    is provider-agnostic and only uses it to key the synthetic span, which is exactly the
    distinct-provider guarantee egress needs. ``resource_id`` is the Cloudflare zone id / Vercel team
    id / AWS account id (a public identifier, NOT a secret). ``usd_per_gb`` is required ONLY for
    Cloudflare (whose analytics report bytes, not dollars); absent, the Cloudflare fetch fails soft
    rather than guessing a price.
    """

    resource_id: str = ""
    usd_per_gb: Decimal | None = None


def _bytes_to_micro(num_bytes: object, usd_per_gb: Decimal | None) -> int:
    """Convert bytes-out to integer micro-USD at ``usd_per_gb``. Raises if no rate is configured.

    Refusing to price bytes without an explicit rate keeps the connector honest: better a ``failed``
    run than a guessed egress number.
    """
    if usd_per_gb is None:
        raise ValueError("cloudflare egress requires usd_per_gb to price bytes-out (none configured)")
    try:
        b = num_bytes if isinstance(num_bytes, Decimal) else Decimal(str(num_bytes))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if b <= 0:
        return 0
    return usd_to_micro((b / _BYTES_PER_GB) * usd_per_gb)


# --- pure response parsers ---------------------------------------------------------------------


def parse_vercel_usage(response: dict[str, object]) -> list[DailyCost]:
    """Parse a Vercel billing/usage response into per-day BANDWIDTH totals (micro-USD).

    Expects ``items[]`` each with a ``date`` (``YYYY-MM-DD``), an ``amount`` (decimal USD string) and
    a ``type``. Only ``type == 'bandwidth'`` items are summed; compute/build/other line items are
    ignored so egress never double-counts another layer's spend. Same-day items sum to ONE total.
    """
    per_day: dict[date, int] = {}
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "bandwidth":
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


def parse_cloudflare_bytes(response: dict[str, object]) -> dict[date, int]:
    """Parse Cloudflare GraphQL analytics aggregates into bytes-out per day, summed across zones.

    Expects the ``httpRequests1dGroups`` shape:
    ``data.viewer.zones[].httpRequests1dGroups[] -> {dimensions.date, sum.bytes}``. Multiple zones
    (or groups) for the same day sum, so a tenant with several Cloudflare zones still lands ONE
    egress span per day. Returns raw bytes; the client applies the tenant's ``usd_per_gb`` rate.
    """
    per_day: dict[date, int] = {}
    data = response.get("data") if isinstance(response, dict) else None
    viewer = data.get("viewer") if isinstance(data, dict) else None
    zones = viewer.get("zones") if isinstance(viewer, dict) else None
    if not isinstance(zones, list):
        return {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        groups = zone.get("httpRequests1dGroups")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            dims = group.get("dimensions")
            totals = group.get("sum")
            if not isinstance(dims, dict) or not isinstance(totals, dict):
                continue
            raw_day = dims.get("date")
            raw_bytes = totals.get("bytes")
            if not isinstance(raw_day, str) or raw_bytes is None:
                continue
            try:
                num = int(raw_bytes)
            except (TypeError, ValueError):
                continue
            if num <= 0:
                continue
            day = date.fromisoformat(raw_day[:10])
            per_day[day] = per_day.get(day, 0) + num
    return per_day


# --- SDK/HTTP-touching clients (fetch-then-parse; vendor deps imported lazily) ------------------


class VercelBandwidthClient:
    """Vercel billing/usage fetcher. HTTP client imported lazily; credentials by reference.

    ``credentials_ref`` is a Secrets Manager ARN for the Vercel API token, resolved per request by
    ``token_provider`` (CTO-381). ``http_getter`` replaces the whole fetch in unit tests;
    ``transport`` replaces only the HTTP call, so tests can exercise the authenticated path.
    """

    def __init__(
        self,
        http_getter=None,
        *,
        token_provider: TokenProvider | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._http_getter = http_getter
        self._token_provider = token_provider
        self._transport = transport

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._http_getter is not None:
            response = self._http_getter(config, start_day, end_day)
        else:
            response = bearer_request(
                "Vercel usage API",
                "GET",
                "https://api.vercel.com/v1/usage",
                config=config,
                token_provider=self._token_provider,
                transport=self._transport,
                params={
                    "teamId": getattr(config, "resource_id", ""),
                    "from": start_day.isoformat(),
                    "to": end_day.isoformat(),
                },
            )
        costs = parse_vercel_usage(response)
        return [c for c in costs if start_day <= c.day <= end_day]


class CloudflareAnalyticsClient:
    """Cloudflare GraphQL analytics fetcher: bytes-out priced at the tenant's ``usd_per_gb`` rate.

    Client imported lazily. ``resource_id`` is the zone id; ``usd_per_gb`` MUST be set on the config
    or the fetch fails soft (no guessed price). The API token comes from ``token_provider`` per
    request (CTO-381); ``graphql_runner`` replaces the whole fetch in unit tests.
    """

    def __init__(
        self,
        graphql_runner=None,
        *,
        token_provider: TokenProvider | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._graphql_runner = graphql_runner
        self._token_provider = token_provider
        self._transport = transport

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._graphql_runner is not None:
            response = self._graphql_runner(config, start_day, end_day)
        else:
            response = bearer_request(
                "Cloudflare GraphQL API",
                "POST",
                "https://api.cloudflare.com/client/v4/graphql",
                config=config,
                token_provider=self._token_provider,
                transport=self._transport,
                json_body={"query": _cloudflare_query(config, start_day, end_day)},
            )
        rate = getattr(config, "usd_per_gb", None)
        per_day = parse_cloudflare_bytes(response)
        out: list[DailyCost] = []
        for day, num_bytes in sorted(per_day.items()):
            if not (start_day <= day <= end_day):
                continue
            micro = _bytes_to_micro(num_bytes, rate)
            if micro <= 0:
                continue
            out.append(DailyCost(day=day, cost_micro_usd=micro))
        return out


def _cloudflare_query(config: ConnectorConfig, start_day: date, end_day: date) -> str:  # pragma: no cover
    """Placeholder GraphQL query (zone id is deployment config)."""
    zone = getattr(config, "resource_id", "")
    return (
        "{ viewer { zones(filter: {zoneTag: \"%s\"}) { httpRequests1dGroups("
        "limit: 1000, filter: {date_geq: \"%s\", date_leq: \"%s\"}) "
        "{ dimensions { date } sum { bytes } } } } }"
    ) % (zone, start_day.isoformat(), end_day.isoformat())


class AwsEgressCostExplorerClient:
    """AWS Cost Explorer fetcher scoped to bytes-out egress. Mirrors compute's client pattern.

    Acts as the tenant through its resolved IAM role (CTO-381), never ai-tally's ambient identity.
    The Filter narrows ``get_cost_and_usage`` to the ``DataTransfer-Out-Bytes`` usage type so only
    egress lands in this layer.
    """

    def __init__(self, credentials: TenantCredentials | None = None) -> None:
        self._credentials = credentials

    def _client(self, config: ConnectorConfig):
        return aws_client_for(self._credentials, config, "ce")

    @staticmethod
    def _filter() -> dict:
        """Cost Explorer Filter isolating bytes-out egress usage."""
        return {
            "Dimensions": {
                "Key": "USAGE_TYPE_GROUP",
                "Values": [f"EC2: {AWS_EGRESS_USAGE_TYPE}"],
                "MatchOptions": ["CONTAINS"],
            }
        }

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        client = self._client(config)
        # Cost Explorer's End is EXCLUSIVE, so add a day to include end_day.
        response = client.get_cost_and_usage(
            TimePeriod={
                "Start": start_day.isoformat(),
                "End": (end_day + timedelta(days=1)).isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=self._filter(),
        )
        return parse_aws_cost_response(response)


class EgressCostConnector(CloudBillingConnector):
    """Daily egress connector. ``operation='egress'`` → synthetic ``egress``-layer spans.

    Constructed with ONE provider's :class:`BillingClient` (the tenant+provider control-plane row).
    The base handles emission, idempotency, and run recording; this subclass only wires the fetch,
    identical to :class:`gateway.connectors.compute.ComputeCostConnector`, proving the base is reused
    unchanged. Running the connector once per configured provider is what makes multiple egress
    providers sum without double-counting (distinct provider per synthetic span id).
    """

    operation = "egress"

    def fetch_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._client is None:
            raise RuntimeError("EgressCostConnector requires a billing_client")
        return self._client.get_daily_costs(config, start_day=start_day, end_day=end_day)


def build_egress_client(
    provider: str, *, credentials: TenantCredentials | None = None
) -> BillingClient:
    """Factory: the live :class:`BillingClient` for an egress provider. Used by cron/backfill.

    ``credentials`` is the run's tenant credential context (CTO-381); without it every live fetch
    fails honestly rather than calling a provider unauthenticated or as ai-tally.
    """
    token_provider = credentials.token_for if credentials is not None else None
    if provider == "vercel":
        return VercelBandwidthClient(token_provider=token_provider)
    if provider == "cloudflare":
        return CloudflareAnalyticsClient(token_provider=token_provider)
    if provider == "aws":
        return AwsEgressCostExplorerClient(credentials=credentials)
    raise ValueError(f"unsupported egress_provider {provider!r} (vercel|cloudflare|aws only)")
