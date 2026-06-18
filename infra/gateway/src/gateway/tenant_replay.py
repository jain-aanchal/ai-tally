"""Per-tenant opt-in config for the replay sampler + executor (CTO-113).

Sampling captures the resolved prompt + tools — a real trust ask. The default for every tenant is
``enabled=false``; the dashboard surfaces an opt-in toggle. ``daily_budget_usd`` is the hard cap
on the replay executor's spend per tenant per day; it's enforced in
:mod:`gateway.replay_executor` and can't be exceeded by buggy/runaway projections.

Reads/writes go through ``GET/POST /v1/tenant/replay/config`` — the web app never touches
Postgres directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg

from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    enabled: bool
    sample_rate: float
    retention_days: int
    daily_budget_usd: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "retention_days": self.retention_days,
            "daily_budget_usd": float(self.daily_budget_usd),
        }


DEFAULT_CONFIG = ReplayConfig(
    enabled=False, sample_rate=0.05, retention_days=30, daily_budget_usd=Decimal("5.00")
)


class TenantReplayStore:
    """Tiny Postgres surface over ``tenant_replay_config``.

    A tenant with no row yet returns :data:`DEFAULT_CONFIG` (off, 5%, 30d, $5/day). This means the
    gateway never has to provision a row at signup — the row only appears when the tenant
    explicitly opts in.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> ReplayConfig:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, sample_rate, retention_days, daily_budget_usd
                FROM tenant_replay_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return DEFAULT_CONFIG
            return ReplayConfig(
                enabled=bool(row[0]),
                sample_rate=float(row[1]),
                retention_days=int(row[2]),
                daily_budget_usd=Decimal(row[3]),
            )

    def upsert(
        self,
        tenant_id: str,
        *,
        enabled: bool | None = None,
        sample_rate: float | None = None,
        retention_days: int | None = None,
        daily_budget_usd: Decimal | float | None = None,
    ) -> ReplayConfig:
        current = self.get(tenant_id)
        new = ReplayConfig(
            enabled=current.enabled if enabled is None else bool(enabled),
            sample_rate=current.sample_rate if sample_rate is None else float(sample_rate),
            retention_days=current.retention_days if retention_days is None else int(retention_days),
            daily_budget_usd=current.daily_budget_usd
            if daily_budget_usd is None
            else Decimal(str(daily_budget_usd)),
        )
        if not (0.0 <= new.sample_rate <= 1.0):
            raise ValueError("sample_rate must be between 0 and 1")
        if new.retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if new.daily_budget_usd < 0:
            raise ValueError("daily_budget_usd must be non-negative")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_replay_config
                    (tenant_id, enabled, sample_rate, retention_days, daily_budget_usd, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET enabled          = EXCLUDED.enabled,
                      sample_rate      = EXCLUDED.sample_rate,
                      retention_days   = EXCLUDED.retention_days,
                      daily_budget_usd = EXCLUDED.daily_budget_usd,
                      updated_at       = now()
                """,
                (
                    tenant_id,
                    new.enabled,
                    new.sample_rate,
                    new.retention_days,
                    new.daily_budget_usd,
                ),
            )
            conn.commit()
        return new
