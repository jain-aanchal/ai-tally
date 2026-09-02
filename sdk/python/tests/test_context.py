# SPDX-License-Identifier: Apache-2.0
import asyncio
import threading

from tally.context import (
    current_context,
    note_context_drop,
    set_default_feature_tag,
    start_trace,
    with_trace_context,
)
from tally.safety import SelfObservability


def test_process_default_feature_tag_reaches_worker_threads():
    # set_default_feature_tag sets a contextvar in the calling thread; a gunicorn/Flask worker
    # thread starts fresh and never copies it, so its spans used to carry feature_tag=None despite
    # the docstring (CTO-260 §3, finding #4). The module-global default must reach any thread.
    set_default_feature_tag("research")
    try:
        assert current_context().feature_tag == "research"  # calling thread
        seen: dict[str, str | None] = {}

        def _worker() -> None:
            seen["tag"] = current_context().feature_tag

        th = threading.Thread(target=_worker)
        th.start()
        th.join()
        assert seen["tag"] == "research"  # a fresh worker thread inherits the process default

        # An explicit trace context still overrides the process default within its scope.
        with with_trace_context(trace_id="t1", feature_tag="checkout"):
            assert current_context().feature_tag == "checkout"
    finally:
        set_default_feature_tag(None)  # do not leak the default into other tests


def test_explicit_none_feature_tag_beats_process_default():
    # An explicit start_trace with no tag reads back as None, not the process default, because the
    # contextvar was set (to None) in this context; the default only fills a genuinely-unset slot.
    set_default_feature_tag("research")
    try:
        with start_trace():
            assert current_context().feature_tag is None
    finally:
        set_default_feature_tag(None)


def test_inactive_by_default():
    assert current_context().is_active is False


def test_set_and_restore():
    assert current_context().trace_id is None
    with with_trace_context(trace_id="t1", feature_tag="research") as ctx:
        assert ctx.trace_id == "t1"
        assert current_context().feature_tag == "research"
    # restored
    assert current_context().trace_id is None


def test_inherit_nested():
    with with_trace_context(trace_id="t1", feature_tag="f1"):
        with with_trace_context(session_id="s1"):
            c = current_context()
            assert c.trace_id == "t1"  # inherited
            assert c.feature_tag == "f1"  # inherited
            assert c.session_id == "s1"


def test_start_trace_generates_id():
    with start_trace(feature_tag="f") as ctx:
        assert ctx.trace_id and len(ctx.trace_id) == 32
        assert current_context().trace_id == ctx.trace_id


def test_propagates_across_asyncio_tasks():
    seen: dict[str, str | None] = {}

    async def child():
        seen["trace"] = current_context().trace_id

    async def main():
        with with_trace_context(trace_id="async-1"):
            # a child task copies the current context at creation time
            await asyncio.create_task(child())

    asyncio.run(main())
    assert seen["trace"] == "async-1"


def test_isolated_across_threads():
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, tid: str):
        with with_trace_context(trace_id=tid):
            barrier.wait()  # ensure both are inside their context simultaneously
            results[name] = current_context().trace_id

    t1 = threading.Thread(target=worker, args=("a", "thread-a"))
    t2 = threading.Thread(target=worker, args=("b", "thread-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results == {"a": "thread-a", "b": "thread-b"}


def test_generator_keeps_context():
    def gen():
        for _ in range(3):
            yield current_context().trace_id

    with with_trace_context(trace_id="gen-1"):
        values = list(gen())
    assert values == ["gen-1", "gen-1", "gen-1"]


def test_context_drop_counter():
    obs = SelfObservability()
    note_context_drop(obs, where="record_span")
    assert obs.context_drop_count == 1
    assert "context drop" in obs.last_errors[-1]
