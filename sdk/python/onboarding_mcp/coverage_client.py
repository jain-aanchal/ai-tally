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
malformed body or absent configuration all mean we do not know. Collapsing any of them into
``not_wired`` would tell a developer their instrumentation is missing when the truth is that ours
could not answer (CLAUDE.md, honest under uncertainty).

Stdlib ``urllib`` only, so the SDK runtime stays dependency-free, and the transport is injectable
so the test suite never touches the network (the pattern ``onboarding_bot/github_pr.py`` uses).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

COVERAGE_PATH = "/v1/tenant/onboarding/coverage"

# Configuration, read at the point of use. The token is held BY REFERENCE: the config carries the
# NAME of the environment variable, never its value, so a serialized config or a logged repr
# cannot leak it (CLAUDE.md, credentials by reference).
GATEWAY_URL_ENV = "TALLY_GATEWAY_URL"
TENANT_ID_ENV = "TALLY_TENANT_ID"
DEFAULT_TOKEN_ENV = "GATEWAY_SERVICE_TOKEN"
TOKEN_ENV_REF = "TALLY_GATEWAY_SERVICE_TOKEN_ENV"

DEFAULT_TIMEOUT_SECONDS = 5.0

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
    if missing:
        return None, (
            f"the coverage probe is not configured: set {' and '.join(missing)}, plus "
            f"${token_env} with the control-plane service token. Nothing is being claimed about "
            f"your instrumentation."
        )
    return CoverageConfig(gateway_url=gateway_url, tenant_id=tenant_id, token_env=token_env), ""


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


def _urllib_transport(
    url: str, headers: dict[str, str], timeout: float
) -> str:  # pragma: no cover - exercised only against a real gateway
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured base
        return response.read().decode("utf-8")


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
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
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
    if derived == "unknown":
        return {"status": "unknown", "reason": reason, "proving_spans": None}
    return {"status": derived, "reason": reason, "proving_spans": spans}
