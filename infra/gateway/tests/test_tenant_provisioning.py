# SPDX-License-Identifier: Apache-2.0
"""Tenant provisioning + resolver extension (Initiative 1, §4/§5).

These exercise the store logic without Postgres by monkeypatching ``psycopg.connect`` with a small
in-memory fake that understands the handful of statements the provisioner runs. The three cases that
carry the acceptance criteria:

* :func:`test_provision_is_idempotent_on_redelivery`: a Clerk webhook redelivery returns the existing
  tenant and mints NO new key material (the riskiest failure would be rolling an org's HMAC key set
  on every retry).
* :func:`test_provision_fresh_org_mints_and_creates`: a brand-new org gets a tenant and a minted HMAC
  reference that is NOT deleted.
* :func:`test_provision_lost_race_cleans_up_orphaned_key`: the loser of a concurrent race adopts the
  winner's tenant and deletes the key set it minted, so no orphaned material is left behind.
"""

from __future__ import annotations

import uuid

import pytest

from types import SimpleNamespace

from gateway import tenant_provisioning
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid
from gateway.tenant_provisioning import (
    LocalDevKeyProvider,
    ProvisionError,
    SecretManagerKeyProvider,
    TenantProvisioner,
    build_key_provider,
)


class SpyKeyProvider:
    """Records mint/delete so a test can assert orphan cleanup exactly."""

    def __init__(self) -> None:
        self.minted: list[str] = []
        self.deleted: list[str] = []

    def mint(self) -> str:
        ref = f"local://hmac/{len(self.minted)}/v1"
        self.minted.append(ref)
        return ref

    def delete(self, ref: str) -> None:
        self.deleted.append(ref)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._result = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT id, plan FROM tenants WHERE clerk_org_id"):
            org = params[0]
            if self._conn.race and not self._conn._race_consumed:
                # Simulate a concurrent writer inserting after our fast-path read: the first
                # SELECT misses even though the row exists for the INSERT conflict below.
                self._conn._race_consumed = True
                self._result = None
            else:
                self._result = self._conn.tenants.get(org)
        elif s.startswith("INSERT INTO plan_tiers"):
            self._result = None
        elif s.startswith("INSERT INTO tenants"):
            org = params[3]
            if org in self._conn.tenants:
                self._result = None  # ON CONFLICT DO NOTHING
            else:
                tid = str(uuid.uuid4())
                self._conn.tenants[org] = (tid, "free")
                self._result = (tid,)
        elif s.startswith("INSERT INTO usage_limits"):
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, tenants: dict, race: bool) -> None:
        self.tenants = tenants
        self.race = race
        self._race_consumed = False

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _patch_connect(monkeypatch, tenants: dict, *, race: bool = False) -> None:
    monkeypatch.setattr(
        tenant_provisioning.psycopg, "connect", lambda *_a, **_k: _FakeConn(tenants, race)
    )


class _Settings:
    postgres_dsn = "postgresql://ignored"


def test_provision_is_idempotent_on_redelivery(monkeypatch) -> None:
    existing = str(uuid.uuid4())
    tenants = {"org_abc": (existing, "free")}
    _patch_connect(monkeypatch, tenants)
    spy = SpyKeyProvider()
    prov = TenantProvisioner(_Settings(), key_provider=spy)

    result = prov.provision(clerk_org_id="org_abc", name="Acme")

    assert result.tenant_id == existing
    assert result.created is False
    # A redelivery must never mint new key material.
    assert spy.minted == []


def test_provision_fresh_org_mints_and_creates(monkeypatch) -> None:
    tenants: dict = {}
    _patch_connect(monkeypatch, tenants)
    spy = SpyKeyProvider()
    prov = TenantProvisioner(_Settings(), key_provider=spy)

    result = prov.provision(clerk_org_id="org_new", name="New Co")

    assert result.created is True
    assert result.plan == "free"
    assert "org_new" in tenants
    # One key set minted, and it survives (it is the tenant's live HMAC reference).
    assert len(spy.minted) == 1
    assert spy.deleted == []


def test_provision_lost_race_cleans_up_orphaned_key(monkeypatch) -> None:
    winner = str(uuid.uuid4())
    # The row already exists (the race winner) but the fast-path SELECT is forced to miss once.
    tenants = {"org_race": (winner, "free")}
    _patch_connect(monkeypatch, tenants, race=True)
    spy = SpyKeyProvider()
    prov = TenantProvisioner(_Settings(), key_provider=spy)

    result = prov.provision(clerk_org_id="org_race", name="Racer")

    assert result.tenant_id == winner
    assert result.created is False
    # Exactly one key set minted, and it was deleted because we lost the race (no orphan).
    assert len(spy.minted) == 1
    assert spy.minted == spy.deleted


def test_provision_rejects_blank_fields(monkeypatch) -> None:
    _patch_connect(monkeypatch, {})
    prov = TenantProvisioner(_Settings(), key_provider=SpyKeyProvider())
    with pytest.raises(ProvisionError):
        prov.provision(clerk_org_id="", name="Acme")
    with pytest.raises(ProvisionError):
        prov.provision(clerk_org_id="org_x", name="   ")


def test_tenant_for_clerk_org_missing_is_none(monkeypatch) -> None:
    _patch_connect(monkeypatch, {})
    prov = TenantProvisioner(_Settings(), key_provider=SpyKeyProvider())
    assert prov.tenant_for_clerk_org("org_absent") is None


def test_local_dev_provider_mint_is_unique_and_check_safe() -> None:
    provider = LocalDevKeyProvider()
    ref_a = provider.mint()
    ref_b = provider.mint()
    assert ref_a != ref_b
    # Must satisfy the no_raw_secret CHECK on tenants.hash_salt_kek_ref.
    for ref in (ref_a, ref_b):
        assert not ref.startswith("sk-")
        assert len(ref) < 512
        assert provider.has(ref)
    provider.delete(ref_a)
    assert not provider.has(ref_a)
    assert provider.has(ref_b)


def test_local_dev_provider_material_survives_a_restart() -> None:
    # Initiative 2 §3.2 regression: after a gateway restart the in-memory-only provider lost the
    # bytes and /v1/tenant/hmac-key 404'd for a durably-provisioned tenant. Material is now derived
    # from the durable reference + root secret, so a FRESH provider instance (a simulated restart)
    # resolves the same reference to the same 32 bytes.
    root = b"root-secret-value"
    before = LocalDevKeyProvider(root_secret=root)
    ref = before.mint()
    material_before = before.material(ref)
    assert len(material_before) == 32

    after = LocalDevKeyProvider(root_secret=root)  # simulated restart: empty minted set
    assert not after.has(ref)  # bookkeeping is gone...
    assert after.material(ref) == material_before  # ...but the material still resolves

    # Distinct references derive distinct material (tenant isolation), and an empty ref is a miss.
    assert after.material(before.mint()) != material_before
    with pytest.raises(KeyError):
        after.material("")


def test_build_key_provider_selects_by_setting() -> None:
    local = build_key_provider(
        SimpleNamespace(hmac_key_provider="local", hmac_local_root_secret="rs")
    )
    assert isinstance(local, LocalDevKeyProvider)
    # Two local providers built from the same root secret agree on material (restart-durable).
    other = build_key_provider(
        SimpleNamespace(hmac_key_provider="local", hmac_local_root_secret="rs")
    )
    ref = local.mint()
    assert local.material(ref) == other.material(ref)

    # The prod seam is selectable but fails fast without a wired client, never silently local.
    with pytest.raises(ProvisionError):
        build_key_provider(
            SimpleNamespace(hmac_key_provider="kms", hmac_local_root_secret="rs")
        )
    with pytest.raises(ProvisionError):
        build_key_provider(
            SimpleNamespace(hmac_key_provider="bogus", hmac_local_root_secret="rs")
        )


def test_secret_manager_provider_requires_a_client() -> None:
    with pytest.raises(ProvisionError):
        SecretManagerKeyProvider()
    # With a client wired it constructs; the concrete cloud calls are a deployment concern.
    assert SecretManagerKeyProvider(client=object()) is not None


# --- resolver extension ---------------------------------------------------------------------------


class _ResolverCursor:
    def __init__(self, by_name_or_org: dict) -> None:
        self._map = by_name_or_org
        self._row = None

    def execute(self, sql: str, params: tuple) -> None:
        # Both bind params are the same identifier (name OR clerk_org_id).
        self._row = self._map.get(params[0])

    def fetchone(self):
        return self._row


def test_resolver_matches_clerk_org_id() -> None:
    tid = str(uuid.uuid4())
    cur = _ResolverCursor({"org_123": (tid,)})
    assert resolve_tenant_uuid(cur, "org_123") == tid


def test_resolver_passes_uuid_through_without_query() -> None:
    tid = str(uuid.uuid4())
    # A cursor that would raise if queried, proving the UUID fast-path never hits Postgres.
    class _Boom:
        def execute(self, *_a: object) -> None:
            raise AssertionError("UUID fast-path must not query")

        def fetchone(self):
            raise AssertionError("UUID fast-path must not query")

    assert resolve_tenant_uuid(_Boom(), tid) == tid


def test_resolver_unknown_identifier_raises() -> None:
    cur = _ResolverCursor({})
    with pytest.raises(TenantNotFoundError):
        resolve_tenant_uuid(cur, "org_missing")
