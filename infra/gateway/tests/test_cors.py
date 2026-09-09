# SPDX-License-Identifier: Apache-2.0
"""Cross-origin policy (CTO-337).

Two halves, deliberately:

* the allowlist resolution, which is ours and is where every judgement call lives, and
* the installed middleware's actual browser-visible behaviour, exercised through a real Starlette
  app so the preflight is answered by the same code path a browser would hit.

The second half runs against a small app rather than the gateway's own ``app`` object because the
middleware stack is built once per process and the gateway's is already built (and configured from
the environment) by the time any test runs. ``test_gateway_app_installs_the_policy`` closes that gap
by asserting the real app carries the middleware with the options this module chose, so "we forgot
to install it" cannot pass.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from types import SimpleNamespace

from gateway.cors import (
    ALLOWED_HEADERS,
    ALLOWED_METHODS,
    DEFAULT_DEV_ORIGINS,
    PREFLIGHT_MAX_AGE_S,
    CorsConfigError,
    install_cors,
    resolve_allowed_origins,
)

DASHBOARD = "https://app.example.com"
STRANGER = "https://evil.example.net"


def _settings(value: str) -> object:
    return SimpleNamespace(cors_allowed_origins=value)


# --- allowlist resolution ---------------------------------------------------------------------


def test_unset_falls_back_to_the_local_dashboard_origins() -> None:
    # `make up` must keep working with no configuration at all, and the fallback must be loopback
    # only: a deployment that forgets to configure this admits nobody rather than everybody.
    assert resolve_allowed_origins(_settings("")) == list(DEFAULT_DEV_ORIGINS)
    assert resolve_allowed_origins(_settings("   ")) == list(DEFAULT_DEV_ORIGINS)
    assert all(o.startswith("http://localhost") or o.startswith("http://127.0.0.1") for o in DEFAULT_DEV_ORIGINS)


def test_configured_origins_replace_the_default_and_are_deduped() -> None:
    resolved = resolve_allowed_origins(
        _settings(f" {DASHBOARD} , https://staging.example.com,{DASHBOARD} ,")
    )
    assert resolved == [DASHBOARD, "https://staging.example.com"]


@pytest.mark.parametrize("value", ["*", f"{DASHBOARD},*", "https://*.example.com"])
def test_a_wildcard_is_refused_at_boot(value: str) -> None:
    # The gateway serves per-tenant credentials and HMAC key material. A wildcard would let any page
    # on the internet script a request from a visitor's browser and read the reply.
    with pytest.raises(CorsConfigError):
        resolve_allowed_origins(_settings(value))


@pytest.mark.parametrize(
    "value",
    [
        "app.example.com",  # no scheme: not an origin
        "ftp://app.example.com",  # not a browser scheme
        "https://app.example.com/dashboard",  # a URL, not an origin: would match nothing
        " , ",  # set, but naming nothing
    ],
)
def test_malformed_configuration_fails_loudly(value: str) -> None:
    with pytest.raises(CorsConfigError):
        resolve_allowed_origins(_settings(value))


# --- installed behaviour ----------------------------------------------------------------------


def _app(origins: str) -> TestClient:
    app = FastAPI()

    @app.post("/v1/tenant/budgets")
    def _write() -> dict:
        return {"ok": True}

    install_cors(app, _settings(origins))
    return TestClient(app)


def test_preflight_from_a_configured_origin_is_allowed() -> None:
    client = _app(DASHBOARD)
    res = client.options(
        "/v1/tenant/budgets",
        headers={
            "Origin": DASHBOARD,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-tenant-id",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == DASHBOARD
    assert "POST" in res.headers["access-control-allow-methods"]
    allowed = res.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "x-tenant-id" in allowed
    # Bounded preflight cache: this is also how long a REMOVED origin keeps working in a warmed
    # browser, so it is a revocation delay and not only a performance knob.
    assert res.headers["access-control-max-age"] == str(PREFLIGHT_MAX_AGE_S)
    # Credentials mode stays off: the gateway authenticates with a bearer header, not cookies.
    assert "access-control-allow-credentials" not in res.headers


def test_preflight_from_an_unconfigured_origin_is_rejected() -> None:
    client = _app(DASHBOARD)
    res = client.options(
        "/v1/tenant/budgets",
        headers={
            "Origin": STRANGER,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    # Starlette answers the preflight itself with a 400 and, crucially, no allow-origin header:
    # the browser refuses to send the real request.
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_actual_request_from_an_unconfigured_origin_gets_no_allow_origin_header() -> None:
    # The response body is still produced (CORS is a browser-enforced read restriction, not an
    # authorization control: every endpoint keeps its own bearer gate). What the stranger's page
    # does NOT get is permission to read it.
    client = _app(DASHBOARD)
    res = client.post("/v1/tenant/budgets", headers={"Origin": STRANGER}, json={})
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers

    allowed = client.post("/v1/tenant/budgets", headers={"Origin": DASHBOARD}, json={})
    assert allowed.headers["access-control-allow-origin"] == DASHBOARD


def test_default_configuration_admits_the_local_dashboard_only() -> None:
    client = _app("")
    ok = client.options(
        "/v1/tenant/budgets",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"

    blocked = client.options(
        "/v1/tenant/budgets",
        headers={"Origin": DASHBOARD, "Access-Control-Request-Method": "POST"},
    )
    assert blocked.status_code == 400


def test_gateway_app_installs_the_policy() -> None:
    from gateway.app import app as gateway_app

    installed = [m for m in gateway_app.user_middleware if m.cls is CORSMiddleware]
    assert len(installed) == 1
    options = installed[0].kwargs
    assert options["allow_credentials"] is False
    assert "*" not in options["allow_origins"]
    assert options["allow_methods"] == list(ALLOWED_METHODS)
    assert options["allow_headers"] == list(ALLOWED_HEADERS)
    assert options["max_age"] == PREFLIGHT_MAX_AGE_S
