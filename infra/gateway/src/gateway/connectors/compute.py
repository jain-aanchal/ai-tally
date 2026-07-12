"""Compute cost-layer connector — AWS Cost Explorer & GCP Cloud Billing (CTO-143).

Pulls a tenant's daily cloud *compute* spend and hands it to the base connector, which lands one
synthetic ``compute`` span per day. Two providers for v1 (aws | gcp — Azure is out of scope), one
per configured tenant.

Structure mirrors the rest of the gateway: the response-parsing logic is a pair of PURE functions
(:func:`parse_aws_cost_response`, :func:`parse_gcp_billing_rows`) that are unit-tested against
recorded fixtures, and the SDK-touching clients (:class:`AwsCostExplorerClient`,
:class:`GcpCloudBillingClient`) are thin wrappers that fetch-then-parse. The cloud SDKs (boto3 /
google-cloud-bigquery) are imported LAZILY inside the clients so the gateway — and the whole test
suite — imports this module without those deps installed. Tests inject a fake ``BillingClient``.

Credentials by reference only: the clients resolve ``config.credentials_ref`` (a Secret Manager /
KMS / ARN pointer, or ``'aws-default-chain'`` for the ambient AWS credential chain) — raw keys never
appear in the DB, in this module, or in logs.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from tally.schema import usd_to_micro

from gateway.connectors.base import (
    BillingClient,
    CloudBillingConnector,
    ConnectorConfig,
    DailyCost,
)

# Default cost-allocation tag set when a tenant hasn't overridden tag_filter. Scopes the billing
# query to the AI workload so unrelated infra spend doesn't leak into the compute layer.
DEFAULT_TAG_FILTER: dict[str, str] = {"tally:workload": "ai"}


def _to_micro(amount: object) -> int:
    """Coerce a provider's money value (str/number) to integer micro-USD; 0 on anything unparseable."""
    if amount is None:
        return 0
    try:
        d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return 0
    if d <= 0:
        return 0
    return usd_to_micro(d)


def parse_aws_cost_response(response: dict[str, object]) -> list[DailyCost]:
    """Parse an AWS Cost Explorer ``get_cost_and_usage`` response into per-day totals.

    Expects DAILY granularity: ``ResultsByTime[].TimePeriod.Start`` (a ``YYYY-MM-DD`` string, the
    inclusive start of the day) and ``ResultsByTime[].Total.UnblendedCost.Amount`` (a decimal
    string, USD). Days with zero/absent cost are dropped — a $0 day carries no synthetic span.
    """
    out: list[DailyCost] = []
    results = response.get("ResultsByTime") or []
    if not isinstance(results, list):
        return out
    for entry in results:
        if not isinstance(entry, dict):
            continue
        period = entry.get("TimePeriod")
        total = entry.get("Total")
        if not isinstance(period, dict) or not isinstance(total, dict):
            continue
        start = period.get("Start")
        cost = total.get("UnblendedCost")
        amount = cost.get("Amount") if isinstance(cost, dict) else None
        if not isinstance(start, str):
            continue
        micro = _to_micro(amount)
        if micro <= 0:
            continue
        out.append(DailyCost(day=date.fromisoformat(start), cost_micro_usd=micro))
    return out


def parse_gcp_billing_rows(rows: list[dict[str, object]]) -> list[DailyCost]:
    """Parse GCP Cloud Billing rows (BigQuery billing-export shape) into per-day totals.

    Each row is expected to carry a day key (``usage_start_time`` or ``day``) and a ``cost`` value.
    ``usage_start_time`` may be a full ISO timestamp — we take the date part. Rows for the same day
    are summed (the export emits one row per service/SKU), so the connector still lands ONE span
    per day.
    """
    per_day: dict[date, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_day = row.get("usage_start_time") or row.get("day")
        if not isinstance(raw_day, str):
            continue
        day = date.fromisoformat(raw_day[:10])
        micro = _to_micro(row.get("cost"))
        if micro <= 0:
            continue
        per_day[day] = per_day.get(day, 0) + micro
    return [DailyCost(day=d, cost_micro_usd=m) for d, m in sorted(per_day.items())]


class AwsCostExplorerClient:
    """AWS Cost Explorer fetcher. boto3 imported lazily; credentials resolved by reference.

    ``credentials_ref == 'aws-default-chain'`` uses the ambient AWS credential chain (instance role
    / env / SSO). Any other value is treated as an assumable-role ARN — resolved by the deployment's
    STS wiring, which is out of scope here; we pass it through so the prod wrapper can assume it.
    """

    def __init__(self, session_factory=None) -> None:
        # session_factory injectable purely so integration harnesses can supply a pre-built session;
        # unit tests use a fake BillingClient instead and never construct this.
        self._session_factory = session_factory

    def _client(self, config: ConnectorConfig):
        if self._session_factory is not None:
            session = self._session_factory(config.credentials_ref)
        else:  # pragma: no cover - exercised only against live AWS
            import boto3  # lazy: keep boto3 out of the base install / test path

            session = boto3.Session()
        return session.client("ce")

    @staticmethod
    def _filter(tag_filter: dict[str, str]) -> dict:
        """Build the Cost Explorer ``Filter`` from the tenant's cost-allocation tag set."""
        tags = tag_filter or DEFAULT_TAG_FILTER
        clauses = [
            {"Tags": {"Key": key, "Values": [value]}} for key, value in sorted(tags.items())
        ]
        return clauses[0] if len(clauses) == 1 else {"And": clauses}

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
            Filter=self._filter(config.tag_filter),
        )
        return parse_aws_cost_response(response)


class GcpCloudBillingClient:
    """GCP Cloud Billing fetcher over the BigQuery billing export. Client imported lazily.

    ``credentials_ref`` is a Secret Manager reference to the service-account/table config the prod
    wrapper resolves; unit tests inject a fake instead of constructing this.
    """

    def __init__(self, query_runner=None) -> None:
        self._query_runner = query_runner

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._query_runner is not None:
            rows = self._query_runner(config, start_day, end_day)
        else:  # pragma: no cover - exercised only against live GCP
            from google.cloud import bigquery  # lazy import

            client = bigquery.Client()
            rows = [dict(r) for r in client.query(_gcp_query(config, start_day, end_day)).result()]
        return parse_gcp_billing_rows(rows)


def _gcp_query(config: ConnectorConfig, start_day: date, end_day: date) -> str:  # pragma: no cover
    """Placeholder BigQuery billing-export query (the export table id is deployment config)."""
    tags = config.tag_filter or DEFAULT_TAG_FILTER
    label_predicate = " AND ".join(
        f"EXISTS (SELECT 1 FROM UNNEST(labels) l WHERE l.key = '{k}' AND l.value = '{v}')"
        for k, v in sorted(tags.items())
    )
    return (
        "SELECT DATE(usage_start_time) AS usage_start_time, SUM(cost) AS cost "
        "FROM `billing_export.gcp_billing` "
        f"WHERE DATE(usage_start_time) BETWEEN '{start_day.isoformat()}' AND '{end_day.isoformat()}' "
        f"AND {label_predicate} "
        "GROUP BY usage_start_time ORDER BY usage_start_time"
    )


class ComputeCostConnector(CloudBillingConnector):
    """Daily compute connector. ``operation='compute'`` → synthetic ``compute``-layer spans.

    Constructed with one provider's :class:`BillingClient` (the tenant's configured provider). The
    base class handles emission, idempotency, and run recording; this subclass only wires the fetch.
    """

    operation = "compute"

    def fetch_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._client is None:
            raise RuntimeError("ComputeCostConnector requires a billing_client")
        return self._client.get_daily_costs(config, start_day=start_day, end_day=end_day)


def build_billing_client(provider: str) -> BillingClient:
    """Factory: the live :class:`BillingClient` for a provider. Used by the cron/backfill entrypoints."""
    if provider == "aws":
        return AwsCostExplorerClient()
    if provider == "gcp":
        return GcpCloudBillingClient()
    raise ValueError(f"unsupported cloud_provider {provider!r} (aws|gcp only)")
