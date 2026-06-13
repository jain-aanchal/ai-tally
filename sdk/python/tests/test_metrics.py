# SPDX-License-Identifier: Apache-2.0
from tally.metrics import cross_process_ratio


def _span(run, service, pid, tenant="t1"):
    return {"TenantId": tenant, "AgentRunId": run, "ServiceName": service, "ProcessId": pid}


def test_all_single_process_ratio_zero():
    spans = [
        _span("r1", "api", 1),
        _span("r1", "api", 1),
        _span("r2", "api", 1),
    ]
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.total_runs == 2
    assert rep.distributed_runs == 0
    assert rep.ratio == 0.0
    assert rep.exceeds_threshold is False


def test_distributed_run_detected():
    spans = [
        _span("r1", "api", 1),
        _span("r1", "worker", 2),  # same run, different service/process → distributed
        _span("r2", "api", 1),
    ]
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.distributed_runs == 1
    assert rep.ratio == 0.5
    assert rep.exceeds_threshold is True


def test_threshold_boundary():
    # 1 distributed of 20 = 5% → exactly meets default 0.05
    spans = []
    spans += [_span("d", "api", 1), _span("d", "worker", 2)]
    for i in range(19):
        spans.append(_span(f"s{i}", "api", 1))
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.total_runs == 20
    assert rep.distributed_runs == 1
    assert abs(rep.ratio - 0.05) < 1e-9
    assert rep.exceeds_threshold is True


def test_spans_without_run_ignored():
    spans = [
        {"TenantId": "t1", "ServiceName": "api"},  # no AgentRunId
        _span("r1", "api", 1),
    ]
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.total_runs == 1


def test_other_tenant_excluded():
    spans = [
        _span("r1", "api", 1, tenant="t1"),
        _span("r1", "worker", 2, tenant="t1"),
        _span("r2", "api", 1, tenant="other"),
        _span("r2", "worker", 9, tenant="other"),
    ]
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.total_runs == 1
    assert rep.distributed_runs == 1


def test_lowercase_wire_keys_supported():
    spans = [
        {"tenant_id": "t1", "gen_ai.agent.run_id": "r1", "service.name": "api", "process.pid": 1},
        {"tenant_id": "t1", "gen_ai.agent.run_id": "r1", "service.name": "api", "process.pid": 2},
    ]
    rep = cross_process_ratio(spans, tenant_id="t1")
    assert rep.distributed_runs == 1


def test_empty_is_zero_not_crash():
    rep = cross_process_ratio([], tenant_id="t1")
    assert rep.total_runs == 0
    assert rep.ratio == 0.0
    assert rep.exceeds_threshold is False
