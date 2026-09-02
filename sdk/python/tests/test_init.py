# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §3 — tally.init: idempotent, env fallback, never-raise, module-level delegation."""

from __future__ import annotations

import sys

import pytest

import tally
from tally.context import current_context

# ``tally.init`` the attribute is the init() function (it shadows the submodule), so reach the
# module object through sys.modules.
init_mod = sys.modules["tally.init"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Keep the bootstrap thread off the network: fail fast instead of hitting a real gateway.
    def _no_net(endpoint, key):
        raise ConnectionError("no network in tests")

    monkeypatch.setattr(init_mod, "fetch_hmac_key", _no_net)
    monkeypatch.delenv("TALLY_KEY", raising=False)
    monkeypatch.delenv("TALLY_ENDPOINT", raising=False)
    yield
    tally.uninstrument()


def test_returns_client_and_is_idempotent():
    c1 = tally.init("tally_sk_live_a", instrument=False)
    c2 = tally.init("tally_sk_live_b", instrument=False)  # ignored: already initialised
    assert c1 is c2
    assert c1._api_key == "tally_sk_live_a"


def test_reads_env(monkeypatch):
    monkeypatch.setenv("TALLY_KEY", "tally_sk_live_env")
    monkeypatch.setenv("TALLY_ENDPOINT", "http://gw.env")
    c = tally.init(instrument=False)
    assert c._api_key == "tally_sk_live_env"
    assert c._endpoint == "http://gw.env"


def test_no_key_disables_without_raising():
    c = tally.init(instrument=False)  # no key, no env
    assert c is not None
    # Safe: recording still never raises, it just lands nowhere useful.
    assert tally.record_tool_call(provider="openai", tool="web_search") is None


def test_feature_tag_becomes_process_default():
    tally.init("tally_sk_live_a", feature_tag="research", instrument=False)
    assert current_context().feature_tag == "research"


def test_module_record_delegates_to_client():
    tally.init("tally_sk_live_a", instrument=False)
    tally.record_tool_call(provider="openai", tool="web_search")
    # The span was enqueued on the background transport (not flushed / no network).
    assert init_mod._transport is not None
    assert init_mod._transport.pending() >= 1


def test_record_before_init_is_noop():
    # Be explicit about the uninstrumented state regardless of test ordering.
    tally.uninstrument()
    assert tally.record_vector_call(provider="p", index="i", operation="query") is None
    assert tally.record_llm_call(provider="openai", model="gpt-4o", usage=None) is None


def test_init_never_raises_on_internal_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("transport construction failed")

    monkeypatch.setattr(init_mod, "BatchingTransport", _boom)
    # Must swallow the internal failure and still hand back a benign client.
    c = tally.init("tally_sk_live_a", instrument=False)
    assert c is not None
    assert init_mod._obs.internal_error_count >= 1


def test_flush_is_safe_before_init():
    tally.uninstrument()
    tally.flush()  # no client / transport -> no-op, no raise
