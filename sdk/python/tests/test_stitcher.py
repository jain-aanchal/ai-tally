"""Tests for the attribution stitcher (CTO-69, spec §7)."""

from __future__ import annotations

from datetime import datetime, timedelta

from tally.stitcher import (
    AttributionConfidence,
    AttributionRule,
    BusinessEvent,
    IdentityEdge,
    IdentityType,
    MemoryTouchStore,
    Stitcher,
    Touch,
    ValueType,
    restitch_on_new_edge,
)

T = "tenant_x"
NOW = datetime(2026, 5, 28, 12, 0, 0)


def _event(*, id_="ev1", user="u_alice", name="subscription_created", at=NOW, value=10_000_000):
    return BusinessEvent(
        business_event_id=id_,
        tenant_id=T,
        user_hash=user,
        event_name=name,
        occurred_at=at,
        value_amount_micro=value,
    )


def _touch(
    *,
    trace="tr1",
    user="u_alice",
    feature="research_agent",
    at=NOW - timedelta(hours=1),
    cost=200_000,
):
    return Touch(trace_id=trace, user_hash=user, feature_tag=feature, ts=at, cost_micro_usd=cost)


def _rule(*, name="subscription_created", feature="research_agent", days=30):
    return AttributionRule(event_name=name, feature_tag=feature, lookback_days=days)


# --- happy path ---------------------------------------------------------------------------------


def test_direct_attribution_when_user_matches():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch())  # type: ignore[union-attr]
    out = s.stitch(_event())
    assert len(out) == 1
    r = out[0]
    assert r.confidence is AttributionConfidence.DIRECT
    assert r.attributed_trace_id == "tr1"
    assert r.feature_tag == "research_agent"
    assert r.value_amount_micro == 10_000_000


def test_idempotent_upsert_not_duplicate():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch())  # type: ignore[union-attr]
    s.stitch(_event())
    s.stitch(_event())  # replay same event
    assert len(s.records) == 1


def test_per_feature_attribution_credits_each():
    s = Stitcher(
        rules=[_rule(feature="research_agent"), _rule(feature="inline_writer")],
        touches=MemoryTouchStore(),
    )
    s.touches.add_touch(T, _touch(feature="research_agent", trace="tr_r"))  # type: ignore[union-attr]
    s.touches.add_touch(T, _touch(feature="inline_writer", trace="tr_w"))  # type: ignore[union-attr]
    out = s.stitch(_event())
    assert {r.feature_tag for r in out} == {"research_agent", "inline_writer"}


# --- identity graph stitching --------------------------------------------------------------------


def test_anonymous_to_authenticated_stitch():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    # touch was made under anonymous id; conversion fires under authenticated user_hash
    s.touches.add_touch(T, _touch(user="u_anon"))  # type: ignore[union-attr]
    s.identity_graph.add_edge(
        T,
        IdentityEdge(
            a="u_anon",
            a_type=IdentityType.ANONYMOUS_ID,
            b="u_alice",
            b_type=IdentityType.USER_ID,
            observed_at=NOW - timedelta(minutes=30),
            source="cdp_alias",
        ),
    )
    out = s.stitch(_event(user="u_alice"))
    assert len(out) == 1
    assert out[0].confidence is AttributionConfidence.IDENTITY_GRAPH_STITCHED


def test_session_stitched_when_edge_is_session():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(user="u_alice_session"))  # type: ignore[union-attr]
    s.identity_graph.add_edge(
        T,
        IdentityEdge(
            a="u_alice",
            a_type=IdentityType.USER_ID,
            b="u_alice_session",
            b_type=IdentityType.SESSION_ID,
            observed_at=NOW - timedelta(minutes=30),
        ),
    )
    out = s.stitch(_event(user="u_alice"))
    assert out[0].confidence is AttributionConfidence.SESSION_STITCHED


def test_2_hop_identity_graph_resolves():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    # anonymous → user → cross-device user
    s.touches.add_touch(T, _touch(user="u_device_a"))  # type: ignore[union-attr]
    s.identity_graph.add_edge(
        T,
        IdentityEdge(
            "u_alice", IdentityType.USER_ID, "u_device_a", IdentityType.EXTERNAL_ID,
            NOW - timedelta(days=1),
        ),
    )
    s.identity_graph.add_edge(
        T,
        IdentityEdge(
            "u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID,
            NOW - timedelta(hours=2),
        ),
    )
    out = s.stitch(_event(user="u_anon"))
    assert len(out) == 1


def test_resolve_depth_is_bounded():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore(), identity_resolve_max_depth=1)
    s.touches.add_touch(T, _touch(user="u_far"))  # type: ignore[union-attr]
    s.identity_graph.add_edge(
        T,
        IdentityEdge("u_alice", IdentityType.USER_ID, "u_mid", IdentityType.USER_ID, NOW),
    )
    s.identity_graph.add_edge(
        T,
        IdentityEdge("u_mid", IdentityType.USER_ID, "u_far", IdentityType.USER_ID, NOW),
    )
    out = s.stitch(_event(user="u_alice"))
    assert out == []  # u_far is 2 hops away, blocked by depth=1


def test_resolve_does_not_use_edges_observed_after_event():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(user="u_other"))  # type: ignore[union-attr]
    # edge observed AFTER the event must not leak into the historical attribution
    s.identity_graph.add_edge(
        T,
        IdentityEdge(
            "u_alice", IdentityType.USER_ID, "u_other", IdentityType.USER_ID,
            NOW + timedelta(hours=1),
        ),
    )
    assert s.stitch(_event(user="u_alice")) == []


# --- window + unattributed -----------------------------------------------------------------------


def test_touch_outside_window_unattributed():
    s = Stitcher(rules=[_rule(days=30)], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(at=NOW - timedelta(days=45)))  # type: ignore[union-attr]
    out = s.stitch(_event())
    assert out == []
    assert (T, "ev1") in s.unattributed
    assert s.unattributed[(T, "ev1")].reason == "no_trace_in_window"


def test_uses_occurred_at_not_now():
    # touch is 25 days before the EVENT (which itself occurred a year ago) — still inside window
    occurred = NOW - timedelta(days=365)
    s = Stitcher(rules=[_rule(days=30)], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(at=occurred - timedelta(days=25)))  # type: ignore[union-attr]
    # stitched today, but window anchored at occurred_at
    out = s.stitch(_event(at=occurred), now=NOW)
    assert len(out) == 1


# --- tenant isolation ---------------------------------------------------------------------------


def test_identity_graph_does_not_leak_across_tenants():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(user="u_anon"))  # type: ignore[union-attr]
    # edge added under a *different* tenant must not connect u_anon→u_alice for T
    s.identity_graph.add_edge(
        "other_tenant",
        IdentityEdge(
            "u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID,
            NOW - timedelta(hours=1),
        ),
    )
    out = s.stitch(_event(user="u_alice"))
    assert out == []


# --- re-stitch on late edge ----------------------------------------------------------------------


def test_restitch_on_late_identity_edge():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch(user="u_anon"))  # type: ignore[union-attr]
    pending = [_event(user="u_alice")]
    # initial stitch fails (no edge)
    assert s.stitch(pending[0]) == []
    assert (T, "ev1") in s.unattributed
    # later, an alias edge arrives — re-stitch should attribute
    new_edge = IdentityEdge(
        "u_anon", IdentityType.ANONYMOUS_ID, "u_alice", IdentityType.USER_ID,
        observed_at=NOW + timedelta(hours=6),
    )
    out = restitch_on_new_edge(s, new_edge, pending, tenant_id=T)
    assert len(out) == 1
    assert (T, "ev1") not in s.unattributed
    assert (T, "ev1", "research_agent") in s.records


# --- refunds (negative value) flow through unchanged ---------------------------------------------


def test_refund_event_attributes_with_value_type_refund():
    s = Stitcher(rules=[_rule(name="refund")], touches=MemoryTouchStore())
    s.touches.add_touch(T, _touch())  # type: ignore[union-attr]
    refund = BusinessEvent(
        business_event_id="rf1",
        tenant_id=T,
        user_hash="u_alice",
        event_name="refund",
        occurred_at=NOW,
        value_amount_micro=-10_000_000,
        value_type=ValueType.REFUND,
    )
    out = s.stitch(refund)
    assert len(out) == 1 and out[0].value_type is ValueType.REFUND
    assert out[0].value_amount_micro == -10_000_000


# --- unknown event names are silently ignored ----------------------------------------------------


def test_unknown_event_name_is_ignored_not_unattributed():
    s = Stitcher(rules=[_rule()], touches=MemoryTouchStore())
    out = s.stitch(_event(name="random_event"))
    assert out == []
    assert s.unattributed == {}  # not "unattributed" — just not a value event
