"""Per-tenant third-party integration status (CTO-117).

The /connectors page in the web app shows a card per third-party integration (Stripe, Segment,
HubSpot, Pendo, …) with three honest states: connected-and-healthy, connected-and-failing, or
not-connected. The status comes from this module — workers (Stripe webhook handler today; the
Segment / HubSpot / Pendo pollers as they land) call :meth:`TenantIntegrationStore.record_run`
after each cycle, and the dashboard reads :meth:`TenantIntegrationStore.get_status` to render
the cards.

Why a separate module from :mod:`gateway.tenant_connectors` — that's the *cost-layer* declaration
(CTO-107), which says "this tenant has the LLM cost connector enabled". This module is the
*third-party integration* run log, which says "the Stripe webhook last fired 12s ago, 1 event".
They look similar but answer different questions, so they live in different tables.

PII scrubbing on ``last_run_error_message``: third-party errors sometimes echo user emails
verbatim (Stripe and HubSpot are repeat offenders). We reuse the validation module's email
regex + forbidden-key list so a single ``"failed for foo@bar.com"`` never lands on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import psycopg

from gateway.config import Settings
from gateway.validation import _EMAIL_RE, _FORBIDDEN_PII_KEYS

# The set of third-party integrations we track. Cost-layer connectors (llm/vector/tools/…) are
# a separate concern — see :mod:`gateway.tenant_connectors`. New integrations must be added to
# the CHECK constraint in 0007_tenant_integration_runs.sql in lockstep.
ALLOWED_CONNECTORS: frozenset[str] = frozenset(
    {"stripe", "segment", "hubspot", "pendo", "rudderstack"}
)

RunStatus = Literal["success", "partial", "failed"]
_ALLOWED_STATUSES: frozenset[str] = frozenset({"success", "partial", "failed"})

# Max length we persist for an error message. Errors longer than this are truncated — the UI
# only ever shows the first ~80 chars anyway, so the rest is log noise.
_ERROR_MAX_LEN = 500


@dataclass(frozen=True, slots=True)
class IntegrationStatus:
    """The dashboard's view of one (tenant, connector) row. Empty on a fresh tenant."""

    connector_id: str
    last_run_at: str
    last_run_status: RunStatus
    last_run_event_count: int
    last_run_error_message: str | None
    total_events_24h: int
    total_events_7d: int

    def as_dict(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "last_run_at": self.last_run_at,
            "last_run_status": self.last_run_status,
            "last_run_event_count": self.last_run_event_count,
            "last_run_error_message": self.last_run_error_message,
            "total_events_24h": self.total_events_24h,
            "total_events_7d": self.total_events_7d,
        }


def scrub_error_message(msg: str | None) -> str | None:
    """Strip raw PII out of a third-party error message before persisting.

    Two passes:

    * If the message contains a forbidden-key marker (``email=``, ``user.email``, ``phone``…)
      we collapse the message to a coarse ``[redacted: contained PII key]`` rather than try to
      parse-and-strip — too easy to get wrong.
    * Replace anything matching the e-mail regex with ``[redacted-email]``. Stripe / HubSpot
      error bodies sometimes embed the customer's email; that must never reach storage.

    Idempotent and side-effect-free; ``None`` and ``""`` pass through untouched.
    """
    if msg is None:
        return None
    s = str(msg).strip()
    if not s:
        return None
    lowered = s.lower()
    for key in _FORBIDDEN_PII_KEYS:
        # Word-ish boundaries so we don't trip on legitimate substrings (e.g. "femaily" or
        # "user_idle") — paranoid but cheap.
        if re.search(rf"(^|[^a-z0-9_]){re.escape(key)}([^a-z0-9_]|=|:|$)", lowered):
            return "[redacted: contained PII key]"
    s = _EMAIL_RE.sub("[redacted-email]", s)
    if len(s) > _ERROR_MAX_LEN:
        s = s[: _ERROR_MAX_LEN - 1].rstrip() + "…"
    return s


class TenantIntegrationStore:
    """Tiny Postgres-backed CRUD over ``tenant_integration_runs``.

    Every method is tenant-scoped — the SQL takes ``tenant_id`` from upstream auth so a buggy
    caller can't accidentally cross tenants.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get_status(self, tenant_id: str) -> list[IntegrationStatus]:
        """Return one row per (tenant, connector) the tenant has ever had a run for.

        Empty list when nothing has run yet — the dashboard renders that as "Not connected"
        across the board, which is the honest first-render state for a fresh tenant.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT connector_id, last_run_at, last_run_status, last_run_event_count,
                       last_run_error_message, total_events_24h, total_events_7d
                FROM tenant_integration_runs
                WHERE tenant_id = %s
                ORDER BY connector_id
                """,
                (tenant_id,),
            )
            return [
                IntegrationStatus(
                    connector_id=str(row[0]),
                    last_run_at=row[1].isoformat(),
                    last_run_status=row[2],  # CHECK constraint guarantees the literal
                    last_run_event_count=int(row[3]),
                    last_run_error_message=row[4],
                    total_events_24h=int(row[5]),
                    total_events_7d=int(row[6]),
                )
                for row in cur.fetchall()
            ]

    def record_run(
        self,
        tenant_id: str,
        connector_id: str,
        status: RunStatus,
        *,
        event_count: int = 0,
        error_message: str | None = None,
    ) -> IntegrationStatus:
        """Stamp the outcome of one worker / webhook cycle.

        Upserts the (tenant, connector) row. ``event_count`` is added to ``total_events_24h``
        and ``total_events_7d`` — we leave the trailing-window decay to a periodic vacuum job
        (out of scope for this ticket); over-counting in the dashboard is preferable to
        under-counting because it stays directionally honest about activity.

        ``error_message`` is run through :func:`scrub_error_message` *before* the SQL parameter
        is bound, so raw PII never reaches the database driver.
        """
        if connector_id not in ALLOWED_CONNECTORS:
            raise ValueError(f"unknown connector_id '{connector_id}'")
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"unknown status '{status}'")
        if event_count < 0:
            raise ValueError("event_count must be non-negative")
        scrubbed = scrub_error_message(error_message)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_integration_runs
                    (tenant_id, connector_id, last_run_at, last_run_status,
                     last_run_event_count, last_run_error_message,
                     total_events_24h, total_events_7d)
                VALUES (%s, %s, now(), %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, connector_id) DO UPDATE
                  SET last_run_at = now(),
                      last_run_status = EXCLUDED.last_run_status,
                      last_run_event_count = EXCLUDED.last_run_event_count,
                      last_run_error_message = EXCLUDED.last_run_error_message,
                      total_events_24h =
                          tenant_integration_runs.total_events_24h
                          + EXCLUDED.last_run_event_count,
                      total_events_7d =
                          tenant_integration_runs.total_events_7d
                          + EXCLUDED.last_run_event_count
                RETURNING connector_id, last_run_at, last_run_status, last_run_event_count,
                          last_run_error_message, total_events_24h, total_events_7d
                """,
                (
                    tenant_id,
                    connector_id,
                    status,
                    event_count,
                    scrubbed,
                    event_count,
                    event_count,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            assert row is not None
            return IntegrationStatus(
                connector_id=str(row[0]),
                last_run_at=row[1].isoformat(),
                last_run_status=row[2],
                last_run_event_count=int(row[3]),
                last_run_error_message=row[4],
                total_events_24h=int(row[5]),
                total_events_7d=int(row[6]),
            )
