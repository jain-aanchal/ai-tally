# SPDX-License-Identifier: Apache-2.0
"""Unknown-tenant-name control-plane 404s (CTO-201).

Every control-plane store folds a name onto ``tenants.id`` via ``resolve_tenant_uuid`` and raises
:class:`TenantNotFoundError` when the name resolves to no row. A name-based caller used to reach a
UUID column and surface an opaque 500; the app-level exception handler now turns that raise into a
clean 404 for every store whose endpoint does not translate it inline. This asserts that contract
across each read endpoint that was made name-safe, using a stand-in store that always raises.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tenant_lookup import TenantNotFoundError

UNKNOWN_TENANT = "no-such-tenant"


class _Raiser:
    """A store stand-in whose every method raises :class:`TenantNotFoundError`.

    Models a caller whose tenant NAME resolves to no row: the real store raises from
    ``resolve_tenant_uuid`` before it can run any query.
    """

    def __getattr__(self, _name: str):
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise TenantNotFoundError(f"no tenant named '{UNKNOWN_TENANT}'")

        return _raise


# (app.state attribute, read endpoint) for every store made name-safe under CTO-201.
CASES = [
    ("tenant_connectors", "/v1/tenant/connectors"),
    ("tenant_cac", "/v1/tenant/cac"),
    ("tenant_guardrails", "/v1/tenant/guardrails"),
    ("tenant_feature_value_events", "/v1/tenant/feature-value-events"),
    ("tenant_eval", "/v1/tenant/eval/config"),
    ("tenant_integrations", "/v1/tenant/integrations/status"),
    ("tenant_unit_economics", "/v1/tenant/unit-economics/config"),
    ("tenant_replay", "/v1/tenant/replay/config"),
    ("tenant_stripe", "/v1/tenant/stripe"),
]


@pytest.mark.parametrize("attr,path", CASES)
def test_unknown_tenant_name_is_404_not_500(attr: str, path: str) -> None:
    with TestClient(app) as client:
        # Auth off so the X-Tenant-Id header is taken as the tenant identifier (dev / dashboard).
        app.state.settings.require_api_key = False
        setattr(app.state, attr, _Raiser())
        r = client.get(path, headers={"X-Tenant-Id": UNKNOWN_TENANT})
    assert r.status_code == 404, r.text
    assert r.json()["detail"], "the 404 body should name the failure"
