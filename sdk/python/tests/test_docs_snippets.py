# SPDX-License-Identifier: Apache-2.0
"""Run the documented Python snippets against a stub gateway (CTO-377).

The docs at ai-tally.com/docs show the dashboard's connect snippet
(docs/public-api/connect-snippets.json) and the SDK calls from the Python SDK page. A renamed
function or keyword would leave those pages telling customers to write code that silently records
nothing, because the SDK never raises. So
this test executes them for real against a local HTTP stub and fails unless a span actually
arrives. Hermetic: the stub listens on 127.0.0.1, no provider or ai-tally endpoint is contacted.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import tally
from tally.client import TallyClient
from tally.pricing import Usage

SNIPPETS = Path(__file__).resolve().parents[3] / "docs" / "public-api" / "connect-snippets.json"
PLACEHOLDER_KEY = "YOUR_TALLY_KEY"
RAW_ACCOUNT_ID = "acct_docs_example_raw_id"


class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def add(self, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        with self.lock:
            self.requests.append((method, path, headers, body))

    def batches(self) -> list[dict]:
        with self.lock:
            return [
                json.loads(b)
                for m, p, _h, b in self.requests
                if m == "POST" and p == "/v1/batches"
            ]

    def all(self) -> list[tuple[str, str, dict[str, str], bytes]]:
        with self.lock:
            return list(self.requests)


def _handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # keep test output quiet
            return

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            recorder.add("GET", self.path, dict(self.headers), b"")
            if self.path == "/v1/tenant/hmac-key":
                self._reply(
                    200,
                    {
                        "tenant_id": "11111111-2222-3333-4444-555555555555",
                        "key_version": "v1",
                        "key_material_b64": base64.b64encode(b"k" * 32).decode(),
                        "algorithm": "HMAC-SHA256",
                    },
                )
            else:
                self._reply(404, {"detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorder.add("POST", self.path, dict(self.headers), body)
            batch = json.loads(body) if body else {}
            self._reply(
                200,
                {
                    "batch_id": batch.get("batch_id", ""),
                    "status": "accepted",
                    "accepted_spans": len(batch.get("resource_spans", [])),
                    "partial_errors": [],
                    "server_hints": {},
                    "replayed": False,
                },
            )

    return Handler


@pytest.fixture
def stub_gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, _Recorder]]:
    recorder = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    # The documented snippet passes only a key, so the endpoint must come from the environment. That
    # is also what keeps this test off the real ingest host.
    monkeypatch.setenv("TALLY_ENDPOINT", endpoint)
    monkeypatch.setenv("TALLY_KEY", PLACEHOLDER_KEY)
    tally.uninstrument()
    try:
        yield endpoint, recorder
    finally:
        tally.uninstrument()
        server.shutdown()
        server.server_close()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _span_values(recorder: _Recorder) -> list[dict]:
    return [span for batch in recorder.batches() for span in batch.get("resource_spans", [])]


def _sdk_snippet() -> str:
    data = json.loads(SNIPPETS.read_text())
    (snippet,) = [s for s in data["snippets"]["sdk"] if s["id"] == "sdk-python"]
    return snippet["code"]


def test_dashboard_sdk_snippet_runs_and_records(stub_gateway: tuple[str, _Recorder]) -> None:
    _endpoint, recorder = stub_gateway
    namespace: dict[str, object] = {}
    exec(compile(_sdk_snippet(), "connect-snippets.json:sdk-python", "exec"), namespace)  # noqa: S102

    # The snippet only connects; record one call so the connection is proven end to end. An
    # embedding call is used because it is always sent: a hand-written record_llm_call is head-
    # sampled (the default keeps 1 in 10 cheap calls), so a single one may legitimately not arrive.
    tally.record_embedding_call(provider="openai", model="text-embedding-3-small", input_tokens=40)
    tally.flush(timeout=5.0)

    assert _wait_for(lambda: len(_span_values(recorder)) >= 1), "the snippet recorded nothing"
    auth = [h.get("Authorization") for m, p, h, _b in recorder.all() if p == "/v1/batches"]
    assert auth and all(a == f"Bearer {PLACEHOLDER_KEY}" for a in auth)


def test_documented_sdk_usage_runs_against_the_gateway(stub_gateway: tuple[str, _Recorder]) -> None:
    """The calls shown on the Python SDK docs page, with the keyword names shown there."""
    _endpoint, recorder = stub_gateway

    tally.init(feature_tag="assistant")  # key from TALLY_KEY, endpoint from TALLY_ENDPOINT
    with tally.with_account(RAW_ACCOUNT_ID, label="Docs Example"):
        with tally.start_trace(feature_tag="summarize"):
            llm = tally.record_llm_call(
                provider="openai",
                model="gpt-4o-mini",
                usage=Usage(input_tokens=12, output_tokens=5, cached_input_tokens=0),
            )
            tally.record_embedding_call(
                provider="openai", model="text-embedding-3-small", input_tokens=40
            )
    tally.record_tool_call(provider="serpapi", tool="search", cost_micro_usd=1_000)
    tally.record_vector_call(
        provider="pinecone", index="docs", operation="query", cost_micro_usd=250
    )
    tally.flush(timeout=5.0)

    # record_llm_call is head-sampled, so whether its span is sent is a coin flip by design. What
    # must hold regardless is that the documented keywords were accepted and the call was priced;
    # a swallowed TypeError would come back as the benign fallback result with no cost.
    assert llm.cost_micro_usd is not None and llm.cost_micro_usd > 0
    assert llm.attributes.get("gen_ai.feature_tag") == "summarize"

    # The embedding, tool and vector calls are always sent.
    assert _wait_for(lambda: len(_span_values(recorder)) >= 3), (
        f"expected the 3 unsampled documented calls to arrive, got {len(_span_values(recorder))}"
    )
    spans = _span_values(recorder)
    operations = {s.get("gen_ai.operation.name") for s in spans}
    assert {"embeddings", "tool", "vector"} <= operations
    assert any(
        s.get("gen_ai.feature_tag") == "summarize"
        and s.get("gen_ai.operation.name") == "embeddings"
        for s in spans
    )
    # The raw customer id must never leave the process, on any request.
    for _m, _p, _h, body in recorder.all():
        assert RAW_ACCOUNT_ID.encode() not in body

    digest = tally.hash_account(RAW_ACCOUNT_ID)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert any(p == "/v1/tenant/hmac-key" for _m, p, _h, _b in recorder.all())


def test_documented_hash_account_cli_prints_a_hash(stub_gateway: tuple[str, _Recorder]) -> None:
    """`python -m tally.hash_account <id>` is how the proxy and OTel pages say to hash an id."""
    endpoint, _recorder = stub_gateway
    env = {**os.environ, "TALLY_KEY": PLACEHOLDER_KEY, "TALLY_ENDPOINT": endpoint}
    result = subprocess.run(
        [sys.executable, "-m", "tally.hash_account", RAW_ACCOUNT_ID],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", result.stdout.strip())


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        (
            "record_llm_call",
            {
                "provider": "openai",
                "model": "m",
                "usage": Usage(),
                "account_id": "a",
                "account_label": "l",
            },
        ),
        (
            "record_embedding_call",
            {"provider": "openai", "model": "m", "input_tokens": 1, "account_id": "a"},
        ),
        (
            "record_tool_call",
            {"provider": "p", "tool": "t", "cost_micro_usd": 1, "account_id": "a"},
        ),
        (
            "record_vector_call",
            {
                "provider": "p",
                "index": "i",
                "operation": "query",
                "cost_micro_usd": 1,
                "account_id": "a",
            },
        ),
    ],
)
def test_documented_record_keywords_still_bind(name: str, kwargs: dict) -> None:
    # record_* never raise, so a renamed keyword would only log. Binding against the real signature
    # turns that silent breakage into a failed test.
    inspect.signature(getattr(TallyClient, name)).bind(None, **kwargs)


def test_documented_init_and_context_keywords_still_bind() -> None:
    inspect.signature(tally.init).bind(
        None,
        endpoint=None,
        feature_tag=None,
        instrument=True,
        instrument_stream_usage=False,
        flush_interval_s=1.0,
        catalog=None,
    )
    inspect.signature(tally.with_account).bind("acct", label=None)
    inspect.signature(tally.start_trace).bind(
        feature_tag=None, session_id=None, account_id=None, account_label=None
    )
    inspect.signature(tally.flush).bind(timeout=5.0)
    inspect.signature(tally.hash_account).bind("acct", key=None, endpoint=None)
