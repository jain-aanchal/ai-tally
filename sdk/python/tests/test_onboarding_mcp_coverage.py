# SPDX-License-Identifier: Apache-2.0
"""The coverage_report tool against the gateway probe (CTO-261 section 7 / P3).

The suite exists to hold one line: nothing reaches ``covered`` without a proving span, and every
failure resolves to unknown-with-a-reason rather than to a report that the developer's
instrumentation is missing. The transport is a fake throughout, so no test opens a socket.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest
from onboarding_mcp import coverage_report
from onboarding_mcp.coverage_client import (
    CoverageConfig,
    CoverageUnavailable,
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
