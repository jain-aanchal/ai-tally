# SPDX-License-Identifier: Apache-2.0
"""Write side of the cost-connector control plane (CTO-176).

The connector modules (compute / egress / vercel) READ their per-tenant config rows through
:mod:`gateway.connectors.config_store`. Nothing could ever WRITE those rows: a tenant had to insert
into Postgres by hand, so the /connectors page could only ever say "configured in the backend
runner". This module is the missing write path, kept deliberately separate from the read stores so
the connector hot path stays a narrow reader.

One dashboard-facing connector id maps onto one storage row:

  ============================  ===============================  ================================
  connector id                  table                            discriminator
  ============================  ===============================  ================================
  ``aws_cost_explorer``         ``tenant_compute_config``        ``cloud_provider='aws'``
  ``gcp_billing``               ``tenant_compute_config``        ``cloud_provider='gcp'``
  ``vercel``                    ``tenant_vercel_config``         (compute; egress via emit_egress)
  ``cloudflare``                ``tenant_egress_config``         ``egress_provider='cloudflare'``
  ``aws_egress``                ``tenant_egress_config``         ``egress_provider='aws'``
  ``vercel_egress``             ``tenant_egress_config``         ``egress_provider='vercel'``
  ============================  ===============================  ================================

``tenant_compute_config`` is keyed on ``tenant_id`` alone, so **AWS and GCP compute are mutually
exclusive per tenant**: connecting one replaces the other. That is a property of the 0011 schema,
not a limitation invented here, and :meth:`CostConnectorAdmin.upsert` surfaces it by returning the
provider that now owns the row so the UI can say so plainly.

Credentials by REFERENCE only (the 0011 / 0012 / 0016 hard rule): every ``*_ref`` field is a Secret
Manager / KMS / ARN pointer, or the literal ``aws-default-chain``. This module rejects anything that
looks like a raw key before it can reach the database, so a pasted credential fails loudly at the
edge instead of landing in a column.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from gateway.config import Settings

# Dashboard connector ids this module can configure.
COMPUTE_CONNECTORS = {"aws_cost_explorer": "aws", "gcp_billing": "gcp"}
EGRESS_CONNECTORS = {"cloudflare": "cloudflare", "aws_egress": "aws", "vercel_egress": "vercel"}
VERCEL_CONNECTOR = "vercel"
ALL_CONNECTORS = frozenset({*COMPUTE_CONNECTORS, *EGRESS_CONNECTORS, VERCEL_CONNECTOR})

# The one non-reference value the schema blesses: the ambient AWS credential chain.
AMBIENT_AWS = "aws-default-chain"

# Shapes that are almost certainly a RAW credential rather than a reference. Checked before write so
# a pasted key never lands in a column (the schema's length bound is the backstop, not the guard).
_RAW_KEY_PATTERNS = (
    re.compile(r"^AKIA[0-9A-Z]{12,}$"),           # AWS access key id
    re.compile(r"^ASIA[0-9A-Z]{12,}$"),           # AWS temporary access key id
    re.compile(r"^sk-[A-Za-z0-9_-]{16,}$"),       # OpenAI-style secret
    re.compile(r"^whsec_[A-Za-z0-9_-]{16,}$"),    # Stripe webhook secret
    re.compile(r"^ghp_[A-Za-z0-9]{16,}$"),        # GitHub PAT
    re.compile(r"^-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# A reference we recognise: a Secret Manager / KMS / Vault style URI or an ARN.
_REFERENCE_HINTS = (
    "arn:",
    "projects/",
    "gcpkms://",
    "vault:",
    "secret://",
    "sm://",
    "op://",
    "env:",
)


class ConfigError(ValueError):
    """Rejected before touching Postgres. The message is safe to return to the caller."""


def validate_credentials_ref(value: object, *, field: str = "credentials_ref") -> str:
    """Return a validated credential REFERENCE, or raise :class:`ConfigError`.

    Accepts the ambient-AWS sentinel, or a bounded non-empty string that does not look like a raw
    key. We do not require a specific URI scheme: deployments use Secret Manager, KMS, Vault and
    plain ARNs, and inventing an allowlist would reject valid setups. We DO reject the shapes that
    are unambiguously secrets.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} is required")
    ref = value.strip()
    if ref == AMBIENT_AWS:
        return ref
    if len(ref) >= 512:
        raise ConfigError(f"{field} must be shorter than 512 characters")
    for pattern in _RAW_KEY_PATTERNS:
        if pattern.match(ref):
            raise ConfigError(
                f"{field} looks like a raw credential. Store the secret in your secret manager and "
                f"pass its reference (for example an ARN, a projects/... path, or '{AMBIENT_AWS}')."
            )
    return ref


def looks_like_reference(ref: str) -> bool:
    """Whether a validated ref matches a known secret-manager shape. Advisory only."""
    return ref == AMBIENT_AWS or any(ref.startswith(h) for h in _REFERENCE_HINTS)


def _clean_tag_filter(value: object, *, field: str) -> dict[str, str] | None:
    """Coerce a tag/label filter to a flat string->string dict."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{field} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a JSON object")
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ConfigError(f"{field} keys and values must both be strings")
        if not key.strip():
            raise ConfigError(f"{field} keys must be non-empty")
        out[key.strip()] = val.strip()
    return out


def _clean_usd_per_gb(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError("usd_per_gb must be a decimal number") from exc
    if rate < 0:
        raise ConfigError("usd_per_gb must be zero or positive")
    return rate


def _opt_str(value: object, *, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string")
    return value.strip() or None


def _as_dict(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


class TenantNotFound(ConfigError):
    """The caller's tenant identifier does not resolve to a row in ``tenants``."""


def _resolve_tenant_uuid(cur: Any, tenant_id: str) -> str:
    """Map the caller's tenant identifier onto ``tenants.id``.

    The config tables key on ``tenants(id)``, a UUID, but the dashboard and the local dev setup
    identify a tenant by NAME (``local-dev``). Accept either: pass a UUID through untouched,
    otherwise look the name up. Without this a name-based caller trips
    ``InvalidTextRepresentation`` deep in the driver, which surfaces as an opaque 503.
    """
    try:
        return str(uuid.UUID(tenant_id))
    except (ValueError, AttributeError, TypeError):
        pass
    cur.execute("SELECT id FROM tenants WHERE name = %s", (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise TenantNotFound(f"no tenant named '{tenant_id}'")
    return str(row[0])


@dataclass(frozen=True, slots=True)
class ConnectorConfigView:
    """Safe, dashboard-facing view of one configured connector row."""

    connector: str
    configured: bool
    credentials_ref: str | None = None
    is_reference: bool = True
    details: dict[str, Any] | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    connected_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "configured": self.configured,
            "credentials_ref": self.credentials_ref,
            "is_reference": self.is_reference,
            "details": self.details or {},
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "connected_at": self.connected_at,
        }


class CostConnectorAdmin:
    """Read/write the three cost-connector config tables for one tenant."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    # --- reads ---------------------------------------------------------------------------------

    def list_configs(self, tenant_id: str) -> list[ConnectorConfigView]:
        """Every configured connector for the tenant, as safe views."""
        out: list[ConnectorConfigView] = []
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT cloud_provider, credentials_ref, tag_filter, bq_billing_export_table,
                       label_filter, last_run_at, last_status, connected_at
                FROM tenant_compute_config WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is not None:
                provider = str(row[0])
                connector = "aws_cost_explorer" if provider == "aws" else "gcp_billing"
                details: dict[str, Any] = {"cloud_provider": provider}
                if provider == "aws":
                    details["tag_filter"] = _as_dict(row[2])
                else:
                    details["bq_billing_export_table"] = row[3] or ""
                    details["label_filter"] = _as_dict(row[4])
                out.append(
                    ConnectorConfigView(
                        connector=connector,
                        configured=True,
                        credentials_ref=str(row[1]),
                        is_reference=looks_like_reference(str(row[1])),
                        details=details,
                        last_run_at=row[5].isoformat() if row[5] else None,
                        last_status=row[6],
                        connected_at=row[7].isoformat() if row[7] else None,
                    )
                )

            cur.execute(
                """
                SELECT egress_provider, credentials_ref, resource_id, usd_per_gb,
                       last_run_at, last_status, connected_at
                FROM tenant_egress_config WHERE tenant_id = %s ORDER BY egress_provider
                """,
                (tenant_id,),
            )
            for erow in cur.fetchall():
                provider = str(erow[0])
                connector = {
                    "cloudflare": "cloudflare",
                    "aws": "aws_egress",
                    "vercel": "vercel_egress",
                }[provider]
                out.append(
                    ConnectorConfigView(
                        connector=connector,
                        configured=True,
                        credentials_ref=str(erow[1]),
                        is_reference=looks_like_reference(str(erow[1])),
                        details={
                            "egress_provider": provider,
                            "resource_id": erow[2] or "",
                            "usd_per_gb": str(erow[3]) if erow[3] is not None else None,
                        },
                        last_run_at=erow[4].isoformat() if erow[4] else None,
                        last_status=erow[5],
                        connected_at=erow[6].isoformat() if erow[6] else None,
                    )
                )

            cur.execute(
                """
                SELECT access_token_ref, team_id, project_id, enabled, emit_egress,
                       last_run_at, last_status, connected_at
                FROM tenant_vercel_config WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            vrow = cur.fetchone()
            if vrow is not None:
                out.append(
                    ConnectorConfigView(
                        connector=VERCEL_CONNECTOR,
                        configured=True,
                        credentials_ref=str(vrow[0]),
                        is_reference=looks_like_reference(str(vrow[0])),
                        details={
                            "team_id": vrow[1] or "",
                            "project_id": vrow[2] or "",
                            "enabled": bool(vrow[3]),
                            "emit_egress": bool(vrow[4]),
                        },
                        last_run_at=vrow[5].isoformat() if vrow[5] else None,
                        last_status=vrow[6],
                        connected_at=vrow[7].isoformat() if vrow[7] else None,
                    )
                )
        return out

    # --- writes --------------------------------------------------------------------------------

    def upsert(self, tenant_id: str, connector: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create or replace one connector's config row. Returns a note for the caller."""
        if connector not in ALL_CONNECTORS:
            raise ConfigError(f"unknown connector '{connector}'")
        if connector in COMPUTE_CONNECTORS:
            return self._upsert_compute(tenant_id, connector, body)
        if connector in EGRESS_CONNECTORS:
            return self._upsert_egress(tenant_id, connector, body)
        return self._upsert_vercel(tenant_id, body)

    def _upsert_compute(
        self, tenant_id: str, connector: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        provider = COMPUTE_CONNECTORS[connector]
        ref = validate_credentials_ref(body.get("credentials_ref"))
        tag_filter = _clean_tag_filter(body.get("tag_filter"), field="tag_filter")
        label_filter = _clean_tag_filter(body.get("label_filter"), field="label_filter")
        bq_table = _opt_str(body.get("bq_billing_export_table"), field="bq_billing_export_table")
        if provider == "gcp" and not bq_table:
            raise ConfigError(
                "bq_billing_export_table is required for GCP (project.dataset.table of the Cloud "
                "Billing export)"
            )

        replaced: str | None = None
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "SELECT cloud_provider FROM tenant_compute_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            existing = cur.fetchone()
            if existing is not None and str(existing[0]) != provider:
                replaced = str(existing[0])
            # tenant_compute_config is keyed on tenant_id alone, so this REPLACES whichever cloud
            # provider held the row. Surfaced to the caller via `replaced`.
            cur.execute(
                """
                INSERT INTO tenant_compute_config
                    (tenant_id, cloud_provider, credentials_ref, tag_filter,
                     bq_billing_export_table, label_filter)
                VALUES (%s, %s, %s,
                        COALESCE(%s::jsonb, '{"tally:workload": "ai"}'::jsonb),
                        %s,
                        COALESCE(%s::jsonb, '{"tally-workload": "ai"}'::jsonb))
                ON CONFLICT (tenant_id) DO UPDATE SET
                    cloud_provider = EXCLUDED.cloud_provider,
                    credentials_ref = EXCLUDED.credentials_ref,
                    tag_filter = EXCLUDED.tag_filter,
                    bq_billing_export_table = EXCLUDED.bq_billing_export_table,
                    label_filter = EXCLUDED.label_filter,
                    last_run_at = NULL,
                    last_status = NULL
                """,
                (
                    tenant_id,
                    provider,
                    ref,
                    json.dumps(tag_filter) if tag_filter is not None else None,
                    bq_table,
                    json.dumps(label_filter) if label_filter is not None else None,
                ),
            )
            conn.commit()
        note = None
        if replaced:
            other = "gcp_billing" if replaced == "gcp" else "aws_cost_explorer"
            note = (
                f"Replaced the existing {replaced.upper()} compute connector. One compute provider "
                f"per tenant, so {other} is now disconnected."
            )
        return {"connector": connector, "replaced": replaced, "note": note}

    def _upsert_egress(self, tenant_id: str, connector: str, body: dict[str, Any]) -> dict[str, Any]:
        provider = EGRESS_CONNECTORS[connector]
        ref = validate_credentials_ref(body.get("credentials_ref"))
        resource_id = _opt_str(body.get("resource_id"), field="resource_id")
        usd_per_gb = _clean_usd_per_gb(body.get("usd_per_gb"))
        if provider == "cloudflare":
            # Cloudflare analytics report BYTES, not dollars. Without a rate the connector would
            # have to invent a price, so the schema and the connector both require one.
            if usd_per_gb is None:
                raise ConfigError(
                    "usd_per_gb is required for Cloudflare. Its analytics API reports bytes, not "
                    "dollars, and we will not guess a price."
                )
            if not resource_id:
                raise ConfigError("resource_id (the Cloudflare zone id) is required")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                INSERT INTO tenant_egress_config
                    (tenant_id, egress_provider, credentials_ref, resource_id, usd_per_gb)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, egress_provider) DO UPDATE SET
                    credentials_ref = EXCLUDED.credentials_ref,
                    resource_id = EXCLUDED.resource_id,
                    usd_per_gb = EXCLUDED.usd_per_gb,
                    last_run_at = NULL,
                    last_status = NULL
                """,
                (tenant_id, provider, ref, resource_id, usd_per_gb),
            )
            conn.commit()
        note = None
        if provider == "vercel":
            note = (
                "Vercel egress is now owned by the egress connector. Leave emit_egress off on the "
                "Vercel compute connector so only one path emits."
            )
        return {"connector": connector, "replaced": None, "note": note}

    def _upsert_vercel(self, tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ref = validate_credentials_ref(body.get("access_token_ref"), field="access_token_ref")
        team_id = _opt_str(body.get("team_id"), field="team_id")
        project_id = _opt_str(body.get("project_id"), field="project_id")
        emit_egress = bool(body.get("emit_egress", False))
        enabled = bool(body.get("enabled", True))

        note = None
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            tenant_id = _resolve_tenant_uuid(cur, tenant_id)
            if emit_egress:
                # Defence against the double-count the 0016 header warns about: if CTO-144 already
                # owns Vercel egress for this tenant, refuse the flag rather than let both claim it.
                cur.execute(
                    """
                    SELECT 1 FROM tenant_egress_config
                    WHERE tenant_id = %s AND egress_provider = 'vercel'
                    """,
                    (tenant_id,),
                )
                if cur.fetchone() is not None:
                    raise ConfigError(
                        "emit_egress cannot be enabled while a Vercel egress connector is also "
                        "configured. Disconnect one so exactly one path emits Vercel egress."
                    )
            cur.execute(
                """
                INSERT INTO tenant_vercel_config
                    (tenant_id, access_token_ref, team_id, project_id, enabled, emit_egress)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    access_token_ref = EXCLUDED.access_token_ref,
                    team_id = EXCLUDED.team_id,
                    project_id = EXCLUDED.project_id,
                    enabled = EXCLUDED.enabled,
                    emit_egress = EXCLUDED.emit_egress,
                    last_run_at = NULL,
                    last_status = NULL
                """,
                (tenant_id, ref, team_id, project_id, enabled, emit_egress),
            )
            conn.commit()
        if emit_egress:
            note = "This connector now emits both Vercel compute and Vercel egress spans."
        return {"connector": VERCEL_CONNECTOR, "replaced": None, "note": note}

    # --- delete --------------------------------------------------------------------------------

    def delete(self, tenant_id: str, connector: str) -> bool:
        """Remove a connector's config row. Returns whether a row was actually deleted."""
        if connector not in ALL_CONNECTORS:
            raise ConfigError(f"unknown connector '{connector}'")
        if connector in COMPUTE_CONNECTORS:
            sql = "DELETE FROM tenant_compute_config WHERE tenant_id = %s AND cloud_provider = %s"
            params: tuple[Any, ...] = (tenant_id, COMPUTE_CONNECTORS[connector])
        elif connector in EGRESS_CONNECTORS:
            sql = "DELETE FROM tenant_egress_config WHERE tenant_id = %s AND egress_provider = %s"
            params = (tenant_id, EGRESS_CONNECTORS[connector])
        else:
            sql = "DELETE FROM tenant_vercel_config WHERE tenant_id = %s"
            params = (tenant_id,)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            params = (_resolve_tenant_uuid(cur, tenant_id), *params[1:])
            cur.execute(sql, params)
            deleted = cur.rowcount > 0
            conn.commit()
        return deleted
