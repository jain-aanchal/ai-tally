"""Replay engine — context-faithful pre-deploy model comparison, no live retrieval.

Implements CTO-59.

Pre-deploy model comparison is worthless if the replayed prompt's retrieved context differs from
the original. Quality and cost deltas would then be noise rather than signal. So replay injects the
EXACT captured context from the original trace (a ``ResolvedContextRef``) and bypasses live
retrieval entirely: the resolved prompt, retrieved-context blobs, and captured tool-call responses
are all replayed verbatim from the original span events. No network, no live LLM call, no live tool
execution happens in this module — the model is an INJECTED callable the caller supplies (exactly
like the judge callable in evals and the model in sampling), which keeps the engine pure and tests
deterministic and offline.

Sampling is STRATIFIED (not random) and deterministic: the same workload + seed always selects the
same items, reusing the seeded-hash approach from :mod:`tally.sampling`. We sample stratified
because prod prompts are not uniform — cost bands / features must each be represented so a candidate
model is judged on the real mix, not whatever a uniform draw happened to surface.

Execution is sandboxed by cost: a per-comparison cap and a per-tenant cap are enforced. Once a cap
would be exceeded the engine stops admitting further replays gracefully (it reports how many ran vs.
were skipped — it never raises). Replay cost is surfaced as integer micro-USD throughout (Decimal
for any rate math, never float dollars — mirrors :mod:`tally.pricing` / :mod:`tally.schema`).

Clean seams (out of scope here):
- Rate-limit governance lives in ``tally.governor`` (CTO-60); this engine does not couple to it.
- Eval scoring lives in ``tally.evals`` (CTO-61); :class:`ReplayResult` carries raw output for it.
- Comparison UI / projection (CTO-62 / CTO-72) consume :meth:`ReplayReport.as_dict` only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tally.pricing import PriceCatalog, Usage, compute_cost_micro_usd

# --- Captured context (the ResolvedContextRef payload) ------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedToolCall:
    """One tool call replayed VERBATIM from a captured span event.

    The ``response`` is the exact output the tool produced on the original trace. The engine never
    re-executes the tool — it hands these captured responses to the injected model unchanged.
    """

    tool_name: str
    request: str
    response: str


@dataclass(frozen=True, slots=True)
class CapturedContext:
    """The ``ResolvedContextRef`` payload injected in place of live retrieval.

    Carries the resolved prompt messages, the retrieved-context blobs, and the captured tool-call
    responses. This is the *whole* faithful context; nothing here is re-fetched at replay time.
    """

    resolved_messages: tuple[str, ...] = ()
    retrieved_blobs: tuple[str, ...] = ()
    tool_calls: tuple[CapturedToolCall, ...] = ()
    resolved_context_ref: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved_messages": list(self.resolved_messages),
            "retrieved_blobs": list(self.retrieved_blobs),
            "tool_calls": [
                {"tool_name": t.tool_name, "request": t.request, "response": t.response}
                for t in self.tool_calls
            ],
            "resolved_context_ref": self.resolved_context_ref,
        }


@dataclass(frozen=True, slots=True)
class ReplayItem:
    """One prompt to replay: an id, its captured context, and a stratum signal.

    ``stratum`` is the stratification key (e.g. a cost band or feature tag). ``original_model`` and
    ``original_cost_micro_usd`` are optional and only carried through for downstream delta work.
    """

    item_id: str
    context: CapturedContext
    stratum: str = "default"
    original_model: str | None = None
    original_cost_micro_usd: int | None = None


# --- Injected model (never hits a network) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """What the injected model produced for one replay: output text + token usage.

    The engine prices the usage via the catalog; ``ReplayOutcome`` itself carries no cost so the
    model stub stays trivial. ``provider`` lets the engine look up the right rate.
    """

    output: str
    usage: Usage
    provider: str = "openai"


@runtime_checkable
class ReplayModel(Protocol):
    """Injected, offline model. Given a candidate ``model`` and the captured context, return a
    :class:`ReplayOutcome`. Implementations MUST NOT perform live retrieval or tool execution — the
    captured tool responses on ``context`` are authoritative. May raise; the engine absorbs it."""

    def __call__(self, model: str, context: CapturedContext) -> ReplayOutcome: ...


# --- Configuration ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Candidate models, deterministic stratified-sample knobs, and cost caps.

    A cap of ``None`` means unbounded. ``sample_size`` is the target number of items to replay
    (across all strata) before per-candidate fan-out; larger than the workload replays everything.
    """

    candidate_models: tuple[str, ...]
    sample_size: int = 50
    seed: str = "replay"
    per_comparison_cap_micro_usd: int | None = None
    per_tenant_cap_micro_usd: int | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_models:
            raise ValueError("candidate_models must not be empty")
        if any(not m for m in self.candidate_models):
            raise ValueError("candidate_models must not contain empty model names")
        if self.sample_size < 0:
            raise ValueError(f"sample_size must be >= 0, got {self.sample_size}")
        for name in ("per_comparison_cap_micro_usd", "per_tenant_cap_micro_usd"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0 or None, got {v}")


# --- Results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Per-item, per-candidate replay outcome."""

    item_id: str
    model: str
    stratum: str
    output: str
    replay_cost_micro_usd: int
    capped: bool = False
    failed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "model": self.model,
            "stratum": self.stratum,
            "output": self.output,
            "replay_cost_micro_usd": self.replay_cost_micro_usd,
            "capped": self.capped,
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Aggregate report for one replay run across the selected sample and all candidate models."""

    workload_size: int
    sample_size: int
    sample_composition: dict[str, int]
    results: tuple[ReplayResult, ...]
    total_replay_cost_micro_usd: int
    per_model_cost_micro_usd: dict[str, int]
    replayed_count: int
    skipped_by_cap_count: int
    failed_count: int
    bound_by_cap: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "workload_size": self.workload_size,
            "sample_size": self.sample_size,
            "sample_composition": dict(self.sample_composition),
            "results": [r.as_dict() for r in self.results],
            "total_replay_cost_micro_usd": self.total_replay_cost_micro_usd,
            "per_model_cost_micro_usd": dict(self.per_model_cost_micro_usd),
            "replayed_count": self.replayed_count,
            "skipped_by_cap_count": self.skipped_by_cap_count,
            "failed_count": self.failed_count,
            "bound_by_cap": self.bound_by_cap,
        }


# --- Deterministic seeded-hash selection (mirrors tally.sampling) -------------------------------


def _unit_hash(seed: str, key: str) -> float:
    """Map ``(seed, key)`` deterministically to a float in [0, 1). Same input → same output."""
    digest = hashlib.sha256(f"{seed}\x00{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def select_stratified(
    items: list[ReplayItem], sample_size: int, seed: str
) -> list[ReplayItem]:
    """Pick a deterministic, stratified sample of ``items``.

    Each stratum receives a share of ``sample_size`` proportional to its size (largest-remainder
    apportionment, so the total lands exactly on the target). Within a stratum, items are ranked by
    a seeded hash and the top-k taken — deterministic for a given (workload, seed). A sample size
    that meets or exceeds the workload returns everything (in stratum order for stable output).
    """
    if sample_size <= 0:
        return []

    strata: dict[str, list[ReplayItem]] = {}
    for it in items:
        strata.setdefault(it.stratum, []).append(it)

    total = len(items)
    if sample_size >= total:
        # everything is selected; emit in stable stratum order for reproducible reports
        out: list[ReplayItem] = []
        for stratum in sorted(strata):
            out.extend(sorted(strata[stratum], key=lambda i: _unit_hash(seed, i.item_id)))
        return out

    # Largest-remainder apportionment of sample_size across strata, proportional to size.
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    assigned = 0
    for stratum in sorted(strata):
        exact = sample_size * len(strata[stratum]) / total
        base = int(exact)
        quotas[stratum] = base
        assigned += base
        remainders.append((exact - base, stratum))

    # distribute the leftover seats to the largest remainders (ties broken by stratum name)
    leftover = sample_size - assigned
    remainders.sort(key=lambda r: (-r[0], r[1]))
    for i in range(leftover):
        quotas[remainders[i][1]] += 1

    selected: list[ReplayItem] = []
    for stratum in sorted(strata):
        ranked = sorted(strata[stratum], key=lambda i: _unit_hash(seed, i.item_id))
        selected.extend(ranked[: quotas[stratum]])
    return selected


def _compose(items: list[ReplayItem]) -> dict[str, int]:
    """Per-stratum counts for a selection (sorted for stable reporting)."""
    comp: dict[str, int] = {}
    for it in items:
        comp[it.stratum] = comp.get(it.stratum, 0) + 1
    return {k: comp[k] for k in sorted(comp)}


# --- Engine -------------------------------------------------------------------------------------


class ReplayEngine:
    """Context-faithful replay engine. Pure-logic: the model is injected, nothing is re-fetched.

    Selects a deterministic stratified sample, replays each selected item against every candidate
    model via the injected model callable, accumulates replay cost, and enforces the per-comparison
    and per-tenant caps. It never raises on a bad workload, a missing price, or a model that throws.
    """

    def __init__(self, catalog: PriceCatalog, config: ReplayConfig) -> None:
        self.catalog = catalog
        self.config = config

    def run(self, workload: object, model: ReplayModel) -> ReplayReport:
        items = [it for it in _iter(workload) if isinstance(it, ReplayItem)]
        selected = select_stratified(items, self.config.sample_size, self.config.seed)
        composition = _compose(selected)

        results: list[ReplayResult] = []
        per_model: dict[str, int] = {m: 0 for m in self.config.candidate_models}
        comparison_spend = 0
        tenant_spend = 0
        replayed = 0
        skipped = 0
        failed = 0
        bound_by: str | None = None

        comp_cap = self.config.per_comparison_cap_micro_usd
        tenant_cap = self.config.per_tenant_cap_micro_usd

        for item in selected:
            for candidate in self.config.candidate_models:
                cost, output, did_fail = self._replay_one(candidate, item, model)

                # Admission control: a replay is admitted only if it keeps BOTH caps satisfied.
                would_comparison = comparison_spend + cost
                would_tenant = tenant_spend + cost
                over_comparison = comp_cap is not None and would_comparison > comp_cap
                over_tenant = tenant_cap is not None and would_tenant > tenant_cap

                if over_comparison or over_tenant:
                    bound_by = "per_comparison" if over_comparison else "per_tenant"
                    skipped += 1
                    results.append(
                        ReplayResult(
                            item_id=item.item_id,
                            model=candidate,
                            stratum=item.stratum,
                            output="",
                            replay_cost_micro_usd=0,
                            capped=True,
                        )
                    )
                    continue

                comparison_spend = would_comparison
                tenant_spend = would_tenant
                per_model[candidate] = per_model.get(candidate, 0) + cost
                replayed += 1
                if did_fail:
                    failed += 1
                results.append(
                    ReplayResult(
                        item_id=item.item_id,
                        model=candidate,
                        stratum=item.stratum,
                        output=output,
                        replay_cost_micro_usd=cost,
                        failed=did_fail,
                    )
                )

        return ReplayReport(
            workload_size=len(items),
            sample_size=len(selected),
            sample_composition=composition,
            results=tuple(results),
            total_replay_cost_micro_usd=comparison_spend,
            per_model_cost_micro_usd=per_model,
            replayed_count=replayed,
            skipped_by_cap_count=skipped,
            failed_count=failed,
            bound_by_cap=bound_by,
        )

    def _replay_one(
        self, candidate: str, item: ReplayItem, model: ReplayModel
    ) -> tuple[int, str, bool]:
        """Invoke the injected model for one (item, candidate) and price it. Never raises.

        Returns ``(cost_micro_usd, output, failed)``. A model that throws yields zero cost and an
        empty output, recorded as failed — the run continues.
        """
        try:
            outcome = model(candidate, item.context)
        except Exception:
            return 0, "", True
        if not isinstance(outcome, ReplayOutcome):
            return 0, "", True
        try:
            cost, _version = compute_cost_micro_usd(
                self.catalog,
                outcome.provider,
                candidate,
                outcome.usage,
                tenant_id=self.config.tenant_id,
            )
        except Exception:
            cost = 0
        return cost, outcome.output, False


def _iter(workload: object) -> list[object]:
    """Coerce an arbitrary workload to a list, never raising on a non-iterable."""
    if workload is None:
        return []
    if isinstance(workload, (str, bytes)):
        return []
    try:
        return list(workload)  # type: ignore[arg-type]
    except TypeError:
        return []
