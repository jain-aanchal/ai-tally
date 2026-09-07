# SPDX-License-Identifier: Apache-2.0
"""The coverage_report tool against the gateway probe (CTO-261 section 7 / P3).

The suite exists to hold one line: nothing reaches ``covered`` without a proving span, and every
failure resolves to unknown-with-a-reason rather than to a report that the developer's
instrumentation is missing. The transport is a fake throughout, so no test opens a socket.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from onboarding_mcp import coverage_report
from onboarding_mcp.coverage_client import (
    MAX_RESPONSE_BYTES,
    CoverageConfig,
    CoverageUnavailable,
    _build_opener,
    _read_capped,
    _SameOriginRedirectHandler,
    config_from_env,
    derive_layer,
    fetch_coverage,
)

CONFIG = CoverageConfig(
    gateway_url="https://gateway.example/", tenant_id="11111111-2222-3333-4444-555555555555"
)
ENV = {"GATEWAY_SERVICE_TOKEN": "svc-token-value"}


def _row(layer: str, state: str, spans: int | None) -> dict[str, Any]:
    return {"layer": layer, "state": state, "reason": f"{layer}: {state}", "proving_spans": spans}


def _payload(*rows: dict[str, Any]) -> str:
    return json.dumps({"tenant_id": CONFIG.tenant_id, "layers": list(rows)})


def _transport(body: str, sink: list[tuple[str, dict[str, str]]] | None = None):
    def send(url: str, headers: dict[str, str], timeout: float) -> str:
        if sink is not None:
            sink.append((url, headers))
        return body

    return send


def _raising(exc: BaseException):
    def send(url: str, headers: dict[str, str], timeout: float) -> str:
        raise exc

    return send


def _by_layer(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {layer["layer"]: layer for layer in result["layers"]}


# ---------------------------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------------------------


def test_happy_path_reports_covered_layers_with_their_evidence():
    sink: list[tuple[str, dict[str, str]]] = []
    body = _payload(
        _row("llm", "covered", 42),
        _row("tools", "covered", 7),
        _row("vector", "not_wired", 0),
        _row("embeddings", "awaiting_first_event", 0),
        _row("account", "covered", 3),
    )
    result = coverage_report(
        "tally_sk_live_x", transport=_transport(body, sink), config=CONFIG, env=ENV
    )

    assert result["probe_available"] is True
    layers = _by_layer(result)
    assert layers["llm"]["status"] == "covered"
    assert layers["llm"]["proving_spans"] == 42
    # The MCP surface says "tool" while the probe says "tools"; the translation must hold.
    assert layers["tool"]["status"] == "covered"
    assert layers["vector"]["status"] == "not_wired"
    assert layers["embeddings"]["status"] == "awaiting_first_event"
    assert layers["account"]["status"] == "covered"
    assert all(layer["reason"] for layer in result["layers"])
    # Grounding survives: the tool still says which signal proves each layer.
    assert layers["llm"]["signal"] == "GenAiOperation = 'chat'"

    url, headers = sink[0]
    assert url.startswith("https://gateway.example/v1/tenant/onboarding/coverage")
    assert headers["x-tenant-id"] == CONFIG.tenant_id
    assert headers["authorization"] == "Bearer svc-token-value"


def test_wired_claim_is_forwarded_as_the_probes_query_param():
    sink: list[tuple[str, dict[str, str]]] = []
    coverage_report(
        "k",
        wired=["tool", "vector"],
        transport=_transport(_payload(), sink),
        config=CONFIG,
        env=ENV,
    )
    assert sink[0][0].endswith("?wired=tools%2Cvector")


def test_the_tenant_key_is_never_echoed_back():
    # It can be a secret. The tool reports only that one was supplied.
    result = coverage_report(
        "tally_sk_live_deadbeef", transport=_transport(_payload()), config=CONFIG, env=ENV
    )
    assert result["tenant_key_present"] is True
    assert "deadbeef" not in json.dumps(result)


# ---------------------------------------------------------------------------------------------
# The evidence gate.
# ---------------------------------------------------------------------------------------------


def test_a_layer_with_zero_spans_is_never_covered():
    body = _payload(_row("tools", "not_wired", 0), _row("vector", "awaiting_first_event", 0))
    layers = _by_layer(
        coverage_report("k", transport=_transport(body), config=CONFIG, env=ENV)
    )
    assert layers["tool"]["status"] == "not_wired"
    assert layers["tool"]["proving_spans"] == 0
    assert layers["vector"]["status"] == "awaiting_first_event"
    assert all(layer["status"] != "covered" for layer in layers.values())


@pytest.mark.parametrize("spans", [0, None, -1, "many", True])
def test_a_covered_claim_without_evidence_is_downgraded_to_unknown(spans):
    # The defense that makes wire drift harmless: covered is re-derived from the count, so a
    # payload asserting it with nothing behind it cannot light the layer green.
    body = _payload(_row("llm", "covered", spans))
    result = coverage_report("k", transport=_transport(body), config=CONFIG, env=ENV)
    layer = _by_layer(result)["llm"]
    assert layer["status"] == "unknown"
    assert layer["proving_spans"] is None
    assert "no span to prove it" in layer["reason"]


def test_a_missing_or_unparseable_layer_row_is_unknown_not_dark():
    body = _payload(_row("llm", "covered", 5), {"layer": "vector", "state": "banana"})
    layers = _by_layer(coverage_report("k", transport=_transport(body), config=CONFIG, env=ENV))
    assert layers["llm"]["status"] == "covered"
    assert layers["vector"]["status"] == "unknown"
    # embeddings was absent from the payload entirely.
    assert layers["embeddings"]["status"] == "unknown"
    assert layers["embeddings"]["proving_spans"] is None
    assert layers["embeddings"]["reason"]


def test_derive_layer_never_invents_a_zero_under_unknown():
    assert derive_layer(None)["proving_spans"] is None
    assert derive_layer({"state": "unknown", "proving_spans": None})["proving_spans"] is None


def test_an_unwired_but_firing_layer_still_reports_covered():
    # The span outranks the claim: evidence beats what the agent says it did or did not wire.
    body = _payload(_row("vector", "covered", 2))
    layers = _by_layer(
        coverage_report("k", wired=["llm"], transport=_transport(body), config=CONFIG, env=ENV)
    )
    assert layers["vector"]["status"] == "covered"


# ---------------------------------------------------------------------------------------------
# Failure modes. Each one is unknown-with-a-reason, never "not covered".
# ---------------------------------------------------------------------------------------------


def _assert_all_unknown_with_reason(result: dict[str, Any], fragment: str) -> None:
    assert result["probe_available"] is False
    assert fragment in result["reason"]
    for layer in result["layers"]:
        assert layer["status"] == "unknown", layer
        assert layer["proving_spans"] is None
        assert layer["reason"]
    statuses = {layer["status"] for layer in result["layers"]}
    assert "not_wired" not in statuses and "covered" not in statuses


def test_unreachable_gateway_is_unknown():
    result = coverage_report(
        "k",
        transport=_raising(urllib.error.URLError("connection refused")),
        config=CONFIG,
        env=ENV,
    )
    _assert_all_unknown_with_reason(result, "could not be reached")


def test_server_error_is_unknown():
    exc = urllib.error.HTTPError("https://gateway.example", 503, "boom", {}, None)
    result = coverage_report("k", transport=_raising(exc), config=CONFIG, env=ENV)
    _assert_all_unknown_with_reason(result, "HTTP 503")


def test_timeout_is_unknown():
    result = coverage_report("k", transport=_raising(TimeoutError()), config=CONFIG, env=ENV)
    _assert_all_unknown_with_reason(result, "could not be reached")


def test_malformed_json_is_unknown():
    result = coverage_report("k", transport=_transport("<html>502</html>"), config=CONFIG, env=ENV)
    _assert_all_unknown_with_reason(result, "not JSON")


def test_a_json_payload_of_the_wrong_shape_is_unknown():
    result = coverage_report("k", transport=_transport("[1, 2, 3]"), config=CONFIG, env=ENV)
    _assert_all_unknown_with_reason(result, "unexpected payload shape")


def test_missing_configuration_is_unknown_and_names_what_to_set():
    result = coverage_report("k", env={})
    _assert_all_unknown_with_reason(result, "not configured")
    assert "TALLY_GATEWAY_URL" in result["reason"]
    assert "TALLY_TENANT_ID" in result["reason"]


def test_missing_service_token_is_unknown_and_never_an_anonymous_call():
    result = coverage_report("k", transport=_transport(_payload()), config=CONFIG, env={})
    _assert_all_unknown_with_reason(result, "GATEWAY_SERVICE_TOKEN")


# ---------------------------------------------------------------------------------------------
# Configuration and credentials by reference.
# ---------------------------------------------------------------------------------------------


def test_config_from_env_holds_a_token_reference_not_a_token():
    config, reason = config_from_env(
        {
            "TALLY_GATEWAY_URL": "https://gw.example",
            "TALLY_TENANT_ID": "abc",
            "TALLY_GATEWAY_SERVICE_TOKEN_ENV": "MY_TOKEN_VAR",
            "MY_TOKEN_VAR": "s3cret",
        }
    )
    assert reason == ""
    assert config is not None
    assert config.token_env == "MY_TOKEN_VAR"
    assert "s3cret" not in repr(config)


def test_fetch_coverage_error_text_carries_no_token_and_no_body():
    exc = urllib.error.HTTPError("https://gateway.example", 500, "internal", {}, None)
    with pytest.raises(CoverageUnavailable) as caught:
        fetch_coverage(CONFIG, transport=_raising(exc), env=ENV)
    assert "svc-token-value" not in str(caught.value)
    assert "internal" not in str(caught.value)


# ---------------------------------------------------------------------------------------------
# Review fixes (CTO-261): the token never crosses an origin, every failure stays a gap, and a
# derived status never carries prose that contradicts it.
# ---------------------------------------------------------------------------------------------


def test_a_scheme_less_url_is_a_gap_not_a_crash():
    """``urlopen`` raises a bare ValueError ("unknown url type"), which is not an OSError."""
    config = CoverageConfig(gateway_url="gateway.example", tenant_id="t")
    boom = ValueError("unknown url type: 'gateway.example'")
    with pytest.raises(CoverageUnavailable) as caught:
        fetch_coverage(config, transport=_raising(boom), env=ENV)
    assert "could not" in str(caught.value)

    result = coverage_report("k", transport=_raising(boom), config=config, env=ENV)
    _assert_all_unknown_with_reason(result, "ValueError")


@pytest.mark.parametrize(
    "exc",
    [
        http.client.IncompleteRead(b""),
        http.client.BadStatusLine("garbage"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RuntimeError("something nobody predicted"),
    ],
)
def test_every_transport_failure_resolves_to_the_gap_shape(exc):
    result = coverage_report("k", transport=_raising(exc), config=CONFIG, env=ENV)
    assert result["probe_available"] is False
    for layer in result["layers"]:
        assert layer["status"] == "unknown"
        assert layer["proving_spans"] is None
        assert layer["reason"].strip()
    # Only the class name travels: no token, no body.
    assert "svc-token-value" not in json.dumps(result)


def test_an_oversized_body_is_a_gap_not_an_unbounded_read():
    class _Fat:
        def __init__(self) -> None:
            self.asked: int | None = None

        def read(self, n: int | None = None) -> bytes:
            self.asked = n
            return b"x" * (MAX_RESPONSE_BYTES + 1)

    fat = _Fat()
    with pytest.raises(CoverageUnavailable) as caught:
        _read_capped(fat)
    # Capped at the source: the reader never pulls an unbounded stream into memory.
    assert fat.asked == MAX_RESPONSE_BYTES + 1
    assert "xxxx" not in str(caught.value)


def test_a_non_utf8_body_decodes_instead_of_raising():
    class _Mojibake:
        def read(self, n: int | None = None) -> bytes:
            return b'{"layers": []} \xff\xfe'

    assert "layers" in _read_capped(_Mojibake())


@pytest.mark.parametrize(
    "base",
    ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080", "https://gw.example"],
)
def test_a_safe_base_url_is_accepted(base):
    config, reason = config_from_env({"TALLY_GATEWAY_URL": base, "TALLY_TENANT_ID": "abc"})
    assert reason == ""
    assert config is not None and config.gateway_url == base


@pytest.mark.parametrize(
    "base",
    [
        "http://gw.example",
        "http://10.0.0.5:8080",
        "ftp://gw.example",
        "gateway.example",
        "https://",
    ],
)
def test_an_unsafe_base_url_is_a_reasoned_gap_not_an_exception(base):
    config, reason = config_from_env({"TALLY_GATEWAY_URL": base, "TALLY_TENANT_ID": "abc"})
    assert config is None
    assert "TALLY_GATEWAY_URL" in reason
    assert "othing is being claimed" in reason


def test_a_cleartext_base_is_refused_before_the_token_is_read():
    env = {
        "TALLY_GATEWAY_URL": "http://gw.example",
        "TALLY_TENANT_ID": "abc",
        "GATEWAY_SERVICE_TOKEN": "svc-token-value",
    }
    result = coverage_report("k", config=None, env=env, transport=_transport(_payload()))
    _assert_all_unknown_with_reason(result, "https")
    assert "svc-token-value" not in json.dumps(result)


class _FakeResponse:
    """Enough of a response object for ``HTTPRedirectHandler.redirect_request``.

    ``HTTPError`` adopts the fp it is handed, so it needs ``close`` as well as ``read``.
    """

    def read(self, n: int | None = None) -> bytes:
        return b""

    def close(self) -> None:
        return None


def _redirect(from_url: str, to_url: str):
    handler = _SameOriginRedirectHandler()
    request = urllib.request.Request(from_url, headers={"authorization": "Bearer svc-token-value"})
    return handler.redirect_request(
        request, _FakeResponse(), 301, "Moved", {"location": to_url}, to_url
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/v1/tenant/onboarding/coverage",
        "http://gateway.example/v1/tenant/onboarding/coverage",
        "https://gateway.example:8443/v1/tenant/onboarding/coverage",
    ],
)
def test_a_cross_origin_redirect_never_forwards_the_authorization_header(target):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _redirect("https://gateway.example/v1/tenant/onboarding/coverage", target)
    assert "svc-token-value" not in str(caught.value)
    assert "cross-origin redirect refused" in str(caught.value)


def test_a_same_origin_redirect_is_still_followed():
    redirected = _redirect(
        "https://gateway.example/v1/tenant/onboarding/coverage",
        "https://gateway.example/v2/tenant/onboarding/coverage",
    )
    assert redirected is not None
    assert redirected.full_url == "https://gateway.example/v2/tenant/onboarding/coverage"


def test_the_transport_opener_carries_the_same_origin_redirect_handler():
    opener = _build_opener()
    assert any(isinstance(handler, _SameOriginRedirectHandler) for handler in opener.handlers)


def test_the_default_timeout_allows_for_a_cold_clickhouse_read():
    assert CoverageConfig(gateway_url="https://gw.example", tenant_id="t").timeout >= 30.0


def test_a_drifted_row_reason_matches_the_status_we_derived():
    """A ``not_wired`` row with real spans must not carry "not wired" prose under ``covered``."""
    row = {
        "layer": "tools",
        "state": "not_wired",
        "proving_spans": 5,
        "reason": "not wired: no span with GenAiOperation = 'tool'",
    }
    derived = derive_layer(row)
    assert derived["status"] == "covered"
    assert derived["proving_spans"] == 5
    assert "not wired" not in derived["reason"]


def test_an_unreadable_count_never_delivers_a_fabricated_negative_as_prose():
    row = {
        "layer": "tools",
        "state": "not_wired",
        "proving_spans": "many",
        "reason": "not wired: no span found",
    }
    derived = derive_layer(row)
    assert derived["status"] == "unknown"
    assert derived["proving_spans"] is None
    assert "not wired" not in derived["reason"]


def test_an_agreeing_row_keeps_the_probes_own_prose():
    derived = derive_layer(_row("llm", "covered", 12))
    assert derived["status"] == "covered"
    assert derived["reason"] == "llm: covered"


def test_a_drifted_awaiting_row_says_it_is_awaiting_not_what_the_wire_said():
    row = {
        "layer": "vector",
        "state": "not_wired",
        "proving_spans": 0,
        "reason": "not wired at all",
    }
    derived = derive_layer(row, wired=True)
    assert derived["status"] == "awaiting_first_event"
    assert "not wired at all" not in derived["reason"]


def test_the_success_path_carries_a_reason_key_too():
    result = coverage_report(
        "k", transport=_transport(_payload(_row("llm", "covered", 3))), config=CONFIG, env=ENV
    )
    assert result["probe_available"] is True
    # Reading result["reason"] unconditionally must not raise on the success branch.
    assert result["reason"] == ""
    failed = coverage_report("k", transport=_raising(TimeoutError()), config=CONFIG, env=ENV)
    assert set(failed) == set(result)
