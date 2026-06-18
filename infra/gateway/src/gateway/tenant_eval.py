"""Per-tenant opt-in config for the pairwise-LLM-judge eval harness (CTO-114).

Mirror of :mod:`gateway.tenant_replay`, with three differences:

* Default off (same).
* Default daily budget is ``$10`` — judges are pricier than replay candidates.
* ``judge_model`` is a string, not derived. Default ``claude-opus-4-8`` (best capability /
  best at following the rubric's "answer with exactly A, B, or TIE" format). Overridable per
  tenant — e.g. to mitigate judge-self-bias if all candidates are claude-family.

Reads/writes go through ``GET/POST /v1/tenant/eval/config`` — the web app never touches
Postgres directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from gateway.config import Settings


DEFAULT_JUDGE_MODEL = os.environ.get("TALLY_EVAL_JUDGE_MODEL", "claude-opus-4-8")
DEFAULT_JUDGE_PROVIDER = "anthropic"


@dataclass(frozen=True, slots=True)
class EvalConfig:
    enabled: bool
    judge_model: str
    daily_budget_usd: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "judge_model": self.judge_model,
            "daily_budget_usd": float(self.daily_budget_usd),
        }


DEFAULT_CONFIG = EvalConfig(
    enabled=False,
    judge_model=DEFAULT_JUDGE_MODEL,
    daily_budget_usd=Decimal("10.00"),
)


class TenantEvalStore:
    """Tiny Postgres surface over ``tenant_eval_config``.

    A tenant with no row yet returns :data:`DEFAULT_CONFIG` (off, opus-4-8, $10/day). The row
    is only created when the tenant explicitly opts in.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str) -> EvalConfig:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, judge_model, daily_budget_usd
                FROM tenant_eval_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return DEFAULT_CONFIG
            return EvalConfig(
                enabled=bool(row[0]),
                judge_model=str(row[1]),
                daily_budget_usd=Decimal(row[2]),
            )

    def upsert(
        self,
        tenant_id: str,
        *,
        enabled: bool | None = None,
        judge_model: str | None = None,
        daily_budget_usd: Decimal | float | None = None,
    ) -> EvalConfig:
        current = self.get(tenant_id)
        new = EvalConfig(
            enabled=current.enabled if enabled is None else bool(enabled),
            judge_model=current.judge_model if judge_model is None else str(judge_model),
            daily_budget_usd=current.daily_budget_usd
            if daily_budget_usd is None
            else Decimal(str(daily_budget_usd)),
        )
        if not new.judge_model:
            raise ValueError("judge_model must be non-empty")
        if new.daily_budget_usd < 0:
            raise ValueError("daily_budget_usd must be non-negative")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_eval_config
                    (tenant_id, enabled, judge_model, daily_budget_usd, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET enabled          = EXCLUDED.enabled,
                      judge_model      = EXCLUDED.judge_model,
                      daily_budget_usd = EXCLUDED.daily_budget_usd,
                      updated_at       = now()
                """,
                (
                    tenant_id,
                    new.enabled,
                    new.judge_model,
                    new.daily_budget_usd,
                ),
            )
            conn.commit()
        return new
