# SPDX-License-Identifier: Apache-2.0
"""Tests for the cost connector framework + v1 connectors (CTO-63)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tally.cost_connectors import (
    DEFAULT_FEATURE_TAG,
    AWSCostExplorerConnector,
    ConnectorHealth,
    ConnectorRegistry,
    CostIngestRunner,
    CostRecord,
    LLMProxyConnector,
    PineconeConnector,
    TavilyConnector,
    VercelConnector,
    run_connector,
)
from tally.pricing import seed_catalog
from tally.schema import DEFAULT_CURRENCY, micro_to_usd, usd_to_micro

TS = "2026-05-01T12:00:00+00:00"
TS_DT = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)


# --- CostRecord --------------------------------------------------------------------------------


def test_cost_record_as_dict_is_json_friendly():
    rec = CostRecord(
        source="x",
        tenant_id="t1",
        feature_tag="search",
        occurred_at=TS_DT,
        cost_micro_usd=1234,
        quantity=Decimal("5"),
        unit="searches",
        metadata={"k": "v"},
    )
    d = rec.as_dict()
    assert d["occurred_at"] == TS
    assert d["cost_micro_usd"] == 1234
    assert d["currency"] == DEFAULT_CURRENCY
    assert d["quantity"] == "5"
    assert d["metadata"] == {"k": "v"}


def test_cost_record_is_frozen():
    rec = CostRecord(
        source="x", tenant_id="t", feature_tag="f", occurred_at=TS_DT, cost_micro_usd=1
    )
    with pytest.raises(FrozenInstanceError):
        rec.cost_micro_usd = 2  # type: ignore[misc]


# --- LLM proxy connector -----------------------------------------------------------------------


def test_llm_proxy_prices_via_catalog_and_maps_tenant_feature_time():
    conn = LLMProxyConnector(catalog=seed_catalog())
    rows = [
        {
            "provider": "openai",
            "model": "gpt-5-mini",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "feature_tag": "chat",
            "occurred_at": TS,
        }
    ]
    records = conn.parse(rows, tenant_id="tenant-A")
    assert len(records) == 1
    rec = records[0]
    assert rec.tenant_id == "tenant-A"
    assert rec.feature_tag == "chat"
    assert rec.occurred_at == TS_DT
    assert rec.metadata["provider"] == "openai"
    # 1000 input @0.25/Mtok + 500 output @2.00/Mtok = 0.00025 + 0.001 = 0.00125 USD
    assert rec.cost_micro_usd == usd_to_micro(Decimal("0.00125"))
    assert micro_to_usd(rec.cost_micro_usd) == Decimal("0.00125000")


def test_llm_proxy_skips_catalog_miss():
    conn = LLMProxyConnector(catalog=seed_catalog())
    rows = [
        {
            "provider": "openai",
            "model": "no-such-model",
            "prompt_tokens": 100,
            "occurred_at": TS,
        }
    ]
    assert conn.parse(rows, tenant_id="t") == []


def test_llm_proxy_skips_rows_missing_fields():
    conn = LLMProxyConnector(catalog=seed_catalog())
    rows = [
        {"model": "gpt-5-mini", "occurred_at": TS},  # no provider
        {"provider": "openai", "occurred_at": TS},  # no model
        {"provider": "openai", "model": "gpt-5-mini"},  # no timestamp
    ]
    assert conn.parse(rows, tenant_id="t") == []


# --- Pinecone connector ------------------------------------------------------------------------


def test_pinecone_units_to_micro():
    conn = PineconeConnector(read_unit_usd=Decimal("0.0001"), write_unit_usd=Decimal("0.001"))
    rows = [
        {
            "index": "prod-index",
            "read_units": 100,
            "write_units": 10,
            "feature_tag": "rag",
            "occurred_at": TS,
        }
    ]
    records = conn.parse(rows, tenant_id="t")
    assert len(records) == 1
    rec = records[0]
    assert rec.feature_tag == "rag"
    assert rec.occurred_at == TS_DT
    # 100*0.0001 + 10*0.001 = 0.01 + 0.01 = 0.02 USD
    assert rec.cost_micro_usd == usd_to_micro(Decimal("0.02"))
    assert rec.metadata["index"] == "prod-index"


def test_pinecone_falls_back_to_index_as_feature():
    conn = PineconeConnector(read_unit_usd=Decimal("0.0001"), write_unit_usd=Decimal("0.001"))
    rows = [{"index": "idx", "read_units": 1, "occurred_at": TS}]
    records = conn.parse(rows, tenant_id="t")
    assert records[0].feature_tag == "idx"


# --- AWS Cost Explorer connector ---------------------------------------------------------------


def _aws_payload():
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-02"},
                "Groups": [
                    {
                        "Keys": ["feature$checkout"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["feature$search"],
                        "Metrics": {"UnblendedCost": {"Amount": "0.50", "Unit": "USD"}},
                    },
                ],
            }
        ]
    }


def test_aws_cost_explorer_maps_tag_to_feature_and_dollars_to_micro():
    conn = AWSCostExplorerConnector()
    records = conn.parse(_aws_payload(), tenant_id="tenant-A")
    assert len(records) == 2
    by_tag = {r.feature_tag: r for r in records}
    assert by_tag["feature$checkout"].cost_micro_usd == usd_to_micro(Decimal("12.34"))
    assert by_tag["feature$search"].cost_micro_usd == usd_to_micro(Decimal("0.50"))
    assert all(r.tenant_id == "tenant-A" for r in records)
    assert by_tag["feature$checkout"].occurred_at == datetime(
        2026, 5, 1, tzinfo=timezone.utc
    )


def test_aws_skips_group_without_amount():
    payload = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01"},
                "Groups": [{"Keys": ["x"], "Metrics": {"UnblendedCost": {}}}],
            }
        ]
    }
    assert AWSCostExplorerConnector().parse(payload, tenant_id="t") == []


# --- Tavily connector --------------------------------------------------------------------------


def test_tavily_count_times_price():
    conn = TavilyConnector(price_per_call_usd=Decimal("0.005"))
    rows = [{"count": 200, "feature_tag": "research", "occurred_at": TS}]
    records = conn.parse(rows, tenant_id="t")
    assert len(records) == 1
    # 200 * 0.005 = 1.00 USD
    assert records[0].cost_micro_usd == usd_to_micro(Decimal("1.00"))
    assert records[0].quantity == Decimal("200")
    assert records[0].feature_tag == "research"


def test_tavily_row_price_override():
    conn = TavilyConnector(price_per_call_usd=Decimal("0.005"))
    rows = [{"count": 10, "price_per_call_usd": "0.01", "occurred_at": TS}]
    records = conn.parse(rows, tenant_id="t")
    assert records[0].cost_micro_usd == usd_to_micro(Decimal("0.10"))
    assert records[0].feature_tag == DEFAULT_FEATURE_TAG


# --- Vercel connector --------------------------------------------------------------------------


def test_vercel_rates_per_metric():
    conn = VercelConnector(
        rates={"bandwidth": Decimal("0.10"), "function_invocations": Decimal("0.0000002")}
    )
    rows = [
        {"metric": "bandwidth", "quantity": "5", "project": "web", "occurred_at": TS},
        {"metric": "function_invocations", "quantity": "1000000", "occurred_at": TS},
    ]
    records = conn.parse(rows, tenant_id="t")
    assert len(records) == 2
    by_unit = {r.unit: r for r in records}
    assert by_unit["bandwidth"].cost_micro_usd == usd_to_micro(Decimal("0.50"))
    assert by_unit["bandwidth"].feature_tag == "web"
    assert by_unit["function_invocations"].cost_micro_usd == usd_to_micro(Decimal("0.20"))


def test_vercel_skips_unknown_metric():
    conn = VercelConnector(rates={"bandwidth": Decimal("0.10")})
    rows = [{"metric": "mystery", "quantity": "5", "occurred_at": TS}]
    assert conn.parse(rows, tenant_id="t") == []


# --- defensiveness -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conn",
    [
        LLMProxyConnector(catalog=seed_catalog()),
        PineconeConnector(read_unit_usd=Decimal("0.1"), write_unit_usd=Decimal("0.1")),
        AWSCostExplorerConnector(),
        TavilyConnector(price_per_call_usd=Decimal("0.005")),
        VercelConnector(rates={"bandwidth": Decimal("0.1")}),
    ],
)
@pytest.mark.parametrize("raw", [None, "garbage", 42, [], {}, [None, 7, "x"], {"rows": "nope"}])
def test_connectors_never_raise_on_garbage(conn, raw):
    out = conn.parse(raw, tenant_id="t")
    assert list(out) == []


def test_connectors_accept_wrapped_rows_mapping():
    conn = TavilyConnector(price_per_call_usd=Decimal("0.01"))
    out = conn.parse({"data": [{"count": 1, "occurred_at": TS}]}, tenant_id="t")
    assert len(out) == 1


def test_epoch_timestamp_parsed():
    conn = TavilyConnector(price_per_call_usd=Decimal("0.01"))
    epoch = int(TS_DT.timestamp())
    out = conn.parse([{"count": 1, "occurred_at": epoch}], tenant_id="t")
    assert out[0].occurred_at == TS_DT


# --- registry / pluggability -------------------------------------------------------------------


def test_registry_register_and_lookup():
    reg = ConnectorRegistry()
    conn = TavilyConnector(price_per_call_usd=Decimal("0.01"))
    reg.register(conn)
    assert "tavily" in reg
    assert reg.get("tavily") is conn
    assert reg.names() == ["tavily"]
    assert reg.get("missing") is None


def test_custom_connector_needs_no_core_change():
    """A brand-new cost source: a class satisfying the Protocol, registered + run with no change
    to the registry or runner."""

    class S3Connector:
        name = "s3"

        def parse(self, raw, *, tenant_id):
            rows = raw if isinstance(raw, list) else []
            return [
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag="storage",
                    occurred_at=TS_DT,
                    cost_micro_usd=usd_to_micro(Decimal(str(r["usd"]))),
                )
                for r in rows
            ]

    runner = CostIngestRunner()
    runner.register(S3Connector())
    result = runner.run("s3", [[{"usd": "1.50"}]], tenant_id="t", now=NOW)
    assert len(result.records) == 1
    assert result.records[0].cost_micro_usd == usd_to_micro(Decimal("1.50"))
    assert result.health.ok


# --- runner + health ---------------------------------------------------------------------------


def test_run_connector_empty_batch_is_healthy_zero_sync():
    conn = TavilyConnector(price_per_call_usd=Decimal("0.01"))
    result = run_connector(conn, [], tenant_id="t", now=NOW)
    assert result.records == []
    assert result.health.records_emitted == 0
    assert result.health.errors_count == 0
    assert result.health.ok
    assert result.health.last_sync == NOW


def test_runner_records_health_and_last_sync():
    runner = CostIngestRunner()
    runner.register(TavilyConnector(price_per_call_usd=Decimal("0.01")))
    runner.run(
        "tavily",
        [[{"count": 5, "occurred_at": TS}], [{"count": 1, "occurred_at": TS}]],
        tenant_id="t",
        now=NOW,
    )
    health = runner.health("tavily")
    assert isinstance(health, ConnectorHealth)
    assert health.records_emitted == 2
    assert health.last_sync == NOW
    assert health.ok


def test_runner_never_raises_on_malformed_payload_and_counts_error():
    class Exploder:
        name = "boom"

        def parse(self, raw, *, tenant_id):
            raise ValueError("kaboom")

    runner = CostIngestRunner()
    runner.register(Exploder())
    result = runner.run("boom", [{"x": 1}, {"y": 2}], tenant_id="t", now=NOW)
    assert result.records == []
    assert result.health.errors_count == 2
    assert result.health.ok is False
    assert "kaboom" in (result.health.last_error or "")


def test_runner_continues_past_bad_payload():
    """One malformed payload doesn't abort the batch; good ones still emit."""

    class Picky:
        name = "picky"

        def parse(self, raw, *, tenant_id):
            if raw == "bad":
                raise RuntimeError("nope")
            return [
                CostRecord(
                    source=self.name,
                    tenant_id=tenant_id,
                    feature_tag="f",
                    occurred_at=TS_DT,
                    cost_micro_usd=1,
                )
            ]

    runner = CostIngestRunner()
    runner.register(Picky())
    result = runner.run("picky", ["bad", "good"], tenant_id="t", now=NOW)
    assert len(result.records) == 1
    assert result.health.errors_count == 1


def test_runner_non_record_entries_ignored():
    class Sloppy:
        name = "sloppy"

        def parse(self, raw, *, tenant_id):
            return ["not a record", 42, None]

    runner = CostIngestRunner()
    runner.register(Sloppy())
    result = runner.run("sloppy", [{}], tenant_id="t", now=NOW)
    assert result.records == []
    assert result.health.errors_count == 0


def test_runner_unknown_connector_flags_error_not_raise():
    runner = CostIngestRunner()
    result = runner.run("ghost", [{}], tenant_id="t", now=NOW)
    assert result.records == []
    assert result.health.ok is False
    assert "ghost" in (result.health.last_error or "")


def test_runner_health_for_never_run_connector_is_healthy_zero_sync():
    runner = CostIngestRunner()
    health = runner.health("never")
    assert isinstance(health, ConnectorHealth)
    assert health.last_sync is None
    assert health.records_emitted == 0
    assert health.ok


def test_runner_run_all_keyed_by_name():
    runner = CostIngestRunner()
    runner.register(TavilyConnector(price_per_call_usd=Decimal("0.01")))
    runner.register(
        PineconeConnector(read_unit_usd=Decimal("0.001"), write_unit_usd=Decimal("0.001"))
    )
    records = runner.run_all(
        {
            "tavily": [[{"count": 1, "occurred_at": TS}]],
            "pinecone": [[{"read_units": 10, "occurred_at": TS}]],
        },
        tenant_id="t",
        now=NOW,
    )
    sources = {r.source for r in records}
    assert sources == {"tavily", "pinecone"}
    all_health = runner.health()
    assert isinstance(all_health, dict)
    assert set(all_health) == {"tavily", "pinecone"}


def test_health_as_dict():
    h = ConnectorHealth(name="x", last_sync=NOW, records_emitted=3, errors_count=0)
    d = h.as_dict()
    assert d["name"] == "x"
    assert d["last_sync"] == NOW.isoformat()
    assert d["ok"] is True
