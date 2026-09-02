# SPDX-License-Identifier: Apache-2.0
"""CTO-260 §3.2/§3.3 - HMAC bootstrap: fetch/parse, TTL cache, registry hashing, fallback."""

from __future__ import annotations

import base64

import pytest

from tally.hmac_keys import (
    HmacKeyBootstrap,
    HmacKeyRegistry,
    RemoteKeyMaterialProvider,
)
from tally.transport import fetch_hmac_key

_MATERIAL = b"0123456789abcdef0123456789abcdef"  # 32 bytes
_TENANT = "11111111-2222-3333-4444-555555555555"


def _fake_response() -> dict:
    return {
        "tenant_id": _TENANT,
        "key_version": "v3",
        "key_material_b64": base64.b64encode(_MATERIAL).decode("ascii"),
        "algorithm": "HMAC-SHA256",
    }


def test_fetch_hmac_key_parses_response():
    seen = {}

    def opener(url, headers, timeout):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return _fake_response()

    boot = fetch_hmac_key("http://gw.test", "tally_sk_live_x", opener=opener)
    assert seen["url"] == "http://gw.test/v1/tenant/hmac-key"
    assert seen["auth"] == "Bearer tally_sk_live_x"
    assert boot.tenant_id == _TENANT
    assert boot.key_version == "v3"
    assert boot.material == _MATERIAL


def test_remote_provider_caches_until_ttl():
    calls = {"n": 0}
    clock = {"t": 0.0}

    def fetch() -> HmacKeyBootstrap:
        calls["n"] += 1
        return HmacKeyBootstrap(_TENANT, "v3", _MATERIAL)

    provider = RemoteKeyMaterialProvider(
        fetch=fetch, ttl_seconds=100.0, _clock=lambda: clock["t"]
    )
    assert provider.material(_TENANT, "v3") == _MATERIAL
    assert provider.material(_TENANT, "v3") == _MATERIAL
    assert calls["n"] == 1  # served from cache
    clock["t"] = 200.0  # advance past TTL
    assert provider.material(_TENANT, "v3") == _MATERIAL
    assert calls["n"] == 2  # re-fetched after expiry


def test_registry_hashes_under_bootstrapped_key():
    provider = RemoteKeyMaterialProvider(fetch=lambda: HmacKeyBootstrap(_TENANT, "v3", _MATERIAL))
    provider._cache["v3"] = (provider._clock(), _MATERIAL)
    registry = HmacKeyRegistry(provider=provider)
    registry.provision(_TENANT, initial_version="v3")
    stamped = registry.hash_account(_TENANT, "acct_northwind")
    assert stamped.key_version == "v3"
    assert len(stamped.value) == 64  # sha256 hex
    # Deterministic under the same key material.
    assert registry.hash_account(_TENANT, "acct_northwind").value == stamped.value


def test_init_bootstrap_sets_registry(monkeypatch):
    import sys
    init_mod = sys.modules["tally.init"]

    monkeypatch.setattr(
        init_mod, "fetch_hmac_key", lambda endpoint, key: HmacKeyBootstrap(_TENANT, "v3", _MATERIAL)
    )
    from tally.client import TallyClient
    from tally.safety import SelfObservability

    client = TallyClient()
    init_mod._bootstrap_hmac(client, "http://gw.test", "tally_sk_live_x", SelfObservability())
    assert client.tenant_id == _TENANT
    assert client.hmac_registry is not None
    # And it can hash without a second network fetch (primed cache).
    assert len(client.hmac_registry.hash_account(_TENANT, "acct_x").value) == 64


def test_init_bootstrap_failure_leaves_unattributed(monkeypatch):
    import sys
    init_mod = sys.modules["tally.init"]

    def boom(endpoint, key):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(init_mod, "fetch_hmac_key", boom)
    from tally.client import TallyClient
    from tally.safety import SelfObservability

    client = TallyClient()
    obs = SelfObservability()
    init_mod._bootstrap_hmac(client, "http://gw.test", "tally_sk_live_x", obs)
    # No raise; account stays unattributed (tenant/ registry never set).
    assert client.tenant_id is None
    assert client.hmac_registry is None
    assert obs.internal_error_count >= 1


def test_provider_unknown_version_raises_when_uncached():
    provider = RemoteKeyMaterialProvider(fetch=lambda: HmacKeyBootstrap(_TENANT, "v3", _MATERIAL))
    with pytest.raises(ValueError):
        provider.material(_TENANT, "v9")
