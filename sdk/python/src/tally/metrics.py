"""Operational metrics computed over spans.

Implements CTO-83: ``agent_run.cross_process_ratio`` — the trigger metric for promoting a tenant's
guardrails from v1 (single-process) to v2 (a shared counter).

A single logical agent run is identified by ``AgentRunId``. If the spans for one run arrive from
more than one process (distinct ``ServiceName`` / process id), that run is *distributed* and the
v1 per-process guardrail counters under-count it. We measure the fraction of distributed runs per
tenant; when it crosses a threshold (~5%), that tenant is a v2 candidate.

Pure computation over span dicts — no infra needed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# span keys we read (support both promoted ClickHouse names and lowercase wire names)
_AGENT_RUN_KEYS = ("AgentRunId", "gen_ai.agent.run_id", "agent_run_id")
_SERVICE_KEYS = ("ServiceName", "service.name", "service_name")
_PROCESS_KEYS = ("ProcessId", "process.pid", "process_id")
_TENANT_KEYS = ("TenantId", "tenant_id")


def _first(span: dict[str, object], keys: tuple[str, ...]) -> object | None:
    for k in keys:
        if k in span and span[k] not in (None, ""):
            return span[k]
    return None


def _process_identity(span: dict[str, object]) -> object:
    """What distinguishes one process from another for a given run."""
    pid = _first(span, _PROCESS_KEYS)
    svc = _first(span, _SERVICE_KEYS)
    return (svc, pid)


@dataclass(frozen=True, slots=True)
class CrossProcessReport:
    tenant_id: str
    total_runs: int
    distributed_runs: int
    threshold: float

    @property
    def ratio(self) -> float:
        return self.distributed_runs / self.total_runs if self.total_runs else 0.0

    @property
    def exceeds_threshold(self) -> bool:
        return self.ratio >= self.threshold


def cross_process_ratio(
    spans: Iterable[dict[str, object]],
    *,
    tenant_id: str,
    threshold: float = 0.05,
) -> CrossProcessReport:
    """Compute the distributed-run ratio for one tenant from its spans.

    A run counts as distributed when its spans span >1 distinct (service, process) identity. Spans
    without an AgentRunId are ignored (not part of an agent run).
    """
    runs: dict[object, set[object]] = {}
    for span in spans:
        if _first(span, _TENANT_KEYS) not in (None, tenant_id):
            continue  # not this tenant
        run_id = _first(span, _AGENT_RUN_KEYS)
        if run_id is None:
            continue
        runs.setdefault(run_id, set()).add(_process_identity(span))

    total = len(runs)
    distributed = sum(1 for procs in runs.values() if len(procs) > 1)
    return CrossProcessReport(
        tenant_id=tenant_id,
        total_runs=total,
        distributed_runs=distributed,
        threshold=threshold,
    )
