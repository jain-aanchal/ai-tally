# SPDX-License-Identifier: Apache-2.0
"""Identity graph: transitive bounded-depth resolution, ingestion, key-version bridge (CTO-67)."""

from __future__ import annotations

from datetime import datetime, timedelta

from tally.identity import (
    KEY_ROTATION_SOURCE,
    PERSON_IDENTITY_TYPES,
    AliasEvent,
    IdentifyEvent,
    IdentityEdge,
    IdentityGraph,
    IdentityType,
)

NOW = datetime(2026, 5, 30, 12, 0, 0)
T = "t-acme"


def _g() -> IdentityGraph:
    return IdentityGraph()


# --- resolution -----------------------------------------------------------------------------------


def test_resolve_self_only_when_no_edges() -> None:
    g = _g()
    assert g.resolve_identity(T, "u_alice") == {"u_alice"}


def test_resolve_one_hop() -> None:
    g = _g()
    g.add_edge(
        T, IdentityEdge("u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID, NOW)
    )
    assert g.resolve_identity(T, "u_anon") == {"u_anon", "u_alice"}


def test_resolve_respects_max_depth() -> None:
    g = _g()
    g.add_edge(T, IdentityEdge("a", IdentityType.ANONYMOUS_ID, "b", IdentityType.USER_ID, NOW))
    g.add_edge(T, IdentityEdge("b", IdentityType.USER_ID, "c", IdentityType.SESSION_ID, NOW))
    g.add_edge(T, IdentityEdge("c", IdentityType.SESSION_ID, "d", IdentityType.USER_ID, NOW))
    # depth 2 from "a" reaches b (1) and c (2) but not d (3).
    assert g.resolve_identity(T, "a", max_depth=2) == {"a", "b", "c"}
    assert "d" not in g.resolve_identity(T, "a", max_depth=2)
    assert "d" in g.resolve_identity(T, "a", max_depth=3)


def test_resolve_handles_cycles() -> None:
    g = _g()
    g.add_edge(T, IdentityEdge("a", IdentityType.USER_ID, "b", IdentityType.USER_ID, NOW))
    g.add_edge(T, IdentityEdge("b", IdentityType.USER_ID, "a", IdentityType.USER_ID, NOW))
    # a self-referential cycle must terminate, not loop forever.
    assert g.resolve_identity(T, "a", max_depth=5) == {"a", "b"}


def test_resolve_is_tenant_scoped() -> None:
    g = _g()
    g.add_edge(
        T, IdentityEdge("u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID, NOW)
    )
    # A different tenant must not see t-acme's edges (no cross-tenant leak).
    assert g.resolve_identity("t-other", "u_anon") == {"u_anon"}


def test_resolve_as_of_ignores_future_edges() -> None:
    g = _g()
    later = NOW + timedelta(days=1)
    g.add_edge(
        T, IdentityEdge("u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID, later)
    )
    # An edge observed after the as_of cutoff must not leak into a historical resolution.
    assert g.resolve_identity(T, "u_anon", as_of=NOW) == {"u_anon"}
    assert g.resolve_identity(T, "u_anon", as_of=later) == {"u_anon", "u_alice"}


def test_resolve_alias_is_back_compat() -> None:
    g = _g()
    g.add_edge(T, IdentityEdge("a", IdentityType.USER_ID, "b", IdentityType.USER_ID, NOW))
    assert g.resolve(T, "a") == g.resolve_identity(T, "a")  # stitcher calls .resolve


# --- ingestion ------------------------------------------------------------------------------------


def test_ingest_identify_links_anonymous_and_session_to_user() -> None:
    g = _g()
    n = g.ingest_identify(
        T,
        IdentifyEvent(
            user_id="u_alice", anonymous_id="anon_1", session_id="sess_1", observed_at=NOW
        ),
    )
    assert n == 2
    resolved = g.resolve_identity(T, "anon_1", max_depth=2)
    assert {"anon_1", "u_alice", "sess_1"} <= resolved


def test_ingest_identify_without_anonymous_only_session() -> None:
    g = _g()
    n = g.ingest_identify(T, IdentifyEvent(user_id="u_alice", session_id="sess_1", observed_at=NOW))
    assert n == 1
    assert g.resolve_identity(T, "sess_1") == {"sess_1", "u_alice"}


def test_ingest_alias_merges_two_ids() -> None:
    g = _g()
    g.ingest_alias(
        T,
        AliasEvent(
            previous_id="anon_1",
            previous_type=IdentityType.ANONYMOUS_ID,
            new_id="u_alice",
            new_type=IdentityType.USER_ID,
            observed_at=NOW,
        ),
    )
    assert g.resolve_identity(T, "anon_1") == {"anon_1", "u_alice"}


# --- key-version bridge ---------------------------------------------------------------------------


def test_bridge_key_versions_relinks_rotated_hashes() -> None:
    g = _g()
    # Same user, hashed under v1 then v2 after a key rotation (CTO-74).
    g.bridge_key_versions(
        T, "u_alice_v1", "u_alice_v2", NOW, old_key_version="v1", new_key_version="v2"
    )
    assert g.resolve_identity(T, "u_alice_v1") == {"u_alice_v1", "u_alice_v2"}
    assert g.edge_type_between(T, "u_alice_v1", "u_alice_v2") is IdentityType.USER_ID


def test_key_rotation_edge_tagged_with_source() -> None:
    g = _g()
    g.bridge_key_versions(T, "old", "new", NOW)
    # Bridge transitively reaches conversion traces hashed under either key version.
    g.add_edge(T, IdentityEdge("anon", IdentityType.ANONYMOUS_ID, "old", IdentityType.USER_ID, NOW))
    assert {"anon", "old", "new"} <= g.resolve_identity(T, "anon", max_depth=2)
    assert KEY_ROTATION_SOURCE == "key_rotation"


# --- late alias re-links prior identity (the headline CTO-67 case) --------------------------------


def test_late_alias_relinks_anonymous_to_authenticated() -> None:
    g = _g()
    # Before login, only the anonymous id is known — resolution is just itself.
    assert g.resolve_identity(T, "anon_1") == {"anon_1"}
    # An identify/alias arrives after the conversion: now the prior anonymous traces re-link.
    g.ingest_identify(T, IdentifyEvent(user_id="u_alice", anonymous_id="anon_1", observed_at=NOW))
    assert g.resolve_identity(T, "anon_1") == {"anon_1", "u_alice"}
    assert g.resolve_identity(T, "u_alice") == {"u_alice", "anon_1"}


# --- account_id in the identity graph (CTO-184) --------------------------------------------------
#
# One user belongs to one account. Multi-account users are explicitly NOT supported: where a person
# resolves to more than one account the pipeline attributes nothing and raises a data-quality
# finding, because splitting or duplicating the cost would inflate the tenant total and break
# reconciliation against /cost.


def _account_edge(user: str, account: str, when: datetime = NOW) -> IdentityEdge:
    return IdentityEdge(
        user, IdentityType.USER_ID, account, IdentityType.ACCOUNT_ID, when, source="crm"
    )


def test_account_id_is_a_distinct_identity_type() -> None:
    assert IdentityType.ACCOUNT_ID.value == "account_id"
    assert IdentityType.ACCOUNT_ID not in PERSON_IDENTITY_TYPES


def test_single_account_resolves() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    res = g.resolve_account(T, "u_alice")
    assert res.account_hash == "acct_widgets"
    assert res.attributable is True
    assert res.conflict is False


def test_unstitched_user_resolves_to_nothing_without_conflict() -> None:
    g = _g()
    res = g.resolve_account(T, "u_alice")
    assert res.account_hash is None
    assert res.candidates == ()
    # Not stitched yet is a normal state, NOT a data-quality finding.
    assert res.conflict is False
    assert res.attributable is False


def test_account_resolves_transitively_through_person_identities() -> None:
    g = _g()
    # The CRM asserts the account against the user; the SDK knows the anonymous id is that user.
    g.ingest_identify(T, IdentifyEvent(user_id="u_alice", anonymous_id="anon_1", observed_at=NOW))
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    assert g.resolve_account(T, "anon_1").account_hash == "acct_widgets"


def test_multi_account_user_attributes_nothing() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    g.add_edge(T, _account_edge("u_alice", "acct_gadgets"))
    res = g.resolve_account(T, "u_alice")
    # Not split, not duplicated, not first-seen. Nothing.
    assert res.account_hash is None
    assert res.attributable is False
    assert res.conflict is True
    assert res.candidates == ("acct_gadgets", "acct_widgets")


def test_multi_account_conflict_is_reported_as_a_finding() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    g.add_edge(T, _account_edge("u_alice", "acct_gadgets"))
    g.add_edge(T, _account_edge("u_bob", "acct_widgets"))
    findings = g.account_conflicts(T)
    # Visible, not silently swallowed: exactly the one ambiguous user, with both candidates.
    assert [f.user_hash for f in findings] == ["u_alice"]
    assert findings[0].candidates == ("acct_gadgets", "acct_widgets")


def test_no_conflict_when_every_user_has_one_account() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    g.add_edge(T, _account_edge("u_bob", "acct_gadgets"))
    assert g.account_conflicts(T) == []


def test_account_conflicts_do_not_leak_across_tenants() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    g.add_edge("t-other", _account_edge("u_alice", "acct_gadgets"))
    # Same user hash in two tenants is two different people; neither is ambiguous.
    assert g.account_conflicts(T) == []
    assert g.resolve_account(T, "u_alice").account_hash == "acct_widgets"
    assert g.resolve_account("t-other", "u_alice").account_hash == "acct_gadgets"


def test_colleagues_are_not_fused_into_one_person() -> None:
    # The hazard that makes account_id different from every other identity type: an account is a
    # container, not a person. If resolution walked THROUGH the account node, Alice and Bob would
    # come back as the same identity and Bob's trace could be attributed to Alice's conversion.
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    g.add_edge(T, _account_edge("u_bob", "acct_widgets"))
    assert g.resolve_identity(T, "u_alice", max_depth=3) == {"u_alice"}


def test_account_edges_are_excluded_from_person_identity_sets() -> None:
    # The account hash must not end up in the set the stitcher uses to query last-touch, or every
    # span belonging to any colleague would match.
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets"))
    assert g.resolve_identity(T, "u_alice") == {"u_alice"}


def test_account_edge_observed_after_as_of_is_ignored() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets", NOW + timedelta(days=1)))
    # Honest under uncertainty: an edge we had not learned yet cannot retroactively attribute.
    assert g.resolve_account(T, "u_alice", as_of=NOW).account_hash is None


def test_as_of_can_resolve_a_conflict_that_only_appears_later() -> None:
    g = _g()
    g.add_edge(T, _account_edge("u_alice", "acct_widgets", NOW - timedelta(days=1)))
    g.add_edge(T, _account_edge("u_alice", "acct_gadgets", NOW + timedelta(days=1)))
    assert g.resolve_account(T, "u_alice", as_of=NOW).account_hash == "acct_widgets"
    assert g.resolve_account(T, "u_alice").conflict is True
