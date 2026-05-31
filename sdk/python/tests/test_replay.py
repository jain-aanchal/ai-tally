"""Tests for the context-faithful replay engine (CTO-59)."""

from __future__ import annotations

import pytest

from tally.pricing import Usage, seed_catalog
from tally.replay import (
    CapturedContext,
    CapturedToolCall,
    ReplayConfig,
    ReplayEngine,
    ReplayItem,
    ReplayModel,
    ReplayOutcome,
    ReplayReport,
    ReplayResult,
    select_stratified,
)

# --- Fixtures / stubs ---------------------------------------------------------------------------


def _ctx(*, ref: str = "ctx", tools: tuple[CapturedToolCall, ...] = ()) -> CapturedContext:
    return CapturedContext(
        resolved_messages=("system: you are a bot", "user: hi"),
        retrieved_blobs=("doc-a", "doc-b"),
        tool_calls=tools,
        resolved_context_ref=ref,
    )


def _item(item_id: str, stratum: str = "default", **kw: object) -> ReplayItem:
    return ReplayItem(item_id=item_id, context=_ctx(ref=item_id), stratum=stratum, **kw)


class FixedCostModel:
    """Stub model: constant token usage so cost per replay is deterministic and known."""

    def __init__(self, output_tokens: int = 100) -> None:
        self.output_tokens = output_tokens
        self.calls: list[tuple[str, CapturedContext]] = []

    def __call__(self, model: str, context: CapturedContext) -> ReplayOutcome:
        self.calls.append((model, context))
        return ReplayOutcome(
            output=f"{model}:{context.resolved_context_ref}",
            usage=Usage(input_tokens=1_000, output_tokens=self.output_tokens),
        )


def _config(**kw: object) -> ReplayConfig:
    base: dict[str, object] = {"candidate_models": ("gpt-5-mini",), "seed": "s", "sample_size": 100}
    base.update(kw)
    return ReplayConfig(**base)  # type: ignore[arg-type]


# --- Config validation --------------------------------------------------------------------------


def test_config_rejects_empty_models() -> None:
    with pytest.raises(ValueError):
        ReplayConfig(candidate_models=())


def test_config_rejects_blank_model_name() -> None:
    with pytest.raises(ValueError):
        ReplayConfig(candidate_models=("gpt-5", ""))


def test_config_rejects_negative_sample_size() -> None:
    with pytest.raises(ValueError):
        ReplayConfig(candidate_models=("gpt-5",), sample_size=-1)


def test_config_rejects_negative_caps() -> None:
    with pytest.raises(ValueError):
        ReplayConfig(candidate_models=("gpt-5",), per_comparison_cap_micro_usd=-5)
    with pytest.raises(ValueError):
        ReplayConfig(candidate_models=("gpt-5",), per_tenant_cap_micro_usd=-5)


def test_protocol_runtime_checkable() -> None:
    assert isinstance(FixedCostModel(), ReplayModel)


# --- Stratified selection: determinism + composition --------------------------------------------


def _workload() -> list[ReplayItem]:
    items: list[ReplayItem] = []
    for i in range(60):
        items.append(_item(f"body-{i}", stratum="body"))
    for i in range(30):
        items.append(_item(f"mid-{i}", stratum="mid"))
    for i in range(10):
        items.append(_item(f"tail-{i}", stratum="tail"))
    return items


def test_selection_is_deterministic() -> None:
    wl = _workload()
    a = select_stratified(wl, 20, "seed-x")
    b = select_stratified(wl, 20, "seed-x")
    assert [i.item_id for i in a] == [i.item_id for i in b]


def test_selection_changes_with_seed() -> None:
    wl = _workload()
    a = {i.item_id for i in select_stratified(wl, 20, "seed-x")}
    b = {i.item_id for i in select_stratified(wl, 20, "seed-y")}
    assert a != b


def test_selection_is_stratified_proportional() -> None:
    wl = _workload()  # 60 / 30 / 10
    sel = select_stratified(wl, 20, "seed")
    comp: dict[str, int] = {}
    for it in sel:
        comp[it.stratum] = comp.get(it.stratum, 0) + 1
    assert sum(comp.values()) == 20
    # proportional apportionment of 20 over 60/30/10 → 12/6/2
    assert comp == {"body": 12, "mid": 6, "tail": 2}


def test_sample_larger_than_workload_returns_all() -> None:
    wl = _workload()
    sel = select_stratified(wl, 1_000, "seed")
    assert len(sel) == len(wl)


def test_zero_sample_size_selects_nothing() -> None:
    assert select_stratified(_workload(), 0, "seed") == []


# --- Engine: cost surfacing ---------------------------------------------------------------------


def test_replay_surfaces_integer_micro_usd_cost() -> None:
    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([_item("a"), _item("b")], FixedCostModel())
    # gpt-5-mini: input 0.25/Mtok * 1000 = 250 micro; output 2.00/Mtok * 100 = 200 micro -> 450
    assert isinstance(report.total_replay_cost_micro_usd, int)
    assert report.total_replay_cost_micro_usd == 900  # 2 items * 450
    assert report.per_model_cost_micro_usd == {"gpt-5-mini": 900}
    assert all(isinstance(r.replay_cost_micro_usd, int) for r in report.results)


def test_report_composition_and_counts() -> None:
    engine = ReplayEngine(seed_catalog(), _config(sample_size=20, seed="seed"))
    report = engine.run(_workload(), FixedCostModel())
    assert report.workload_size == 100
    assert report.sample_size == 20
    assert report.sample_composition == {"body": 12, "mid": 6, "tail": 2}
    assert report.replayed_count == 20
    assert report.skipped_by_cap_count == 0


def test_multiple_candidates_fan_out() -> None:
    cfg = _config(candidate_models=("gpt-5-mini", "gpt-5"))
    engine = ReplayEngine(seed_catalog(), cfg)
    report = engine.run([_item("a")], FixedCostModel())
    assert report.replayed_count == 2
    assert set(report.per_model_cost_micro_usd) == {"gpt-5-mini", "gpt-5"}
    # gpt-5 is pricier than gpt-5-mini for identical usage
    assert report.per_model_cost_micro_usd["gpt-5"] > report.per_model_cost_micro_usd["gpt-5-mini"]


# --- Verbatim tool replay (no live execution) ---------------------------------------------------


class ToolRecordingModel:
    """Records the captured tool responses handed to it; never executes a tool itself."""

    def __init__(self) -> None:
        self.seen_tool_responses: list[str] = []

    def __call__(self, model: str, context: CapturedContext) -> ReplayOutcome:
        for tc in context.tool_calls:
            self.seen_tool_responses.append(tc.response)
        return ReplayOutcome(output="ok", usage=Usage(input_tokens=10, output_tokens=10))


def test_tool_responses_replayed_verbatim_not_executed() -> None:
    tool = CapturedToolCall(
        tool_name="search", request="q=cats", response="CAPTURED_RESULT_42"
    )
    item = ReplayItem(item_id="t1", context=_ctx(ref="t1", tools=(tool,)), stratum="default")
    model = ToolRecordingModel()
    engine = ReplayEngine(seed_catalog(), _config())
    engine.run([item], model)
    # The engine handed the captured response through verbatim; it never invoked a live tool.
    assert model.seen_tool_responses == ["CAPTURED_RESULT_42"]


def test_captured_context_injected_not_refetched() -> None:
    model = FixedCostModel()
    item = _item("x")
    ReplayEngine(seed_catalog(), _config()).run([item], model)
    # the exact captured context object is what the model received
    assert model.calls[0][1] is item.context


# --- Cap enforcement ----------------------------------------------------------------------------


def test_per_comparison_cap_stops_admission() -> None:
    # each replay costs 450 micro; cap at 1000 admits 2, skips the 3rd
    cfg = _config(per_comparison_cap_micro_usd=1_000)
    engine = ReplayEngine(seed_catalog(), cfg)
    report = engine.run([_item("a"), _item("b"), _item("c")], FixedCostModel())
    assert report.replayed_count == 2
    assert report.skipped_by_cap_count == 1
    assert report.total_replay_cost_micro_usd == 900
    assert report.bound_by_cap == "per_comparison"
    capped = [r for r in report.results if r.capped]
    assert len(capped) == 1
    assert capped[0].replay_cost_micro_usd == 0


def test_per_tenant_cap_stops_admission() -> None:
    cfg = _config(per_tenant_cap_micro_usd=450, tenant_id="acme")
    engine = ReplayEngine(seed_catalog(), cfg)
    report = engine.run([_item("a"), _item("b")], FixedCostModel())
    assert report.replayed_count == 1
    assert report.skipped_by_cap_count == 1
    assert report.bound_by_cap == "per_tenant"


def test_zero_cap_admits_nothing() -> None:
    cfg = _config(per_comparison_cap_micro_usd=0)
    engine = ReplayEngine(seed_catalog(), cfg)
    report = engine.run([_item("a")], FixedCostModel())
    assert report.replayed_count == 0
    assert report.skipped_by_cap_count == 1


def test_no_cap_runs_everything() -> None:
    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([_item("a"), _item("b"), _item("c")], FixedCostModel())
    assert report.replayed_count == 3
    assert report.bound_by_cap is None


# --- Defensiveness ------------------------------------------------------------------------------


def test_empty_workload_yields_empty_report() -> None:
    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([], FixedCostModel())
    assert isinstance(report, ReplayReport)
    assert report.workload_size == 0
    assert report.sample_size == 0
    assert report.results == ()
    assert report.total_replay_cost_micro_usd == 0


def test_none_workload_is_safe() -> None:
    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run(None, FixedCostModel())
    assert report.workload_size == 0


def test_non_replayitem_entries_ignored() -> None:
    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([_item("a"), "garbage", 42, None, _item("b")], FixedCostModel())
    assert report.workload_size == 2
    assert report.replayed_count == 2


def test_model_that_raises_is_recorded_failed_zero_cost() -> None:
    class Boom:
        def __call__(self, model: str, context: CapturedContext) -> ReplayOutcome:
            raise RuntimeError("model exploded")

    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([_item("a"), _item("b")], Boom())
    assert report.failed_count == 2
    assert report.replayed_count == 2  # they still "ran", just failed
    assert report.total_replay_cost_micro_usd == 0
    assert all(r.failed for r in report.results)


def test_model_returning_garbage_is_failed() -> None:
    class Bad:
        def __call__(self, model: str, context: CapturedContext) -> ReplayOutcome:
            return "not an outcome"  # type: ignore[return-value]

    engine = ReplayEngine(seed_catalog(), _config())
    report = engine.run([_item("a")], Bad())
    assert report.failed_count == 1
    assert report.total_replay_cost_micro_usd == 0


def test_missing_price_yields_zero_cost_not_crash() -> None:
    # unknown model has no catalog entry -> cost 0, no raise
    cfg = _config(candidate_models=("totally-unknown-model",))
    engine = ReplayEngine(seed_catalog(), cfg)
    report = engine.run([_item("a")], FixedCostModel())
    assert report.replayed_count == 1
    assert report.total_replay_cost_micro_usd == 0
    assert report.failed_count == 0


# --- Serialization ------------------------------------------------------------------------------


def test_as_dict_round_trips_shape() -> None:
    engine = ReplayEngine(seed_catalog(), _config(sample_size=5, seed="seed"))
    report = engine.run(_workload(), FixedCostModel())
    d = report.as_dict()
    assert set(d) == {
        "workload_size",
        "sample_size",
        "sample_composition",
        "results",
        "total_replay_cost_micro_usd",
        "per_model_cost_micro_usd",
        "replayed_count",
        "skipped_by_cap_count",
        "failed_count",
        "bound_by_cap",
    }
    assert isinstance(d["results"], list)
    assert isinstance(d["results"][0], dict)


def test_result_as_dict_shape() -> None:
    r = ReplayResult(
        item_id="a", model="m", stratum="body", output="o", replay_cost_micro_usd=5
    )
    assert r.as_dict()["replay_cost_micro_usd"] == 5
    assert r.as_dict()["capped"] is False


def test_captured_context_as_dict() -> None:
    tool = CapturedToolCall(tool_name="t", request="r", response="resp")
    ctx = _ctx(ref="z", tools=(tool,))
    d = ctx.as_dict()
    assert d["resolved_context_ref"] == "z"
    assert d["tool_calls"] == [{"tool_name": "t", "request": "r", "response": "resp"}]
