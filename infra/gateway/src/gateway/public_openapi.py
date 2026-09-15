# SPDX-License-Identifier: Apache-2.0
"""The public OpenAPI document for the docs site (CTO-371, CTO-375).

The gateway serves dozens of routes, but a customer can reach exactly three of them on the hosted
ingest host (``deploy/demo/caddy-ingest-api/on/api.caddy``): ``POST /v1/batches``,
``POST /v1/otlp/traces`` and ``GET /v1/tenant/hmac-key``. Publishing the whole ``app.openapi()``
would document control-plane routes nobody outside can call, so this filters to those three.

FastAPI knows little about these routes: they read a raw ``Request`` and return ``JSONResponse``, so
the generated schema has no request body, no typed response, and a default 422 that these handlers
never produce. The overlay below adds what the handlers actually return. Every error code in it is
an ``ErrorCode`` member, so renaming a code breaks this module rather than leaving the docs wrong,
and ``tests/test_public_openapi.py`` fails when the committed spec is stale.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from gateway.errors import ErrorCode
from gateway.protocol import SUPPORTED_PROTOCOLS

PUBLIC_ROUTES: dict[str, str] = {
    "/v1/batches": "post",
    "/v1/otlp/traces": "post",
    "/v1/tenant/hmac-key": "get",
}

HOSTED_SERVER = "https://ingest.ai-tally.com"

OUTPUT = Path(__file__).resolve().parents[4] / "docs" / "public-api" / "public-openapi.json"


def _json(schema_ref: str, description: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}},
    }
    if headers:
        out["headers"] = headers
    return out


def _codes(*codes: ErrorCode) -> str:
    return ", ".join(f"`{c.value}`" for c in codes)


_RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying (integer).",
        "schema": {"type": "integer", "minimum": 1},
    }
}

# Shared by /v1/batches and /v1/otlp/traces: both run the same pipeline (app.py `_run_pipeline`).
_INGEST_RESPONSES: dict[str, Any] = {
    "200": _json(
        "BatchResponse",
        "Accepted. `status` is `accepted`, or `partial` when some spans were rejected (see "
        "`partial_errors`). A batch whose `batch_id` was already processed returns the original "
        "result with `replayed: true`.",
    ),
    "401": _json(
        "ErrorDetail",
        f"{_codes(ErrorCode.UNAUTHENTICATED)}: missing bearer token, or an invalid or revoked key.",
    ),
    "403": _json(
        "ErrorDetail",
        f"{_codes(ErrorCode.FORBIDDEN_SCOPE)}: the key has `read` scope and cannot write spans. "
        f"{_codes(ErrorCode.TENANT_MISMATCH)}: the body names a tenant the key is not bound to.",
    ),
    "422": {
        "description": "Every span in the batch was rejected (a `BatchResponse` with `status: "
        "rejected` and per-span `partial_errors`), or the body is malformed (`{\"detail\": "
        "\"...\"}`).",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/BatchResponse"},
                        {"type": "object", "properties": {"detail": {"type": "string"}}},
                    ]
                }
            }
        },
    },
    "429": _json(
        "RateLimited",
        f"{_codes(ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED)}: back off for `Retry-After` "
        "seconds and resend the same body with the same `batch_id`.",
        _RETRY_AFTER,
    ),
    "503": _json(
        "BatchResponse",
        "Temporarily unable to accept the batch (`status: retry`). Includes "
        f"{_codes(ErrorCode.IDEMPOTENCY_UNAVAILABLE)}. Resend the same body with the same "
        "`batch_id`; it is never double counted.",
        _RETRY_AFTER,
    ),
}

_COMPONENT_SCHEMAS: dict[str, Any] = {
    "ErrorDetail": {
        "type": "object",
        "required": ["detail"],
        "properties": {
            "detail": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
                    "message": {"type": "string"},
                },
            }
        },
    },
    "PartialError": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string"},
            "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
            "message": {"type": "string"},
        },
    },
    "BatchResponse": {
        "type": "object",
        "properties": {
            "batch_id": {"type": "string"},
            "status": {"type": "string", "enum": ["accepted", "partial", "rejected", "retry"]},
            "accepted_spans": {"type": "integer"},
            "partial_errors": {"type": "array", "items": {"$ref": "#/components/schemas/PartialError"}},
            "server_hints": {
                "type": "object",
                "properties": {
                    "flush_interval_ms": {"type": "integer"},
                    "max_batch_size": {"type": "integer"},
                    "sample_rate_override": {"type": ["number", "null"]},
                    "retry_after_ms": {"type": ["integer", "null"]},
                },
            },
            "replayed": {"type": "boolean"},
        },
    },
    "RateLimited": {
        "type": "object",
        "properties": {
            "batch_id": {"type": "string"},
            "status": {"type": "string", "enum": ["rejected"]},
            "error": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "enum": [ErrorCode.RATE_LIMITED.value, ErrorCode.QUOTA_EXCEEDED.value],
                    },
                    "message": {"type": "string"},
                },
            },
            "retry_after_ms": {"type": "integer"},
        },
    },
    "BatchRequest": {
        "type": "object",
        "description": "The ai-tally batch format (tally.wire.BatchRequest). Unknown fields are "
        "tolerated. With key auth the key decides the tenant, so `tenant_id` may be empty.",
        "required": ["tenant_id"],
        "properties": {
            "tenant_id": {"type": "string"},
            "batch_id": {"type": "string", "description": "Idempotency key for the batch."},
            "sdk_version": {"type": "string"},
            "client_send_ts_ns": {"type": "integer"},
            "resource_spans": {
                "type": "array",
                "description": "Flat span objects whose keys are `gen_ai.*` attributes. Prompt, "
                "completion and other body fields are never stored.",
                "items": {"type": "object", "additionalProperties": True},
            },
            "business_events": {"type": "array", "items": {"type": "object"}},
            "identity_links": {"type": "array", "items": {"type": "object"}},
            "sampling": {
                "type": "object",
                "properties": {
                    "head_sample_rate": {"type": "number"},
                    "sampling_strategy": {"type": "string"},
                },
            },
        },
    },
    "HmacKey": {
        "type": "object",
        "required": ["tenant_id", "key_version", "key_material_b64", "algorithm"],
        "properties": {
            "tenant_id": {"type": "string"},
            "key_version": {"type": "integer"},
            "key_material_b64": {"type": "string", "description": "Treat as a secret."},
            "algorithm": {"type": "string", "enum": ["HMAC-SHA256"]},
        },
    },
}

_OVERLAY: dict[str, dict[str, Any]] = {
    "/v1/batches": {
        "summary": "Send spans in the ai-tally batch format",
        "description": "The endpoint the Python SDK posts to. Idempotent on `batch_id`: resending "
        "a batch after a timeout or a 429/503 never double counts it.",
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BatchRequest"}}},
        },
        "responses": {
            **_INGEST_RESPONSES,
            "400": _json(
                "ErrorDetail",
                f"{_codes(ErrorCode.INVALID_SCHEMA)}: `X-Ingest-Protocol` names a protocol the "
                f"gateway does not support. Supported: {', '.join(SUPPORTED_PROTOCOLS)}.",
            ),
        },
    },
    "/v1/otlp/traces": {
        "summary": "Send OpenTelemetry traces (OTLP/HTTP, JSON only)",
        "description": "Accepts an OTLP `ExportTraceServiceRequest` encoded as JSON "
        "(`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json`). Protobuf is not supported. Spans are "
        "grouped by `gen_ai.operation.name`.",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "description": "OTLP ExportTraceServiceRequest (JSON encoding).",
                        "properties": {"resourceSpans": {"type": "array", "items": {"type": "object"}}},
                    }
                }
            },
        },
        "responses": _INGEST_RESPONSES,
    },
    "/v1/tenant/hmac-key": {
        "summary": "Fetch your organization's account hashing key",
        "description": "Returns the active HMAC-SHA256 key the SDK uses to hash account ids before "
        "they leave your process. Always requires a key with `write` or `admin` scope. The response "
        "is a secret: do not log it.",
        "responses": {
            "200": _json("HmacKey", "The active key and its version."),
            "401": _json(
                "ErrorDetail",
                f"{_codes(ErrorCode.UNAUTHENTICATED)}: missing bearer token, or an invalid or "
                "revoked key.",
            ),
            "403": _json(
                "ErrorDetail",
                f"{_codes(ErrorCode.FORBIDDEN_SCOPE)}: a `read` key cannot fetch key material. "
                f"{_codes(ErrorCode.HMAC_EXPORT_DISABLED)}: key export is disabled for this "
                "organization.",
            ),
            "404": {
                "description": "No key material exists for this organization. Hash nothing and "
                "send spans unattributed rather than sending raw ids.",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
                    }
                },
            },
            "503": {
                "description": "The key store could not be reached. Retryable; no key material "
                "was returned.",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
                    }
                },
            },
        },
    },
}


def build_public_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Filter a full ``app.openapi()`` document down to the public routes and overlay the facts."""
    missing = [p for p, m in PUBLIC_ROUTES.items() if m not in schema.get("paths", {}).get(p, {})]
    if missing:
        raise RuntimeError(f"public routes missing from the gateway schema: {missing}")

    paths: dict[str, Any] = {}
    for path, method in PUBLIC_ROUTES.items():
        op = copy.deepcopy(schema["paths"][path][method])
        # Authorization is expressed once as a security scheme, not as a loose header parameter.
        params = [p for p in op.get("parameters", []) if p.get("name", "").lower() != "authorization"]
        overlay = _OVERLAY[path]
        out: dict[str, Any] = {
            "operationId": op["operationId"],
            "summary": overlay["summary"],
            "description": overlay["description"],
            "security": [{"bearerAuth": []}],
        }
        if params:
            out["parameters"] = params
        if "requestBody" in overlay:
            out["requestBody"] = overlay["requestBody"]
        out["responses"] = dict(sorted(overlay["responses"].items()))
        paths[path] = {method: out}

    return {
        "openapi": schema["openapi"],
        "info": {
            "title": "ai-tally public ingest API",
            "version": schema["info"]["version"],
            "description": "The three routes a customer can call on the hosted ingest host. Generated "
            "from the gateway (infra/gateway/src/gateway/public_openapi.py); do not edit by hand.",
        },
        "servers": [{"url": HOSTED_SERVER}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "An ai-tally API key with `write` or `admin` scope, sent as "
                    "`Authorization: Bearer <key>`.",
                }
            },
            "schemas": _COMPONENT_SCHEMAS,
        },
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(build_public_openapi(schema), indent=2, ensure_ascii=False) + "\n"
