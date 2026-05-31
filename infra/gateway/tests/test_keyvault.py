"""Pure tests for the in-memory provider-key vault (CTO-42, spec §4.2 / §14.12).

No infra, network, or filesystem — this is the audit surface for the never-log/never-persist
guarantee and the fail-open-routing / fail-closed-guardrail policy.
"""

from __future__ import annotations

import pytest

from gateway.keyvault import (
    GuardrailState,
    ProviderKey,
    ProviderKeyVault,
    should_allow_routing,
    should_apply_guardrail,
)

SECRET = "sk-super-secret-customer-key-1234567890"


# --- never-log / never-persist guarantee ---------------------------------------------------------


def test_repr_and_str_redact_the_secret() -> None:
    key = ProviderKey.create("t1", "openai", SECRET)
    for rendered in (repr(key), str(key), f"{key}", f"{key!r}"):
        assert SECRET not in rendered
        assert "redacted" in rendered
    assert "openai" in repr(key)  # non-secret metadata is fine to show


def test_secret_does_not_leak_via_exception_message() -> None:
    key = ProviderKey.create("t1", "openai", SECRET)
    try:
        raise ValueError(f"failed to use {key}")  # accidental f-string of the container
    except ValueError as exc:
        assert SECRET not in str(exc)


def test_pydantic_secret_wrapper_is_redacted_in_repr() -> None:
    key = ProviderKey.create("t1", "openai", SECRET)
    # The wrapped SecretStr itself must not render the value.
    assert SECRET not in repr(key._secret)


def test_only_explicit_accessor_returns_raw_value() -> None:
    key = ProviderKey.create("t1", "openai", SECRET)
    assert key.reveal() == SECRET
    # No other zero-arg public attribute/method exposes the raw value.
    for name in dir(key):
        if name in ("reveal", "create") or name.startswith("__"):
            continue
        attr = getattr(key, name)
        try:
            value = attr() if callable(attr) else attr
        except TypeError:
            continue  # requires args; not a passive leak
        assert value != SECRET, f"raw secret leaked via {name!r}"


# --- vault CRUD + lookups ------------------------------------------------------------------------


def test_upsert_and_get_roundtrip() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "openai", SECRET)
    got = vault.get("t1", "openai")
    assert got is not None
    assert got.reveal() == SECRET
    assert vault.has_key("t1", "openai")


def test_get_miss_returns_none() -> None:
    vault = ProviderKeyVault()
    assert vault.get("t1", "openai") is None
    assert not vault.has_key("t1", "openai")


def test_revoke_is_idempotent() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "openai", SECRET)
    vault.revoke("t1", "openai")
    vault.revoke("t1", "openai")  # no error second time
    assert vault.get("t1", "openai") is None


def test_keys_are_partitioned_by_tenant_and_provider() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "openai", "a-key")
    vault.upsert("t1", "anthropic", "b-key")
    vault.upsert("t2", "openai", "c-key")
    assert vault.get("t1", "openai").reveal() == "a-key"
    assert vault.get("t1", "anthropic").reveal() == "b-key"
    assert vault.get("t2", "openai").reveal() == "c-key"


# --- rotation (config-refresh channel) -----------------------------------------------------------


def test_rotate_takes_effect_immediately() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "openai", "old-key")
    vault.rotate("t1", "openai", "new-key")
    assert vault.get("t1", "openai").reveal() == "new-key"


def test_apply_refresh_swaps_all_tenant_keys_and_prunes() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "openai", "old-openai")
    vault.upsert("t1", "anthropic", "old-anthropic")
    vault.apply_refresh("t1", {"openai": "new-openai"})
    assert vault.get("t1", "openai").reveal() == "new-openai"
    # anthropic was absent from the snapshot -> pruned (revoked).
    assert vault.get("t1", "anthropic") is None


def test_apply_refresh_no_prune_keeps_unlisted() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t1", "anthropic", "keep-me")
    vault.apply_refresh("t1", {"openai": "new-openai"}, prune=False)
    assert vault.get("t1", "anthropic").reveal() == "keep-me"
    assert vault.get("t1", "openai").reveal() == "new-openai"


def test_apply_refresh_does_not_touch_other_tenants() -> None:
    vault = ProviderKeyVault()
    vault.upsert("t2", "openai", "tenant-2-key")
    vault.apply_refresh("t1", {"openai": "tenant-1-key"})
    assert vault.get("t2", "openai").reveal() == "tenant-2-key"


# --- control-plane partition policy --------------------------------------------------------------


@pytest.mark.parametrize(
    ("partitioned", "have_cached_key", "expected"),
    [
        (False, True, True),  # connected + key -> allow
        (False, False, False),  # connected, no key -> deny (nothing to serve)
        (True, True, True),  # partitioned but cached key -> fail open (availability)
        (True, False, False),  # partitioned, no cached key -> deny
    ],
)
def test_should_allow_routing_fails_open_with_cached_key(
    partitioned: bool, have_cached_key: bool, expected: bool
) -> None:
    assert should_allow_routing(partitioned, have_cached_key) is expected


@pytest.mark.parametrize(
    ("partitioned", "state", "expected"),
    [
        (False, GuardrailState.DISABLED, False),
        (True, GuardrailState.DISABLED, False),  # explicitly disabled -> never apply
        (False, GuardrailState.ENABLED, True),
        (True, GuardrailState.ENABLED, True),  # enabled -> always apply
        (True, GuardrailState.PENDING, True),  # partitioned + unconfirmed -> fail closed
        (False, GuardrailState.PENDING, False),  # connected + not yet promoted -> not applied
    ],
)
def test_should_apply_guardrail_fails_closed(
    partitioned: bool, state: GuardrailState, expected: bool
) -> None:
    assert should_apply_guardrail(partitioned, state) is expected


def test_partition_policy_is_asymmetric() -> None:
    # The defining property: under partition with no confirmation, routing stays up (open) while a
    # pending guardrail still blocks (closed).
    assert should_allow_routing(partitioned=True, have_cached_key=True) is True
    assert should_apply_guardrail(True, GuardrailState.PENDING) is True
