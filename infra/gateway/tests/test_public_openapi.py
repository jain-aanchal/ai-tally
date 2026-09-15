# SPDX-License-Identifier: Apache-2.0
"""The public OpenAPI export (CTO-371, CTO-375).

Two jobs: prove the filter publishes exactly the three customer-reachable routes and nothing from
the control plane, and act as the drift check that fails CI when a public route changes without
docs/public-api/public-openapi.json being regenerated.
"""

from __future__ import annotations

import json

from gateway.app import app
from gateway.errors import ErrorCode
from gateway.public_openapi import OUTPUT, PUBLIC_ROUTES, build_public_openapi, render


def _spec() -> dict:
    return build_public_openapi(app.openapi())


def test_exactly_the_three_public_routes() -> None:
    spec = _spec()
    assert set(spec["paths"]) == {"/v1/batches", "/v1/otlp/traces", "/v1/tenant/hmac-key"}
    assert {p: list(ops) for p, ops in spec["paths"].items()} == {
        "/v1/batches": ["post"],
        "/v1/otlp/traces": ["post"],
        "/v1/tenant/hmac-key": ["get"],
    }
    assert set(PUBLIC_ROUTES) == set(spec["paths"])


def test_no_control_plane_route_leaks() -> None:
    text = json.dumps(_spec())
    for internal in ("/v1/tenant/keys", "/v1/edge/keys", "/v1/tenant/proxy", "/readyz", "/v1/events"):
        assert internal not in text


def test_auth_is_a_bearer_scheme_not_a_header_parameter() -> None:
    spec = _spec()
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    for ops in spec["paths"].values():
        for op in ops.values():
            assert op["security"] == [{"bearerAuth": []}]
            names = [p["name"].lower() for p in op.get("parameters", [])]
            assert "authorization" not in names


def test_documented_error_codes_are_real() -> None:
    real = {c.value for c in ErrorCode}
    text = json.dumps(_spec())
    for code in ("UNAUTHENTICATED", "FORBIDDEN_SCOPE", "TENANT_MISMATCH", "RATE_LIMITED",
                 "QUOTA_EXCEEDED", "IDEMPOTENCY_UNAVAILABLE", "INVALID_SCHEMA",
                 "HMAC_EXPORT_DISABLED"):
        assert code in real
        assert code in text


def test_rate_limit_documents_retry_after_and_protocol_header() -> None:
    spec = _spec()
    batches = spec["paths"]["/v1/batches"]["post"]
    assert "Retry-After" in batches["responses"]["429"]["headers"]
    assert [p["name"] for p in batches["parameters"]] == ["X-Ingest-Protocol"]
    assert "422" in batches["responses"] and "400" in batches["responses"]


def test_hmac_key_schema_matches_what_the_handler_returns() -> None:
    # The overlay is hand-written, so pin it to the real response model: key_version is the string
    # selector ("v1"), which an earlier draft of this spec wrongly called an integer.
    from dataclasses import fields

    from gateway.tenant_hmac_key import HmacKeyMaterial

    schema = _spec()["components"]["schemas"]["HmacKey"]
    assert set(schema["properties"]) == {f.name for f in fields(HmacKeyMaterial)}
    assert schema["properties"]["key_version"]["type"] == "string"
    material = HmacKeyMaterial(tenant_id="t", key_version="v1", key_material_b64="eA==")
    assert set(material.as_dict()) == set(schema["properties"])


def test_committed_spec_is_current() -> None:
    assert OUTPUT.exists(), f"{OUTPUT} is missing; run scripts/export_public_openapi.py"
    assert OUTPUT.read_text() == render(app.openapi()), (
        "docs/public-api/public-openapi.json is stale: a public route changed. Regenerate with "
        "`uv run python scripts/export_public_openapi.py` from infra/gateway and commit it."
    )
