# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.account_identity (CTO-195): routing revenue identity to AccountIdHash."""

from __future__ import annotations

import pytest

from tally.account_identity import (
    UNATTRIBUTED,
    AccountConflict,
    AccountLinker,
    hash_account_id,
    hubspot_account_id,
    stripe_account_id,
)
from tally.hmac_keys import HmacKeyRegistry

T = "t-acme"


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def test_stripe_account_id_is_the_customer():
    assert stripe_account_id({"customer": "cus_123", "amount": 4900}) == "cus_123"


def test_stripe_account_id_unwraps_an_expanded_customer_object():
    # Stripe expands `customer` into the full object when asked; stringifying that dict would
    # produce a hash that joins to nothing.
    assert stripe_account_id({"customer": {"id": "cus_123", "email": "a@b.com"}}) == "cus_123"


def test_stripe_account_id_absent_or_junk_is_none():
    assert stripe_account_id({}) is None
    assert stripe_account_id({"customer": ""}) is None
    assert stripe_account_id({"customer": None}) is None
    assert stripe_account_id({"customer": True}) is None
    assert stripe_account_id("not a mapping") is None  # type: ignore[arg-type]


def test_hubspot_prefers_the_associated_company_over_the_deal():
    # A company can win several deals. Hashing the deal id would mint a new "account" per
    # contract, so the company association always wins where it exists.
    event = {"objectId": 77, "properties": {"associatedcompanyid": 4242, "amount": 100}}
    assert hubspot_account_id(event) == "4242"


def test_hubspot_reads_a_company_id_off_the_event_or_associations_envelope():
    assert hubspot_account_id({"objectId": 77, "companyId": "c-9"}) == "c-9"
    assert (
        hubspot_account_id({"objectId": 77, "associations": {"company": {"id": "c-8"}}}) == "c-8"
    )


def test_hubspot_falls_back_to_the_deal_object_id():
    assert hubspot_account_id({"objectId": 77}) == "77"


def test_hubspot_with_nothing_identifying_is_none():
    assert hubspot_account_id({"propertyName": "dealstage"}) is None
    assert hubspot_account_id("nope") is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def test_hash_account_id_uses_the_per_tenant_versioned_key():
    registry = HmacKeyRegistry()
    stamped = hash_account_id(registry, T, "cus_123")
    assert stamped is not None
    assert len(stamped.value) == 64
    assert stamped.key_version == "v1"
    # Same identifier under a different tenant must not collide. That is the whole point of a
    # per-tenant key.
    other = hash_account_id(registry, "t-other", "cus_123")
    assert other is not None and other.value != stamped.value


def test_hash_account_id_preserves_case_and_strips_whitespace():
    registry = HmacKeyRegistry()
    lower = hash_account_id(registry, T, "cus_paying")
    upper = hash_account_id(registry, T, "cus_PaYiNg")
    padded = hash_account_id(registry, T, "  cus_PaYiNg  ")
    assert lower is not None and upper is not None and padded is not None
    assert lower.value != upper.value  # opaque provider ids are case-sensitive
    assert padded.value == upper.value


def test_hash_account_id_returns_none_for_nothing_to_hash():
    registry = HmacKeyRegistry()
    assert hash_account_id(registry, T, None) is None
    assert hash_account_id(registry, T, "") is None
    assert hash_account_id(registry, T, "   ") is None
    assert hash_account_id(registry, "", "cus_1") is None


def test_hash_account_id_survives_a_key_rotation_like_a_user_hash():
    registry = HmacKeyRegistry()
    before = hash_account_id(registry, T, "cus_123")
    registry.rotate(T)
    after = hash_account_id(registry, T, "cus_123")
    assert before is not None and after is not None
    assert (before.key_version, after.key_version) == ("v1", "v2")
    assert before.value != after.value


# --------------------------------------------------------------------------- #
# One user, one account
# --------------------------------------------------------------------------- #
def test_linker_learns_a_mapping_and_answers_with_it():
    linker = AccountLinker()
    assert linker.observe(T, "u1", "a1", source="stripe") is None
    assert linker.account_for(T, "u1") == "a1"


def test_linker_is_tenant_scoped():
    linker = AccountLinker()
    linker.observe(T, "u1", "a1")
    assert linker.account_for("t-other", "u1") == UNATTRIBUTED


def test_repeat_observation_of_the_same_pairing_is_not_a_conflict():
    linker = AccountLinker()
    linker.observe(T, "u1", "a1")
    assert linker.observe(T, "u1", "a1", source="stripe") is None
    assert linker.account_for(T, "u1") == "a1"


def test_a_second_account_for_one_user_attributes_nothing_and_raises_a_finding():
    # docs/cost-per-customer-plan.md, "Constraints decided": one user belongs to one account.
    # Where a user maps to more than one, attribute nothing rather than guess.
    linker = AccountLinker()
    linker.observe(T, "u1", "a1", source="stripe")
    conflict = linker.observe(T, "u1", "a2", source="hubspot")

    assert isinstance(conflict, AccountConflict)
    assert conflict.reason == "user_maps_to_multiple_accounts"
    assert conflict.known_account_id_hash == "a1"
    assert conflict.conflicting_account_id_hash == "a2"
    assert conflict.source == "hubspot"
    assert conflict.as_dict()["user_id_hash"] == "u1"

    # Neither account wins. The first is not kept and the second does not overwrite it.
    assert linker.account_for(T, "u1") == UNATTRIBUTED
    assert linker.is_ambiguous(T, "u1") is True
    assert linker.conflicts(T) == (conflict,)


def test_ambiguity_is_permanent_and_cannot_be_resurrected():
    linker = AccountLinker()
    linker.observe(T, "u1", "a1")
    linker.observe(T, "u1", "a2")
    # A third sighting must not quietly re-establish a mapping we already decided we cannot trust.
    assert linker.observe(T, "u1", "a1") is None
    assert linker.account_for(T, "u1") == UNATTRIBUTED


def test_linker_learns_nothing_from_a_half_identified_event():
    linker = AccountLinker()
    assert linker.observe(T, "", "a1") is None
    assert linker.observe(T, "u1", "") is None
    assert linker.observe("", "u1", "a1") is None
    assert linker.account_for(T, "u1") == UNATTRIBUTED


# --------------------------------------------------------------------------- #
# resolve(): the one call a connector makes
# --------------------------------------------------------------------------- #
def test_resolve_honours_a_stated_account_and_learns_from_it():
    linker = AccountLinker()
    res = linker.resolve(T, user_id_hash="u1", stated_account_id_hash="a1", source="stripe")
    assert res.account_id_hash == "a1"
    assert res.inferred is False
    assert res.is_attributed is True
    assert res.conflict is None
    assert linker.account_for(T, "u1") == "a1"


def test_resolve_infers_an_account_when_the_provider_stated_none():
    linker = AccountLinker()
    linker.observe(T, "u1", "a1", source="stripe")
    res = linker.resolve(T, user_id_hash="u1", stated_account_id_hash="", source="stripe")
    assert res.account_id_hash == "a1"
    assert res.inferred is True


def test_resolve_infers_nothing_for_an_unknown_or_ambiguous_user():
    linker = AccountLinker()
    unknown = linker.resolve(T, user_id_hash="u9", stated_account_id_hash="")
    assert unknown.account_id_hash == UNATTRIBUTED
    assert unknown.is_attributed is False

    linker.observe(T, "u1", "a1")
    linker.observe(T, "u1", "a2")
    ambiguous = linker.resolve(T, user_id_hash="u1", stated_account_id_hash="")
    assert ambiguous.account_id_hash == UNATTRIBUTED
    assert ambiguous.inferred is False
    # The finding was raised when the ambiguity was discovered; resolve does not re-raise it.
    assert ambiguous.conflict is None


def test_a_stated_account_is_still_stamped_when_the_user_is_ambiguous():
    # A Stripe invoice names the customer it was raised against. That is stated, not inferred, so
    # the ambiguity of the *person* on the event does not make the revenue unattributable.
    linker = AccountLinker()
    linker.observe(T, "u1", "a1")
    res = linker.resolve(T, user_id_hash="u1", stated_account_id_hash="a2", source="stripe")
    assert res.account_id_hash == "a2"
    assert res.inferred is False
    assert res.conflict is not None


@pytest.mark.parametrize("blank", ["", "   "])
def test_hash_account_id_blank_variants(blank: str):
    assert hash_account_id(HmacKeyRegistry(), T, blank) is None
