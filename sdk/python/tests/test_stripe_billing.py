# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.stripe_billing (CTO-90): metered billing, lifecycle, invoices."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tally.stripe_billing import (
    BillableUsage,
    BillingPlan,
    BillingService,
    DunningAction,
    DunningPolicy,
    FakeStripeClient,
    Invoice,
    Meter,
    PaymentEvent,
    SubscriptionStatus,
    build_invoice,
    cancel,
    change_plan,
    micro_to_cents,
    reconcile_invoice,
    record_payment_failure,
    record_payment_success,
)

UTC = timezone.utc
P_START = datetime(2026, 5, 1, tzinfo=UTC)
P_END = datetime(2026, 6, 1, tzinfo=UTC)

PRO = BillingPlan(
    name="pro",
    price_id="price_pro",
    metered={Meter.TRACE_COUNT: 50, Meter.FEATURE_COUNT: 1_000_000},
)
SCALE = BillingPlan(
    name="scale",
    price_id="price_scale",
    metered={Meter.TRACE_COUNT: 30, Meter.FEATURE_COUNT: 800_000},
)


def _usage(tenant, meter, qty, key):
    return BillableUsage(
        tenant_id=tenant,
        meter=meter,
        quantity=qty,
        period_start=P_START,
        period_end=P_END,
        idempotency_key=key,
    )


# --------------------------------------------------------------------------- #
# Money conversion
# --------------------------------------------------------------------------- #
def test_micro_to_cents():
    assert micro_to_cents(10_000) == 1  # 1 cent
    assert micro_to_cents(1_000_000) == 100  # $1
    assert micro_to_cents(0) == 0
    assert micro_to_cents(15_000) == 2  # 1.5 cents -> round half up to 2
    assert micro_to_cents(14_999) == 1


# --------------------------------------------------------------------------- #
# Plan validation
# --------------------------------------------------------------------------- #
def test_plan_validation():
    with pytest.raises(ValueError):
        BillingPlan(name="", price_id="p", metered={})
    with pytest.raises(ValueError):
        BillingPlan(name="x", price_id="", metered={})
    with pytest.raises(ValueError):
        BillingPlan(name="x", price_id="p", metered={Meter.TRACE_COUNT: -1})
    with pytest.raises(ValueError):
        BillingPlan(name="x", price_id="p", metered={"trace": 1})  # type: ignore[dict-item]


def test_plan_unit_price():
    assert PRO.unit_price_micro(Meter.TRACE_COUNT) == 50
    assert PRO.unit_price_micro(Meter.FEATURE_COUNT) == 1_000_000


# --------------------------------------------------------------------------- #
# BillableUsage validation
# --------------------------------------------------------------------------- #
def test_usage_validation():
    with pytest.raises(ValueError):
        _usage("", Meter.TRACE_COUNT, 1, "k")
    with pytest.raises(ValueError):
        _usage("t", Meter.TRACE_COUNT, -1, "k")
    with pytest.raises(ValueError):
        _usage("t", Meter.TRACE_COUNT, 1, "")
    with pytest.raises(ValueError):
        _usage("t", "trace", 1, "k")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _usage("t", Meter.TRACE_COUNT, True, "k")  # bool rejected


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #
def test_build_invoice_prices_usage():
    usage = {Meter.TRACE_COUNT: 1000, Meter.FEATURE_COUNT: 3}
    inv = build_invoice("in_1", "t1", "cus_1", usage, PRO)
    assert inv.total_micro_usd == 1000 * 50 + 3 * 1_000_000
    assert len(inv.lines) == 2
    # lines emitted in Meter declaration order
    assert inv.lines[0].meter is Meter.TRACE_COUNT
    assert inv.lines[1].meter is Meter.FEATURE_COUNT


def test_invoice_total_cents_and_summary_and_as_dict():
    inv = build_invoice("in_1", "t1", "cus_1", {Meter.TRACE_COUNT: 2000}, PRO)
    assert inv.total_micro_usd == 100_000
    assert inv.total_cents == 10
    assert "in_1" in inv.summary()
    d = inv.as_dict()
    assert d["total_micro_usd"] == 100_000
    assert d["total_cents"] == 10
    assert d["lines"][0]["meter"] == "trace_count"


def test_build_invoice_zero_quantity_line_kept():
    inv = build_invoice("in_1", "t1", "cus_1", {Meter.TRACE_COUNT: 0}, PRO)
    assert len(inv.lines) == 1
    assert inv.lines[0].amount_micro_usd == 0


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def test_reconcile_matches():
    usage = {Meter.TRACE_COUNT: 1000, Meter.FEATURE_COUNT: 3}
    inv = build_invoice("in_1", "t1", "cus_1", usage, PRO)
    res = reconcile_invoice(inv, usage, PRO)
    assert res.ok is True
    assert res.total_drift_micro_usd == 0
    assert res.line_drifts == {}


def test_reconcile_detects_drift():
    inv = build_invoice("in_1", "t1", "cus_1", {Meter.TRACE_COUNT: 1000}, PRO)
    # expected fewer traces than invoiced -> positive drift
    res = reconcile_invoice(inv, {Meter.TRACE_COUNT: 900}, PRO)
    assert res.ok is False
    assert res.line_drifts["trace_count"] == (1000 - 900) * 50
    assert res.total_drift_micro_usd == 5000
    assert res.as_dict()["ok"] is False


# --------------------------------------------------------------------------- #
# Subscription lifecycle (pure transitions)
# --------------------------------------------------------------------------- #
def _sub(status=SubscriptionStatus.ACTIVE, attempts=0, plan="pro"):
    from tally.stripe_billing import Subscription

    return Subscription(
        id="sub_1",
        tenant_id="t1",
        customer_id="cus_1",
        plan_name=plan,
        status=status,
        current_period_start=P_START,
        current_period_end=P_END,
        failed_payment_attempts=attempts,
    )


def test_change_plan_swaps_and_rejects_canceled():
    s = change_plan(_sub(), "scale")
    assert s.plan_name == "scale"
    with pytest.raises(ValueError):
        change_plan(_sub(status=SubscriptionStatus.CANCELED), "scale")
    with pytest.raises(ValueError):
        change_plan(_sub(), "")


def test_cancel_is_idempotent():
    s = cancel(_sub())
    assert s.is_canceled
    assert cancel(s).is_canceled


def test_payment_success_reactivates_and_clears_dunning():
    s = record_payment_success(_sub(status=SubscriptionStatus.PAST_DUE, attempts=2))
    assert s.status is SubscriptionStatus.ACTIVE
    assert s.failed_payment_attempts == 0


def test_payment_success_noop_on_canceled():
    s = _sub(status=SubscriptionStatus.CANCELED)
    assert record_payment_success(s) is s


def test_dunning_retries_then_cancels():
    pol = DunningPolicy(max_attempts=3, retry_schedule_days=(1, 3, 5))
    s = _sub()
    out1 = record_payment_failure(s, pol)
    assert out1.action is DunningAction.RETRY
    assert out1.attempt == 1
    assert out1.retry_in_days == 1
    assert out1.subscription.status is SubscriptionStatus.PAST_DUE

    out2 = record_payment_failure(out1.subscription, pol)
    assert out2.attempt == 2
    assert out2.retry_in_days == 3

    out3 = record_payment_failure(out2.subscription, pol)
    assert out3.attempt == 3
    assert out3.retry_in_days == 5
    assert out3.action is DunningAction.RETRY

    # 4th failure exceeds max_attempts -> cancel
    out4 = record_payment_failure(out3.subscription, pol)
    assert out4.action is DunningAction.CANCEL
    assert out4.subscription.is_canceled


def test_dunning_noop_on_canceled():
    out = record_payment_failure(_sub(status=SubscriptionStatus.CANCELED))
    assert out.action is DunningAction.CANCEL
    assert out.retry_in_days is None


def test_dunning_policy_validation_and_schedule():
    with pytest.raises(ValueError):
        DunningPolicy(max_attempts=0)
    pol = DunningPolicy(max_attempts=5, retry_schedule_days=(2,))
    assert pol.next_retry_day(1) == 2
    assert pol.next_retry_day(5) == 2  # clamps to last entry
    assert pol.next_retry_day(6) is None
    assert pol.next_retry_day(0) is None


# --------------------------------------------------------------------------- #
# End-to-end: signup -> usage -> invoice (test mode)
# --------------------------------------------------------------------------- #
def test_end_to_end_signup_usage_invoice():
    client = FakeStripeClient()
    svc = BillingService(client=client, plans=[PRO])
    sub = svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    assert sub.status is SubscriptionStatus.ACTIVE
    assert client.customer_count() == 1
    assert client.has_subscription(sub.id)

    assert svc.report_usage(_usage("t1", Meter.TRACE_COUNT, 1000, "k1")) is True
    assert svc.report_usage(_usage("t1", Meter.FEATURE_COUNT, 3, "k2")) is True

    inv = svc.finalize_invoice("t1")
    assert isinstance(inv, Invoice)
    assert inv.total_micro_usd == 1000 * 50 + 3 * 1_000_000
    res = reconcile_invoice(inv, svc.usage_for("t1"), PRO)
    assert res.ok is True


def test_report_usage_idempotent():
    svc = BillingService(plans=[PRO])
    svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    assert svc.report_usage(_usage("t1", Meter.TRACE_COUNT, 100, "dup")) is True
    assert svc.report_usage(_usage("t1", Meter.TRACE_COUNT, 100, "dup")) is False  # no double
    assert svc.usage_for("t1")[Meter.TRACE_COUNT] == 100


def test_trial_signup():
    svc = BillingService(plans=[PRO])
    sub = svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END, trial=True)
    assert sub.status is SubscriptionStatus.TRIALING
    assert sub.is_active


def test_change_plan_through_service_updates_stripe():
    client = FakeStripeClient()
    svc = BillingService(client=client, plans=[PRO, SCALE])
    svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    updated = svc.change_plan("t1", "scale")
    assert updated.plan_name == "scale"
    # subsequent invoice uses the new plan's prices
    svc.report_usage(_usage("t1", Meter.TRACE_COUNT, 100, "k"))
    inv = svc.finalize_invoice("t1")
    assert inv.total_micro_usd == 100 * 30  # SCALE trace price


def test_cancel_through_service():
    client = FakeStripeClient()
    svc = BillingService(client=client, plans=[PRO])
    sub = svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    updated = svc.cancel("t1")
    assert updated.is_canceled
    assert client.has_subscription(sub.id) is False


def test_handle_payment_events_drive_lifecycle():
    svc = BillingService(plans=[PRO], dunning=DunningPolicy(max_attempts=1))
    svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    s = svc.handle_payment_event("t1", PaymentEvent.PAYMENT_FAILED)
    assert s.status is SubscriptionStatus.PAST_DUE
    # max_attempts=1 -> next failure cancels
    s = svc.handle_payment_event("t1", PaymentEvent.PAYMENT_FAILED)
    assert s.is_canceled
    # success after cancel is a no-op
    s = svc.handle_payment_event("t1", PaymentEvent.PAYMENT_SUCCEEDED)
    assert s.is_canceled


def test_payment_success_recovers_past_due():
    svc = BillingService(plans=[PRO])
    svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    svc.handle_payment_event("t1", PaymentEvent.PAYMENT_FAILED)
    s = svc.handle_payment_event("t1", PaymentEvent.PAYMENT_SUCCEEDED)
    assert s.status is SubscriptionStatus.ACTIVE
    assert s.failed_payment_attempts == 0


def test_unknown_plan_and_missing_sub_raise():
    svc = BillingService(plans=[PRO])
    with pytest.raises(ValueError):
        svc.signup("t1", "a@b.co", "nope", period_start=P_START, period_end=P_END)
    with pytest.raises(ValueError):
        svc.finalize_invoice("ghost")
    with pytest.raises(ValueError):
        svc.cancel("ghost")


def test_report_usage_unbound_tenant_still_accumulates():
    # Billable data is never dropped, even with no subscription yet.
    svc = BillingService(plans=[PRO])
    assert svc.report_usage(_usage("t1", Meter.TRACE_COUNT, 7, "k")) is True
    assert svc.usage_for("t1")[Meter.TRACE_COUNT] == 7


def test_fake_client_dedupes_usage():
    c = FakeStripeClient()
    assert c.report_usage("sub_1", Meter.TRACE_COUNT, 10, "k") is True
    assert c.report_usage("sub_1", Meter.TRACE_COUNT, 10, "k") is False
    assert c.reported_quantity("sub_1", Meter.TRACE_COUNT) == 10


def test_subscription_as_dict():
    d = _sub().as_dict()
    assert d["status"] == "active"
    assert d["tenant_id"] == "t1"


def test_register_plan_after_construction():
    svc = BillingService()
    svc.register_plan(PRO)
    sub = svc.signup("t1", "a@b.co", "pro", period_start=P_START, period_end=P_END)
    assert sub.plan_name == "pro"
