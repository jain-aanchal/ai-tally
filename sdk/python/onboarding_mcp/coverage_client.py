# SPDX-License-Identifier: Apache-2.0
"""Client for the gateway's per-layer coverage probe (CTO-261 section 7 / P3).

The probe itself lives in the gateway, which is the only side that can read ClickHouse. This
module is the other end of that wire: it calls
``GET /v1/tenant/onboarding/coverage``, service-token gated on ``x-tenant-id``, and turns the
response into the ``coverage_report`` MCP tool's per-layer answer.

WHY THIS RE-DERIVES INSTEAD OF TRUSTING THE WIRE. This is the last gate before a coverage claim
reaches a developer, and a green tick that nothing proves is the exact failure CTO-261 exists to
avoid. So :func:`derive_layer` computes the state from the proving-span count and downgrades any
payload that claims ``covered`` with no span behind it to ``unknown``. Wire-shape drift, a partial
deploy or a future gateway refactor therefore cannot light a layer green without evidence. The web
client on the dashboard side applies the same defense (``web/lib/firstEvent.ts``); this mirrors it
for the agent-facing path.

WHY EVERY FAILURE IS ``unknown``, NEVER "not covered". An unreachable gateway, a 5xx, a timeout, a
malformed body, a tenant id that is not a UUID or absent configuration all mean we do not know.
Collapsing any of them into ``not_wired`` would tell a developer their instrumentation is missing
when the truth is that ours could not answer (CLAUDE.md, honest under uncertainty).

Stdlib ``urllib`` only, so the SDK runtime stays dependency-free, and the transport is injectable
so the test suite never touches the network (the pattern ``onboarding_bot/github_pr.py`` uses).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

COVERAGE_PATH = "/v1/tenant/onboarding/coverage"

# Configuration, read at the point of use. The token is held BY REFERENCE: the config carries the
# NAME of the environment variable, never its value, so a serialized config or a logged repr
# cannot leak it (CLAUDE.md, credentials by reference).
GATEWAY_URL_ENV = "TALLY_GATEWAY_URL"
TENANT_ID_ENV = "TALLY_MCP_TENANT_ID"
DEFAULT_TOKEN_ENV = "GATEWAY_SERVICE_TOKEN"
TOKEN_ENV_REF = "TALLY_GATEWAY_SERVICE_TOKEN_ENV"

# WHY THIS TOOL DOES NOT READ ``TALLY_TENANT_ID`` (CTO-261). That name is retired: it used to scope
# the dashboard, no product code reads it any more, and several deploy manifests still SET it,
# inertly, to the old value ``local-dev``. Reading it here (even as a fallback) would let a stale
# taskdef, Helm value or ``.env`` silently scope the coverage probe to a tenant the operator did not
# choose. Worse, ``local-dev`` is a tenant NAME while this value is bound into a UUID read filter,
# so it would match nothing and the tool would report a confident, entirely wrong "nothing is
# wired". So the retired name is never a value source: it is only DETECTED, to tell the operator to
# rename it, and the answer stays an honest gap (CLAUDE.md, honest under uncertainty).
RETIRED_TENANT_ID_ENV = "TALLY_TENANT_ID"

# The probe does two ClickHouse reads, one of them a grouped pass over ``otel_spans`` that gets no
# key-prefix benefit, so a large or cold tenant can sit well past a few seconds. A short timeout
# would report every layer unknown for a tenant that is in fact fully instrumented, so this matches
# the 30s the sibling ``onboarding_bot/github_pr.py`` transport allows (CTO-261).
DEFAULT_TIMEOUT_SECONDS = 30.0

# A coverage payload is five small rows. Anything past this is not our endpoint answering, and
# reading it unbounded would let a wrong or hostile host sit on the tool's memory (CTO-261).
MAX_RESPONSE_BYTES = 1_048_576

# Schemes we will send a bearer token over. Plain http is allowed only to a loopback host, where
# there is no network to sniff, so local dev against ``http://localhost:8080`` keeps working while
# a real deployment cannot ship the service token in cleartext (CTO-261).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# The layer names the gateway speaks, keyed by the names this package has always used. The MCP
# surface says "tool" (it mirrors ``tally.record_tool_call``); the probe says "tools". Translating
# here keeps the tool's output stable for callers that already read it.
_LAYER_TO_WIRE: Mapping[str, str] = {
    "llm": "llm",
    "tool": "tools",
    "vector": "vector",
    "embeddings": "embeddings",
    "account": "account",
}
_WIRE_TO_LAYER: Mapping[str, str] = {wire: layer for layer, wire in _LAYER_TO_WIRE.items()}

DEFAULT_LAYERS: tuple[str, ...] = ("llm", "tool", "vector", "embeddings", "account")

# The four states the probe reports. Anything else on the wire is not understood, and not
# understood means unknown.
_STATES = frozenset({"covered", "awaiting_first_event", "not_wired", "unknown"})

UNREADABLE = "the coverage probe returned nothing readable for this layer"
NO_EVIDENCE = (
    "the probe reported this layer covered but returned no span to prove it, so we are not "
    "claiming it"
)

Transport = Callable[[str, dict[str, str], float], str]
"""(url, headers, timeout) -> response body. Raises like ``urllib`` does on a non-2xx or a
transport failure. Injected in tests so the suite never opens a socket."""


class CoverageUnavailable(RuntimeError):
    """The probe could not answer. Carries the reason a developer is shown, never a verdict."""


@dataclass(frozen=True)
class CoverageConfig:
    """Where the probe lives and who is asking. Holds a token reference, never a token."""

    gateway_url: str
    tenant_id: str
    token_env: str = DEFAULT_TOKEN_ENV
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def url(self, wired: Iterable[str] = ()) -> str:
        wire_names = [_LAYER_TO_WIRE[layer] for layer in wired if layer in _LAYER_TO_WIRE]
        base = f"{self.gateway_url.rstrip('/')}{COVERAGE_PATH}"
        if not wire_names:
            return base
        return f"{base}?{urllib.parse.urlencode({'wired': ','.join(sorted(set(wire_names)))})}"


def config_from_env(env: Mapping[str, str] | None = None) -> tuple[CoverageConfig | None, str]:
    """Build the config from the environment, or say why we cannot.

    Returns ``(None, reason)`` rather than raising or defaulting to a guessed endpoint: a probe we
    are not configured to reach is an unknown with a reason, not a report that nothing is wired.
    """
    source = os.environ if env is None else env
    gateway_url = (source.get(GATEWAY_URL_ENV) or "").strip()
    tenant_id = (source.get(TENANT_ID_ENV) or "").strip()
    token_env = (source.get(TOKEN_ENV_REF) or DEFAULT_TOKEN_ENV).strip() or DEFAULT_TOKEN_ENV

    required = ((GATEWAY_URL_ENV, gateway_url), (TENANT_ID_ENV, tenant_id))
    missing = [name for name, value in required if not value]

    # The retired name set while the current one is absent is the stale-manifest case, and it gets
    # its own reason: "not configured" would send an operator hunting for a variable they believe
    # they already set. We still do not READ it (CTO-261).
    if not tenant_id and (source.get(RETIRED_TENANT_ID_ENV) or "").strip():
        return None, (
            f"${RETIRED_TENANT_ID_ENV} is set but that name is retired and is not read: rename it "
            f"to ${TENANT_ID_ENV}, whose value must be your tenant UUID. Its value was not used "
            f"and nothing is being claimed about your instrumentation."
        )

    if missing:
        return None, (
            f"the coverage probe is not configured: set {' and '.join(missing)}, plus "
            f"${token_env} with the control-plane service token. Nothing is being claimed about "
            f"your instrumentation."
        )
    scheme_reason = _reject_unsafe_base(gateway_url)
    if scheme_reason:
        return None, scheme_reason

    if not _is_tenant_uuid(tenant_id):
        return None, (
            f"${TENANT_ID_ENV} must be your tenant UUID, not a tenant name. The coverage probe "
            f"binds this value into a UUID read filter, so a name matches nothing and would be "
            f"reported as an empty but confident-looking answer. The probe was not called and "
            f"nothing is being claimed about your instrumentation."
        )

    return CoverageConfig(gateway_url=gateway_url, tenant_id=tenant_id, token_env=token_env), ""


def _is_tenant_uuid(value: str) -> bool:
    """True only for a canonical 36-character UUID (CTO-261).

    Deliberately stricter than ``uuid.UUID`` alone, which also accepts braced, URN and undashed
    forms: the gateway compares this against a canonical UUID column, so accepting a shape it will
    not match would just move the silent-empty-answer failure one step later. The value is never
    echoed into the reason, since a mis-set variable can hold anything (CLAUDE.md, no bodies).
    """
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _reject_unsafe_base(gateway_url: str) -> str:
    """Say why ``gateway_url`` is not somewhere we will send a service token, or "" if it is.

    A reason rather than an exception: a base URL we refuse is one more thing we do not know the
    coverage for, and the caller turns it into the honest gap shape (CLAUDE.md, honest under
    uncertainty). We check the scheme here rather than at call time so the refusal happens before
    the token is ever read out of the environment (CTO-261).
    """
    try:
        parsed = urllib.parse.urlsplit(gateway_url)
    except ValueError:
        return (
            f"${GATEWAY_URL_ENV} is not a URL we can parse, so the coverage probe was not called. "
            f"Nothing is being claimed about your instrumentation."
        )

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return (
            f"${GATEWAY_URL_ENV} must be an http or https URL (got "
            f"{scheme or 'no scheme'!r}), so the coverage probe was not called. Nothing is being "
            f"claimed about your instrumentation."
        )
    if not parsed.hostname:
        return (
            f"${GATEWAY_URL_ENV} has no host, so the coverage probe was not called. Nothing is "
            f"being claimed about your instrumentation."
        )
    if scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        return (
            f"${GATEWAY_URL_ENV} uses plain http to a non-loopback host, which would send the "
            f"control-plane service token in cleartext. Use https (plain http is allowed only for "
            f"localhost). The coverage probe was not called and nothing is being claimed about "
            f"your instrumentation."
        )
    return ""


def resolve_token(config: CoverageConfig, env: Mapping[str, str] | None = None) -> str:
    """Read the service token at the point of use. Absent is a refusal, never an anonymous call."""
    source = os.environ if env is None else env
    token = (source.get(config.token_env) or "").strip()
    if not token:
        raise CoverageUnavailable(
            f"no control-plane service token in ${config.token_env}. Supply it by reference "
            f"(environment or secret manager); the probe is never called unauthenticated."
        )
    return token


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that will not carry the service token to another origin (CTO-261).

    ``HTTPRedirectHandler.redirect_request`` strips only ``content-length`` and ``content-type``
    and re-sends every other header, ``authorization`` included, at whatever host the 30x names.
    A gateway behind a load balancer that 301s elsewhere would therefore hand
    ``$GATEWAY_SERVICE_TOKEN`` to that host. So a redirect that leaves the origin (scheme, host or
    port) is refused outright rather than silently followed without the header: the tool's answer
    is then an honest unknown, which is the correct outcome for "we could not safely ask"
    (CLAUDE.md, credentials by reference).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if _origin(newurl) != _origin(req.full_url):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "cross-origin redirect refused: the control-plane service token is not "
                "forwarded off the configured gateway origin",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) for same-origin comparison. Unparseable compares equal to nothing."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ("", "", None)
    default = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port or default)


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener whose redirect handling cannot leak the token to another host (CTO-261)."""
    return urllib.request.build_opener(_SameOriginRedirectHandler())


def _urllib_transport(
    url: str, headers: dict[str, str], timeout: float
) -> str:  # pragma: no cover - exercised only against a real gateway
    request = urllib.request.Request(url, headers=headers, method="GET")
    # Never the module-level ``urlopen``: its default opener follows cross-host redirects with the
    # authorization header attached (CTO-261).
    with _build_opener().open(request, timeout=timeout) as response:  # noqa: S310 - scheme checked in config_from_env
        return _read_capped(response)


def _read_capped(response: Any) -> str:
    """Read at most :data:`MAX_RESPONSE_BYTES`, refusing anything longer (CTO-261).

    ``errors="replace"`` matches ``onboarding_bot/github_pr.py``: a non-UTF-8 error page from some
    proxy in the path must not become a ``UnicodeDecodeError`` escaping the failure funnel. The
    body is decoded only to be JSON-parsed, and never lands in an error message.
    """
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CoverageUnavailable(
            f"the coverage probe returned more than {MAX_RESPONSE_BYTES} bytes, which is not a "
            f"coverage payload, so we could not read your coverage"
        )
    return raw.decode("utf-8", errors="replace")


def fetch_coverage(
    config: CoverageConfig,
    *,
    wired: Iterable[str] = (),
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch and JSON-decode the probe's payload, or raise :class:`CoverageUnavailable`.

    Every failure mode is funnelled into that one exception with a human reason. The exception text
    never carries the token or the response body: an error routed onward carries no payload
    (CLAUDE.md, no bodies in telemetry).
    """
    token = resolve_token(config, env)
    headers = {
        "authorization": f"Bearer {token}",
        "x-tenant-id": config.tenant_id,
        "accept": "application/json",
        "user-agent": "ai-tally-onboarding-mcp",
    }
    send = transport or _urllib_transport
    try:
        raw = send(config.url(wired), headers, config.timeout)
    except urllib.error.HTTPError as exc:
        raise CoverageUnavailable(
            f"the coverage probe answered HTTP {exc.code}, so we could not read your coverage"
        ) from None
    except CoverageUnavailable:
        # The transport already produced a reason fit to show a developer (an oversized body, for
        # one). Let it through rather than relabelling it.
        raise
    except Exception as exc:
        # Deliberately everything else (CTO-261). The tool's contract is that no failure escapes as
        # a crash: a scheme-less base makes ``urlopen`` raise a bare ``ValueError`` ("unknown url
        # type"), ``http.client`` raises ``IncompleteRead`` / ``BadStatusLine``, and a non-UTF-8
        # body raises ``UnicodeDecodeError``, none of which is an OSError. Every one of them means
        # the same thing to a developer: we could not read your coverage. Only the exception CLASS
        # NAME reaches the message, never ``str(exc)``, so neither the token nor a URL nor a
        # response body can ride out in the reason (CLAUDE.md, no bodies in telemetry).
        raise CoverageUnavailable(
            f"the coverage probe could not be reached ({type(exc).__name__}), so we could not "
            f"read your coverage"
        ) from None

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise CoverageUnavailable(
            "the coverage probe returned a body that is not JSON, so we could not read your "
            "coverage"
        ) from None
    if not isinstance(payload, dict):
        raise CoverageUnavailable(
            "the coverage probe returned an unexpected payload shape, so we could not read your "
            "coverage"
        )
    return payload


def index_layers(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the payload's ``layers`` list by this package's layer names, skipping junk rows."""
    rows: dict[str, dict[str, Any]] = {}
    listed = payload.get("layers")
    if not isinstance(listed, list):
        return rows
    for row in listed:
        if not isinstance(row, dict):
            continue
        name = row.get("layer")
        if isinstance(name, str) and name in _WIRE_TO_LAYER:
            rows[_WIRE_TO_LAYER[name]] = row
    return rows


def _proving_spans(value: Any) -> int | None:
    """A usable span count, or None. A bool, a negative or a non-number is not a count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # NaN, an infinity or a negative count is not evidence of anything.
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return int(value)


def derive_layer(row: Mapping[str, Any] | None, *, wired: bool = False) -> dict[str, Any]:
    """Re-derive one layer's honest verdict from the evidence in ``row``.

    The state returned comes from the span count, so ``covered`` is structurally unreachable
    without a proving span no matter what the wire claimed. A row that claims coverage with no
    evidence is downgraded to ``unknown`` with a reason saying exactly that, and a missing row is
    unknown rather than silently dropped.
    """
    if row is None:
        return {"status": "unknown", "reason": UNREADABLE, "proving_spans": None}

    spans = _proving_spans(row.get("proving_spans"))
    reason_raw = row.get("reason")
    has_reason = isinstance(reason_raw, str) and reason_raw.strip()
    reason = reason_raw.strip() if has_reason else UNREADABLE
    claimed = row.get("state")

    if not isinstance(claimed, str) or claimed not in _STATES or claimed == "unknown":
        return {"status": "unknown", "reason": reason, "proving_spans": None}

    claims_wired = wired or claimed == "awaiting_first_event"
    if spans is None:
        derived = "unknown"
    elif spans > 0:
        derived = "covered"
    else:
        derived = "awaiting_first_event" if claims_wired else "not_wired"

    if claimed == "covered" and derived != "covered":
        return {"status": "unknown", "reason": NO_EVIDENCE, "proving_spans": None}
    if derived != claimed:
        # The wire's prose describes the wire's state, so once we have re-derived a different one
        # the two contradict each other and the prose is the half that is wrong. A row claiming
        # ``not_wired`` with 5 proving spans would otherwise be reported covered while carrying
        # "not wired: no span with GenAiOperation = 'tool'", and an unreadable count under
        # ``not_wired`` would deliver a fabricated negative as text. Neither is honest, so we say
        # what we derived and why (CTO-261).
        return {
            "status": derived,
            "reason": _derived_reason(derived, spans),
            "proving_spans": spans,
        }
    if derived == "unknown":
        return {"status": "unknown", "reason": reason, "proving_spans": None}
    return {"status": derived, "reason": reason, "proving_spans": spans}


def _derived_reason(derived: str, spans: int | None) -> str:
    """The reason for a status we derived ourselves, used when the wire's prose disagrees."""
    if derived == "covered":
        return (
            f"{spans} span(s) prove this layer is flowing, though the probe described it "
            f"differently; we report what the evidence shows"
        )
    if derived == "awaiting_first_event":
        return (
            "this layer is wired but no span has arrived yet, though the probe described it "
            "differently; we report what the evidence shows"
        )
    if derived == "not_wired":
        return (
            "no span proves this layer is flowing and it was not reported as newly wired, though "
            "the probe described it differently; we report what the evidence shows"
        )
    return UNREADABLE
