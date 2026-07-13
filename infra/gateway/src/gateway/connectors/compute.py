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

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tally.schema import usd_to_micro

from gateway.connectors.base import (
    BillingClient,
    CloudBillingConnector,
    ConnectorConfig,
    DailyCost,
)

# Default cost-allocation tag set when a tenant hasn't overridden tag_filter. Scopes the AWS billing
# query to the AI workload so unrelated infra spend doesn't leak into the compute layer.
DEFAULT_TAG_FILTER: dict[str, str] = {"tally:workload": "ai"}

# Default GCP label set when a tenant hasn't overridden label_filter (CTO-150). GCP label keys can't
# contain ``:``, so the AI workload is tagged with a ``-`` — distinct from the AWS ``tally:workload``.
DEFAULT_GCP_LABEL_FILTER: dict[str, str] = {"tally-workload": "ai"}

# A fully-qualified BigQuery table id: ``project.dataset.table`` (dots optional for dataset-qualified
# ids). We interpolate the table into the SQL (BQ can't parameterize identifiers), so we validate it
# to a safe charset first — everything else in the query is a bound parameter.
_BQ_TABLE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


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


def _coerce_day(raw: object) -> date | None:
    """Coerce a BQ billing-export day value to a :class:`date`, or ``None`` if unparseable.

    The live BigQuery client hands back ``datetime.date`` / ``datetime.datetime`` objects; recorded
    JSON fixtures hand back ISO strings (possibly a full timestamp — we take the date part).
    """
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def parse_gcp_billing_rows(rows: list[dict[str, object]]) -> list[DailyCost]:
    """Parse GCP Cloud Billing rows (BigQuery billing-export shape) into per-day totals.

    Each row is expected to carry a day key (``usage_start_time`` or ``day``) and a ``cost`` value.
    The day may be a ``date``/``datetime`` (live BQ) or an ISO string/timestamp (fixtures). Rows for
    the same day are summed (the export emits one row per service/SKU), so the connector still lands
    ONE span per day.
    """
    per_day: dict[date, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _coerce_day(row.get("usage_start_time") or row.get("day"))
        if day is None:
            continue
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


def build_gcp_billing_query(
    config: ConnectorConfig, start_day: date, end_day: date
) -> tuple[str, dict[str, object]]:
    """Build the parameterized Cloud Billing BigQuery-export query for ``config``.

    Returns ``(sql, params)`` where ``params`` maps bind-parameter name → value. The day range and
    every label key/value are BOUND parameters (never string-interpolated), so a tenant-supplied
    label can't inject SQL. The export table id is the one value that can't be a bind parameter (BQ
    can't parameterize identifiers), so it's validated against :data:`_BQ_TABLE_RE` and interpolated.

    Standard Cloud Billing export schema: ``usage_start_time`` (TIMESTAMP), ``cost`` (FLOAT64), and
    ``labels`` (``ARRAY<STRUCT<key STRING, value STRING>>``). We sum ``cost`` per calendar day for
    rows carrying every configured label, so the connector lands ONE ``compute`` span per day.
    """
    table = config.bq_billing_export_table
    if not table or not _BQ_TABLE_RE.match(table):
        raise ValueError(f"invalid or missing bq_billing_export_table: {table!r}")

    labels = config.label_filter or DEFAULT_GCP_LABEL_FILTER
    params: dict[str, object] = {"start_day": start_day, "end_day": end_day}
    predicates: list[str] = []
    for i, (key, value) in enumerate(sorted(labels.items())):
        kp, vp = f"label_key_{i}", f"label_val_{i}"
        params[kp] = key
        params[vp] = value
        predicates.append(
            f"EXISTS (SELECT 1 FROM UNNEST(labels) l WHERE l.key = @{kp} AND l.value = @{vp})"
        )
    label_predicate = " AND ".join(predicates)

    sql = (
        "SELECT DATE(usage_start_time) AS day, SUM(cost) AS cost "
        f"FROM `{table}` "
        "WHERE DATE(usage_start_time) BETWEEN @start_day AND @end_day "
        f"AND {label_predicate} "
        "GROUP BY day ORDER BY day"
    )
    return sql, params


class GcpCloudBillingClient:
    """GCP Cloud Billing fetcher over the BigQuery billing export. Client imported LAZILY.

    GCP has no fine-grained REST cost API, so the Cloud Billing *BigQuery export* is the source of
    truth. This client reads the tenant's ``bq_billing_export_table``, filtered to its GCP
    ``label_filter``, and sums cost per day.

    Two injection seams keep the whole thing off the network in tests:

    * ``query_runner`` — a ``(config, start_day, end_day) -> list[dict]`` callable that returns raw
      export rows. Unit tests inject one returning a recorded fixture; nothing touches BigQuery.
    * ``bq_client`` — a pre-built BigQuery-like client (``.query(sql, job_config=...).result()``).
      Integration harnesses may supply one; unit tests use ``query_runner`` instead.

    With neither injected, the live path lazily imports ``google-cloud-bigquery`` and constructs a
    ``bigquery.Client`` from ADC / Workload Identity (``project`` optional) — raw credentials never
    appear here; ``credentials_ref`` is a Secret Manager pointer the deployment resolves out of band.
    """

    def __init__(self, *, query_runner=None, bq_client=None, project: str | None = None) -> None:
        self._query_runner = query_runner
        self._bq_client = bq_client
        self._project = project

    def get_daily_costs(
        self, config: ConnectorConfig, *, start_day: date, end_day: date
    ) -> list[DailyCost]:
        if self._query_runner is not None:
            rows = self._query_runner(config, start_day, end_day)
        else:  # pragma: no cover - exercised only against live GCP / an injected bq_client
            from google.cloud import bigquery  # lazy: keep the SDK out of the base install/test path

            client = self._bq_client or bigquery.Client(project=self._project or None)
            sql, params = build_gcp_billing_query(config, start_day, end_day)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter(
                        name, "DATE" if isinstance(value, date) else "STRING", value
                    )
                    for name, value in params.items()
                ]
            )
            rows = [dict(r) for r in client.query(sql, job_config=job_config).result()]
        return parse_gcp_billing_rows(rows)


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


def build_billing_client(provider: str, *, gcp_project: str | None = None) -> BillingClient:
    """Factory: the live :class:`BillingClient` for a provider. Used by the cron/backfill entrypoints.

    ``gcp_project`` (from ``TALLY_COMPUTE_GCP_BQ_PROJECT``) sets the BigQuery job project for the GCP
    source; ``None`` falls back to the ADC-resolved default project. Ignored for AWS.
    """
    if provider == "aws":
        return AwsCostExplorerClient()
    if provider == "gcp":
        return GcpCloudBillingClient(project=gcp_project or None)
    raise ValueError(f"unsupported cloud_provider {provider!r} (aws|gcp only)")
