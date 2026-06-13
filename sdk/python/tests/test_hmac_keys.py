# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.hmac_keys (CTO-74): per-tenant HMAC versioned keys + Option B rotation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tally.hmac_keys import (
    HmacKeyRegistry,
    InMemoryKeyMaterialProvider,
    StampedHash,
    next_version,
    plan_rehash_backfill,
    record_rotation_edges,
)
from tally.identity import IdentityGraph

UTC = timezone.utc
NOW = datetime(2026, 5, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# version helper
# --------------------------------------------------------------------------- #
def test_next_version():
    assert next_version("v1") == "v2"
    assert next_version("v9") == "v10"


def test_next_version_rejects_garbage():
    with pytest.raises(ValueError):
        next_version("x1")
    with pytest.raises(ValueError):
        next_version("v")


# --------------------------------------------------------------------------- #
# key material provider
# --------------------------------------------------------------------------- #
def test_material_is_deterministic_and_version_scoped():
    p = InMemoryKeyMaterialProvider()
    assert p.material("t1", "v1") == p.material("t1", "v1")
    assert p.material("t1", "v1") != p.material("t1", "v2")  # version-scoped
    assert p.material("t1", "v1") != p.material("t2", "v1")  # tenant-scoped


def test_material_rejects_empty():
    p = InMemoryKeyMaterialProvider()
    with pytest.raises(ValueError):
        p.material("", "v1")
    with pytest.raises(ValueError):
        p.material("t1", "")


# --------------------------------------------------------------------------- #
# provisioning + rotation
# --------------------------------------------------------------------------- #
def test_provision_is_idempotent():
    reg = HmacKeyRegistry()
    assert reg.provision("t1") == "v1"
    assert reg.provision("t1") == "v1"  # idempotent
    assert reg.versions("t1") == ("v1",)


def test_hash_requires_provisioning():
    reg = HmacKeyRegistry()
    with pytest.raises(KeyError):
        reg.hash("t1", "user@example.com")


def test_rotate_bumps_version_and_retains_old():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    assert reg.rotate("t1") == "v2"
    assert reg.active_version("t1") == "v2"
    assert reg.versions("t1") == ("v1", "v2")
    assert reg.rotate("t1") == "v3"
    assert reg.versions("t1") == ("v1", "v2", "v3")


# --------------------------------------------------------------------------- #
# hashing + stamping
# --------------------------------------------------------------------------- #
def test_hash_stamps_active_version():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    h = reg.hash("t1", "user@example.com")
    assert isinstance(h, StampedHash)
    assert h.key_version == "v1"
    assert len(h.value) == 64  # sha256 hex


def test_same_user_different_version_yields_different_hash():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    h1 = reg.hash("t1", "u1")
    reg.rotate("t1")
    h2 = reg.hash("t1", "u1")
    assert h1.value != h2.value
    assert h1.key_version == "v1"
    assert h2.key_version == "v2"


def test_same_user_different_tenant_yields_different_hash():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    reg.provision("t2")
    assert reg.hash("t1", "u1").value != reg.hash("t2", "u1").value


def test_hash_with_unknown_version_rejected():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    with pytest.raises(ValueError):
        reg.hash_with("t1", "u1", "v2")  # not provisioned yet


def test_hash_rejects_empty_user():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    with pytest.raises(ValueError):
        reg.hash("t1", "")


# --------------------------------------------------------------------------- #
# rotation backfill plan
# --------------------------------------------------------------------------- #
def test_plan_rehash_backfill_emits_edges():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    reg.rotate("t1")
    edges = plan_rehash_backfill(reg, "t1", ["u1", "u2"], old_version="v1", new_version="v2")
    assert len(edges) == 2
    e = edges[0]
    assert e.old_hash == reg.hash_with("t1", "u1", "v1").value
    assert e.new_hash == reg.hash_with("t1", "u1", "v2").value
    assert (e.old_version, e.new_version) == ("v1", "v2")


def test_plan_backfill_dedups_and_skips_empty():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    reg.rotate("t1")
    edges = plan_rehash_backfill(
        reg, "t1", ["u1", "u1", "", "u2"], old_version="v1", new_version="v2"
    )
    assert [e.user_id_preview for e in edges] == ["u1", "u2"]


# --------------------------------------------------------------------------- #
# end-to-end: rotation bridges attribution through the identity graph
# --------------------------------------------------------------------------- #
def test_rotation_edges_bridge_identity_across_versions():
    reg = HmacKeyRegistry()
    reg.provision("t1")
    old_hash = reg.hash("t1", "u1")  # v1 digest, captured before rotation
    reg.rotate("t1")
    new_hash = reg.hash("t1", "u1")  # v2 digest

    edges = plan_rehash_backfill(reg, "t1", ["u1"], old_version="v1", new_version="v2")
    graph = IdentityGraph()
    added = record_rotation_edges(graph, "t1", edges, observed_at=NOW)
    assert added == 1

    # A conversion attributed under the NEW key still reaches the pre-rotation (old-key) identity.
    resolved = graph.resolve_identity("t1", new_hash.value)
    assert old_hash.value in resolved
    # ...and tenant isolation holds — t2 sees nothing.
    assert graph.resolve_identity("t2", new_hash.value) == {new_hash.value}
