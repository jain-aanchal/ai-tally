# SPDX-License-Identifier: Apache-2.0
import pytest

from tally.client import MemoryExporter, TallyClient
from tally.safety import SelfObservability, safe, safe_block
from tally.schema import GenAI, SpanFields


def test_safe_swallows_and_returns_fallback():
    obs = SelfObservability()

    @safe(obs, fallback=-1)
    def boom():
        raise ValueError("kaboom")

    assert boom() == -1
    assert obs.internal_error_count == 1
    assert "kaboom" in obs.last_errors[-1]


def test_safe_passes_through_success():
    obs = SelfObservability()

    @safe(obs, fallback=None)
    def ok(x):
        return x * 2

    assert ok(21) == 42
    assert obs.internal_error_count == 0


def test_safe_block_records_not_raises():
    obs = SelfObservability()
    with safe_block(obs, where="unit"):
        raise RuntimeError("inside block")
    assert obs.internal_error_count == 1


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_never_swallow_interrupts(exc):
    obs = SelfObservability()

    @safe(obs)
    def interrupt():
        raise exc()

    with pytest.raises(exc):
        interrupt()


def test_client_absorbs_faulty_exporter():
    class FaultyExporter:
        def export(self, attributes):
            raise OSError("network down")

    client = TallyClient(exporter=FaultyExporter())
    # Must not raise into caller.
    client.record_span(SpanFields(system="openai", input_tokens=5))
    assert client.observability.internal_error_count == 1


def test_client_happy_path_exports():
    exporter = MemoryExporter()
    client = TallyClient(exporter=exporter)
    client.record_span(SpanFields(system="openai", request_model="gpt-5-mini", input_tokens=10))
    assert len(exporter.spans) == 1
    assert exporter.spans[0][GenAI.SYSTEM] == "openai"
    assert client.observability.internal_error_count == 0


def test_snapshot_shape():
    obs = SelfObservability()
    snap = obs.snapshot()
    assert set(snap) == {"internal_error_count", "context_drop_count", "dropped_span_count"}
