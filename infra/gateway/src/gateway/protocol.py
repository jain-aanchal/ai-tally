"""Ingest protocol: version negotiation, capabilities, and OTLP/HTTP fallback (CTO-31).

ai-tally's native wire is ``ingest-v1`` (the :mod:`tally.wire` envelope). The contract is *additive*:
new optional fields may appear; an older gateway/client tolerates unknown fields rather than
rejecting (see :func:`negotiate` and the unknown-field tests). For interop, the gateway also accepts
standard **OTLP/HTTP JSON** trace exports and translates them into native span dicts, so any
OpenTelemetry SDK can ship spans without the ai-tally SDK.
"""

from __future__ import annotations

from typing import Any

#: Native ingest protocol identifier (advertised + negotiated).
INGEST_V1 = "ingest-v1"

#: Protocols this gateway can accept, newest first.
SUPPORTED_PROTOCOLS: tuple[str, ...] = (INGEST_V1,)


def negotiate(requested: str | None) -> str | None:
    """Resolve a client's requested protocol to one we support.

    ``None``/empty means "client didn't ask" → default to the native protocol. A non-empty value we
    don't recognize returns ``None`` (caller rejects with a clear error rather than guessing).
    """
    if not requested:
        return INGEST_V1
    return requested if requested in SUPPORTED_PROTOCOLS else None


def capabilities(*, max_batch_size: int, max_span_bytes: int) -> dict[str, Any]:
    """Self-describing capability document for ``GET /v1/capabilities``.

    A conformant client fetches this once to learn the negotiated protocol, ceilings, and which
    optional features (idempotency, server hints, OTLP fallback) the gateway honors.
    """
    return {
        "protocols": list(SUPPORTED_PROTOCOLS),
        "default_protocol": INGEST_V1,
        "max_batch_size": max_batch_size,
        "max_span_bytes": max_span_bytes,
        "compression": ["none"],
        "features": {
            "idempotency": True,
            "server_hints": True,
            "partial_acceptance": True,
            "otlp_http_traces": True,
            "unknown_field_tolerance": True,
        },
    }


# --- OTLP/HTTP JSON → native span dicts ----------------------------------------------------------


def _any_value(v: dict[str, Any]) -> Any:
    """Unwrap an OTLP ``AnyValue`` (https://opentelemetry.io/docs/specs/otlp) to a Python scalar."""
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        # OTLP encodes int64 as a string in JSON; coerce back to int.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_any_value(e) for e in v["arrayValue"].get("values", [])]
    return None


def _attrs_to_dict(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in attributes or []:
        key = a.get("key")
        if isinstance(key, str) and "value" in a:
            out[key] = _any_value(a["value"])
    return out


def otlp_traces_to_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate an OTLP/HTTP ``ExportTraceServiceRequest`` (JSON) into native span dicts.

    Resource attributes are merged under each span (span attributes win on conflict). ``traceId`` /
    ``spanId`` / ``startTimeUnixNano`` map to our structural keys; everything else (notably
    ``gen_ai.*``) passes through unchanged so the existing validator + enricher apply as-is.
    """
    spans: list[dict[str, Any]] = []
    for rs in payload.get("resourceSpans", []):
        resource_attrs = _attrs_to_dict(rs.get("resource", {}).get("attributes", []))
        service_name = resource_attrs.get("service.name")
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                span: dict[str, Any] = dict(resource_attrs)
                span.update(_attrs_to_dict(sp.get("attributes", [])))
                if sp.get("traceId"):
                    span["trace_id"] = sp["traceId"]
                if sp.get("spanId"):
                    span["span_id"] = sp["spanId"]
                start = sp.get("startTimeUnixNano")
                if start is not None:
                    try:
                        span["timestamp_ns"] = int(start)
                    except (TypeError, ValueError):
                        pass
                if service_name is not None and "ServiceName" not in span:
                    span["ServiceName"] = service_name
                spans.append(span)
    return spans
