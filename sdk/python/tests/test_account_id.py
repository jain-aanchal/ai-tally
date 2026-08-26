# SPDX-License-Identifier: Apache-2.0
"""Account dimension — context scoping, per-call override, and hashing (CTO-181 / B2).

The two properties worth guarding hardest:

1. **A raw account id never reaches a span.** Everything downstream of this SDK assumes the
   telemetry store holds no customer identifiers, so the tests assert on the absence of the
   plaintext, not only on the presence of the digest.
2. **The same account id under different tenant keys hashes differently.** That is the property
   that makes an account hash non-joinable across tenants. If it ever regresses, two of our
   customers could correlate their end customers through us.
"""

from __future__ import annotations

import logging

import pytest

from tally.client import MemoryExporter, TallyClient
from tally.context import current_context, start_trace, with_account, with_trace_context
from tally.hmac_keys import HmacKeyRegistry
from tally.pricing import Usage
from tally.sampling import Sampler, SamplingConfig
from tally.schema import GenAI, SpanFields, build_span_attributes, validate_span_attributes

ACCOUNT = "acct_northwind"


def _keep_everything() -> Sampler:
    """Sampling is orthogonal here; keep every span so the assertions are about attribution."""
    return Sampler(SamplingConfig(body_rate=1.0))


def _client(tenant_id: str = "tenant-a") -> tuple[TallyClient, MemoryExporter, HmacKeyRegistry]:
    registry = HmacKeyRegistry()
    registry.provision(tenant_id)
    exporter = MemoryExporter()
    client = TallyClient(
        exporter=exporter,
        tenant_id=tenant_id,
        hmac_registry=registry,
        sampler=_keep_everything(),
    )
    return client, exporter, registry


def _usage() -> Usage:
    return Usage(input_tokens=100, output_tokens=50)


# --- schema ---------------------------------------------------------------------------------


def test_schema_keys_are_conformant():
    attrs = build_span_attributes(
        SpanFields(
            account_id_hash="a" * 64,
            account_id_hash_key_version="v1",
            account_label="Northwind Traders",
        )
    )
    assert attrs[GenAI.ACCOUNT_ID_HASH] == "a" * 64
    assert attrs[GenAI.ACCOUNT_ID_HASH_KEY_VERSION] == "v1"
    assert attrs[GenAI.ACCOUNT_LABEL] == "Northwind Traders"
    assert validate_span_attributes(attrs) == []


def test_schema_keys_are_namespaced_under_gen_ai():
    assert GenAI.ACCOUNT_ID_HASH == "gen_ai.account_id_hash"
    assert GenAI.ACCOUNT_ID_HASH_KEY_VERSION == "gen_ai.account_id_hash_key_version"
    assert GenAI.ACCOUNT_LABEL == "gen_ai.account_label"


def test_account_hash_must_be_a_non_empty_string():
    assert validate_span_attributes({GenAI.ACCOUNT_ID_HASH: ""}) != []
    assert validate_span_attributes({GenAI.ACCOUNT_ID_HASH: 7}) != []


# --- hashing --------------------------------------------------------------------------------


def test_hash_account_is_stamped_with_the_active_key_version():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    stamped = reg.hash_account("t1", ACCOUNT)
    assert stamped.key_version == "v1"
    assert len(stamped.value) == 64
    assert ACCOUNT not in stamped.value


def test_same_account_different_tenants_hashes_differently():
    """The non-joinability guarantee. Two tenants must not be able to correlate an account."""
    reg = HmacKeyRegistry()
    reg.provision("tenant-a")
    reg.provision("tenant-b")
    a = reg.hash_account("tenant-a", ACCOUNT)
    b = reg.hash_account("tenant-b", ACCOUNT)
    assert a.value != b.value


def test_same_account_same_tenant_is_stable():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    assert reg.hash_account("t1", ACCOUNT).value == reg.hash_account("t1", ACCOUNT).value


def test_rotation_changes_the_hash_and_the_stamp():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    before = reg.hash_account("t1", ACCOUNT)
    reg.rotate("t1")
    after = reg.hash_account("t1", ACCOUNT)
    assert (before.value, before.key_version) != (after.value, after.key_version)
    assert after.key_version == "v2"


def test_empty_account_id_is_rejected():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    with pytest.raises(ValueError):
        reg.hash_account("t1", "")


# --- context scoping ------------------------------------------------------------------------


def test_context_is_unset_by_default():
    ctx = current_context()
    assert ctx.account_id is None
    assert ctx.account_label is None


def test_with_account_sets_and_restores():
    with with_account(ACCOUNT, label="Northwind Traders"):
        assert current_context().account_id == ACCOUNT
        assert current_context().account_label == "Northwind Traders"
    assert current_context().account_id is None


def test_with_account_leaves_the_trace_alone():
    with with_trace_context(trace_id="t1", feature_tag="research"):
        with with_account(ACCOUNT):
            ctx = current_context()
            assert ctx.trace_id == "t1"
            assert ctx.feature_tag == "research"
            assert ctx.account_id == ACCOUNT


def test_with_account_none_clears_for_the_block():
    """The opt-out for a background job that must not inherit its caller's account."""
    with with_account(ACCOUNT):
        with with_account(None):
            assert current_context().account_id is None
        assert current_context().account_id == ACCOUNT


def test_label_does_not_survive_a_cleared_account():
    with with_account(ACCOUNT, label="Northwind"):
        with with_account(None):
            assert current_context().account_label is None


def test_trace_context_inherits_the_account():
    with with_account(ACCOUNT):
        with with_trace_context(trace_id="t1"):
            assert current_context().account_id == ACCOUNT


def test_start_trace_carries_the_account():
    with start_trace(account_id=ACCOUNT, account_label="Northwind"):
        assert current_context().account_id == ACCOUNT
        assert current_context().account_label == "Northwind"


def test_start_trace_does_not_inherit_an_outer_account():
    with with_account(ACCOUNT):
        with start_trace():
            assert current_context().account_id is None


# --- client emission ------------------------------------------------------------------------


def test_llm_call_picks_up_the_scoped_account():
    client, exporter, registry = _client()
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())

    attrs = exporter.spans[0]
    assert attrs[GenAI.ACCOUNT_ID_HASH] == registry.hash_account("tenant-a", ACCOUNT).value
    assert attrs[GenAI.ACCOUNT_ID_HASH_KEY_VERSION] == "v1"
    assert validate_span_attributes(attrs) == []


def test_raw_account_id_never_reaches_the_span():
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT, label="Northwind Traders"):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())

    serialized = repr(exporter.spans[0])
    assert ACCOUNT not in serialized


def test_every_span_in_the_scope_is_tagged():
    """The ergonomic claim of the API shape: set it once, everything inside inherits."""
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
        client.record_tool_call(provider="openai", tool="web_search", cost_micro_usd=10)
        client.record_embedding_call(provider="openai", model="text-embedding-3-small",
                                     input_tokens=10)
        client.record_vector_call(provider="pinecone", index="docs", operation="query",
                                  cost_micro_usd=1)

    assert len(exporter.spans) == 4
    hashes = {s.get(GenAI.ACCOUNT_ID_HASH) for s in exporter.spans}
    assert len(hashes) == 1 and None not in hashes


def test_per_call_override_beats_the_scope():
    client, exporter, registry = _client()
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(
            provider="openai", model="gpt-4o", usage=_usage(), account_id="acct_other"
        )
    assert exporter.spans[0][GenAI.ACCOUNT_ID_HASH] == (
        registry.hash_account("tenant-a", "acct_other").value
    )


def test_per_call_account_needs_no_scope():
    client, exporter, _ = _client()
    with start_trace():
        client.record_llm_call(
            provider="openai", model="gpt-4o", usage=_usage(), account_id=ACCOUNT
        )
    assert GenAI.ACCOUNT_ID_HASH in exporter.spans[0]


def test_no_account_means_no_account_keys():
    """Untagged spans are unchanged: no empty strings, no placeholder account."""
    client, exporter, _ = _client()
    with start_trace():
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
    attrs = exporter.spans[0]
    assert GenAI.ACCOUNT_ID_HASH not in attrs
    assert GenAI.ACCOUNT_ID_HASH_KEY_VERSION not in attrs
    assert GenAI.ACCOUNT_LABEL not in attrs


def test_user_id_behaviour_is_untouched():
    """CTO-181 adds a dimension; it must not start populating the user one."""
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
    attrs = exporter.spans[0]
    assert GenAI.USER_ID_HASH not in attrs
    assert GenAI.USER_ID_HASH_KEY_VERSION not in attrs


# --- label ----------------------------------------------------------------------------------


def test_label_rides_along_when_scoped():
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT, label="Northwind Traders"):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
    assert exporter.spans[0][GenAI.ACCOUNT_LABEL] == "Northwind Traders"


def test_label_can_be_overridden_per_call():
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT, label="Stale Name"):
        client.record_llm_call(
            provider="openai", model="gpt-4o", usage=_usage(), account_label="Fresh Name"
        )
    assert exporter.spans[0][GenAI.ACCOUNT_LABEL] == "Fresh Name"


def test_label_without_an_account_is_dropped():
    """A label is keyed on a hash in the gateway's store, so it never travels alone."""
    client, exporter, _ = _client()
    with start_trace():
        client.record_llm_call(
            provider="openai", model="gpt-4o", usage=_usage(), account_label="Orphan"
        )
    assert GenAI.ACCOUNT_LABEL not in exporter.spans[0]


def test_account_is_usable_with_no_label():
    """A tenant that wants no customer names in our system still gets the dimension."""
    client, exporter, _ = _client()
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
    attrs = exporter.spans[0]
    assert GenAI.ACCOUNT_ID_HASH in attrs
    assert GenAI.ACCOUNT_LABEL not in attrs


# --- degradation ----------------------------------------------------------------------------


def test_no_registry_emits_unattributed_rather_than_raw(caplog):
    exporter = MemoryExporter()
    client = TallyClient(
        exporter=exporter, tenant_id="tenant-zzz", sampler=_keep_everything()
    )  # no hmac_registry
    with caplog.at_level(logging.WARNING, logger="tally"):
        with start_trace(), with_account(ACCOUNT):
            client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())

    attrs = exporter.spans[0]
    assert GenAI.ACCOUNT_ID_HASH not in attrs
    assert ACCOUNT not in repr(attrs)
    assert any("unattributed" in r.message for r in caplog.records)


def test_unprovisioned_tenant_emits_unattributed():
    exporter = MemoryExporter()
    client = TallyClient(
        exporter=exporter,
        tenant_id="tenant-never-provisioned",
        hmac_registry=HmacKeyRegistry(),
        sampler=_keep_everything(),
    )
    with start_trace(), with_account(ACCOUNT):
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())
    assert exporter.spans and GenAI.ACCOUNT_ID_HASH not in exporter.spans[0]


def test_account_survives_an_await():
    """contextvars propagate into asyncio tasks; the account must ride with them."""
    import asyncio

    client, exporter, _ = _client()

    async def child() -> None:
        client.record_llm_call(provider="openai", model="gpt-4o", usage=_usage())

    async def main() -> None:
        with start_trace(), with_account(ACCOUNT):
            await asyncio.create_task(child())

    asyncio.run(main())
    assert GenAI.ACCOUNT_ID_HASH in exporter.spans[0]
