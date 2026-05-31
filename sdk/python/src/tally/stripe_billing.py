"""Stripe billing integration (CTO-90).

Why this module exists
----------------------
Self-serve only works if the loop closes without a human: **usage → invoice →
payment**. This module turns the abstract billable usage a tenant accrues into
Stripe metered-billing calls, drives the subscription lifecycle (create,
upgrade/downgrade, cancel, dunning on failed payment), and reconciles the
resulting invoice back against the source usage so we can prove the customer was
charged for exactly what they used.

Design
------
* **Injected client.** Every Stripe side effect goes through the
  :class:`StripeClient` Protocol. The default :class:`FakeStripeClient`
  simulates Stripe entirely in memory, so the whole signup → usage → invoice
  path runs in tests (and local dev) with no network and no secret key. Wire a
  real adapter in production.
* **Independent.** Consumes an abstract :class:`BillableUsage` record rather
  than importing the ledger/meter modules (which live on their own branches), so
  this ships off ``main`` on its own.
* **Money.** Amounts are integer **micro-USD** internally (the codebase-wide
  invariant — see :mod:`tally.schema`). They convert to integer **cents** only
  at the Stripe boundary, since Stripe charges in the smallest currency unit.
* **Idempotency.** Usage reporting is keyed by the record's idempotency key, so
  retries never double-bill — mirroring the ledger's guarantee.

Nothing here logs or persists a Stripe secret key; the real client adapter is
responsible for holding credentials out of this layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from tally.schema import DEFAULT_CURRENCY, micro_to_usd

#: micro-USD per cent (1 cent = 1e-2 USD = 10_000 micro-USD).
_MICRO_PER_CENT = Decimal(10_000)


def micro_to_cents(micro_usd: int) -> int:
    """Convert integer micro-USD to integer cents (Stripe's smallest unit).

    Rounds half-up at the cent boundary. Stripe wants whole cents; sub-cent
    precision is carried in micro-USD right up to this boundary.
    """
    return int((Decimal(micro_usd) / _MICRO_PER_CENT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Meters, prices, plans
# --------------------------------------------------------------------------- #
class Meter(str, Enum):
    """The two billable meters (per spec CTO-81: per-trace + per-feature)."""

    TRACE_COUNT = "trace_count"
    FEATURE_COUNT = "feature_count"


@dataclass(frozen=True, slots=True)
class BillingPlan:
    """A purchasable plan mapped to its Stripe price + per-unit metered prices.

    ``price_id`` is the Stripe Price object id for the subscription. ``metered``
    maps each :class:`Meter` to a per-unit price in micro-USD. Limits/tiers are
    CTO-89's concern and deliberately not modelled here.
    """

    name: str
    price_id: str
    metered: Mapping[Meter, int]
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("plan name must be non-empty")
        if not self.price_id:
            raise ValueError("price_id must be non-empty")
        for meter, unit in self.metered.items():
            if not isinstance(meter, Meter):
                raise ValueError(f"metered keys must be Meter, got {meter!r}")
            if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
                raise ValueError(f"unit price for {meter} must be a non-negative int")

    def unit_price_micro(self, meter: Meter) -> int:
        """Per-unit price in micro-USD for *meter* (0 if the plan omits it)."""
        return int(self.metered.get(meter, 0))


# --------------------------------------------------------------------------- #
# Billable usage (abstract input)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BillableUsage:
    """One usage quantity for a tenant+meter over a billing period.

    ``idempotency_key`` makes reporting safe to retry. ``quantity`` is a count
    (traces or distinct features), never money.
    """

    tenant_id: str
    meter: Meter
    quantity: int
    period_start: datetime
    period_end: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(self.meter, Meter):
            raise ValueError(f"meter must be a Meter, got {self.meter!r}")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise ValueError("quantity must be an int")
        if self.quantity < 0:
            raise ValueError("quantity must be non-negative")
        if not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty")


# --------------------------------------------------------------------------- #
# Subscription lifecycle
# --------------------------------------------------------------------------- #
class SubscriptionStatus(str, Enum):
    """Lifecycle states a subscription moves through."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class Subscription:
    """Immutable subscription snapshot. Transitions return new instances."""

    id: str
    tenant_id: str
    customer_id: str
    plan_name: str
    status: SubscriptionStatus
    current_period_start: datetime
    current_period_end: datetime
    failed_payment_attempts: int = 0

    @property
    def is_active(self) -> bool:
        return self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)

    @property
    def is_canceled(self) -> bool:
        return self.status is SubscriptionStatus.CANCELED

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "customer_id": self.customer_id,
            "plan_name": self.plan_name,
            "status": self.status.value,
            "current_period_start": self.current_period_start.isoformat(),
            "current_period_end": self.current_period_end.isoformat(),
            "failed_payment_attempts": self.failed_payment_attempts,
        }


@dataclass(frozen=True, slots=True)
class DunningPolicy:
    """How failed payments are retried before the subscription is canceled.

    ``retry_schedule_days`` is the delay (days from the failed charge) for each
    successive attempt. Once attempts exceed ``max_attempts`` the subscription
    is canceled. A customer is **never** silently dropped — cancellation is an
    explicit, surfaced state.
    """

    max_attempts: int = 4
    retry_schedule_days: tuple[int, ...] = (1, 3, 5, 7)

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

    def next_retry_day(self, attempt: int) -> int | None:
        """Delay (days) before retry *attempt* (1-based), or None if past max."""
        if attempt < 1 or attempt > self.max_attempts:
            return None
        idx = min(attempt - 1, len(self.retry_schedule_days) - 1)
        return self.retry_schedule_days[idx]


class DunningAction(str, Enum):
    """What dunning decided after a failed payment."""

    RETRY = "retry"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class DunningOutcome:
    """Result of processing a failed payment."""

    subscription: Subscription
    action: DunningAction
    attempt: int
    retry_in_days: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "attempt": self.attempt,
            "retry_in_days": self.retry_in_days,
            "subscription": self.subscription.as_dict(),
        }


def activate(sub: Subscription) -> Subscription:
    """Move a subscription to ACTIVE and clear any failed-payment counter."""
    return replace(sub, status=SubscriptionStatus.ACTIVE, failed_payment_attempts=0)


def change_plan(sub: Subscription, new_plan_name: str) -> Subscription:
    """Upgrade/downgrade: swap the plan; limits take effect immediately.

    A canceled subscription cannot be re-planned (must re-subscribe).
    """
    if not new_plan_name:
        raise ValueError("new_plan_name must be non-empty")
    if sub.is_canceled:
        raise ValueError("cannot change plan on a canceled subscription")
    return replace(sub, plan_name=new_plan_name)


def cancel(sub: Subscription) -> Subscription:
    """Cancel the subscription (idempotent)."""
    return replace(sub, status=SubscriptionStatus.CANCELED)


def record_payment_success(sub: Subscription) -> Subscription:
    """A successful charge clears dunning and (re)activates the subscription."""
    if sub.is_canceled:
        return sub
    return activate(sub)


def record_payment_failure(
    sub: Subscription, policy: DunningPolicy | None = None
) -> DunningOutcome:
    """Process a failed charge through the dunning policy.

    Increments the attempt counter, moves to PAST_DUE and schedules a retry
    until ``max_attempts`` is exceeded, at which point the subscription is
    canceled. Never raises on a canceled input — returns a CANCEL no-op.
    """
    pol = policy if policy is not None else DunningPolicy()
    if sub.is_canceled:
        return DunningOutcome(sub, DunningAction.CANCEL, sub.failed_payment_attempts, None)
    attempt = sub.failed_payment_attempts + 1
    if attempt > pol.max_attempts:
        return DunningOutcome(cancel(sub), DunningAction.CANCEL, attempt, None)
    updated = replace(
        sub,
        status=SubscriptionStatus.PAST_DUE,
        failed_payment_attempts=attempt,
    )
    return DunningOutcome(updated, DunningAction.RETRY, attempt, pol.next_retry_day(attempt))


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One metered line on an invoice."""

    meter: Meter
    quantity: int
    unit_amount_micro_usd: int
    amount_micro_usd: int

    def as_dict(self) -> dict[str, object]:
        return {
            "meter": self.meter.value,
            "quantity": self.quantity,
            "unit_amount_micro_usd": self.unit_amount_micro_usd,
            "amount_micro_usd": self.amount_micro_usd,
            "amount_usd": str(micro_to_usd(self.amount_micro_usd)),
        }


@dataclass(frozen=True, slots=True)
class Invoice:
    """A finalized invoice for one tenant over one period."""

    id: str
    tenant_id: str
    customer_id: str
    lines: tuple[InvoiceLine, ...]
    currency: str = DEFAULT_CURRENCY

    @property
    def total_micro_usd(self) -> int:
        return sum(line.amount_micro_usd for line in self.lines)

    @property
    def total_cents(self) -> int:
        return micro_to_cents(self.total_micro_usd)

    def summary(self) -> str:
        return (
            f"invoice {self.id} for {self.tenant_id}: "
            f"{len(self.lines)} line(s), total ${micro_to_usd(self.total_micro_usd)}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "customer_id": self.customer_id,
            "currency": self.currency,
            "lines": [line.as_dict() for line in self.lines],
            "total_micro_usd": self.total_micro_usd,
            "total_cents": self.total_cents,
            "total_usd": str(micro_to_usd(self.total_micro_usd)),
        }


def build_invoice(
    invoice_id: str,
    tenant_id: str,
    customer_id: str,
    usage_by_meter: Mapping[Meter, int],
    plan: BillingPlan,
) -> Invoice:
    """Price *usage_by_meter* against *plan* into an :class:`Invoice`.

    Lines are emitted in :class:`Meter` declaration order for determinism. A
    meter with zero quantity is still emitted (a $0 line) so the invoice mirrors
    the meters the plan bills on.
    """
    lines: list[InvoiceLine] = []
    for meter in Meter:
        if meter not in usage_by_meter:
            continue
        qty = int(usage_by_meter[meter])
        unit = plan.unit_price_micro(meter)
        lines.append(
            InvoiceLine(
                meter=meter,
                quantity=qty,
                unit_amount_micro_usd=unit,
                amount_micro_usd=qty * unit,
            )
        )
    return Invoice(
        id=invoice_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        lines=tuple(lines),
        currency=plan.currency,
    )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Whether an invoice matches the expected usage priced against the plan."""

    ok: bool
    expected_total_micro_usd: int
    invoice_total_micro_usd: int
    line_drifts: Mapping[str, int]

    @property
    def total_drift_micro_usd(self) -> int:
        return self.invoice_total_micro_usd - self.expected_total_micro_usd

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "expected_total_micro_usd": self.expected_total_micro_usd,
            "invoice_total_micro_usd": self.invoice_total_micro_usd,
            "total_drift_micro_usd": self.total_drift_micro_usd,
            "line_drifts": dict(self.line_drifts),
        }


def reconcile_invoice(
    invoice: Invoice,
    expected_usage: Mapping[Meter, int],
    plan: BillingPlan,
) -> ReconciliationResult:
    """Verify *invoice* charges exactly what *expected_usage* priced on *plan*.

    Computes the expected per-line amount and reports any drift (invoice minus
    expected) per meter and in total. ``ok`` is True iff every line and the
    total match to the micro-USD.
    """
    invoiced: dict[Meter, int] = {line.meter: line.amount_micro_usd for line in invoice.lines}
    meters = set(invoiced) | set(expected_usage)
    line_drifts: dict[str, int] = {}
    expected_total = 0
    for meter in meters:
        expected_amount = int(expected_usage.get(meter, 0)) * plan.unit_price_micro(meter)
        expected_total += expected_amount
        drift = invoiced.get(meter, 0) - expected_amount
        if drift != 0:
            line_drifts[meter.value] = drift
    return ReconciliationResult(
        ok=not line_drifts and invoice.total_micro_usd == expected_total,
        expected_total_micro_usd=expected_total,
        invoice_total_micro_usd=invoice.total_micro_usd,
        line_drifts=line_drifts,
    )


# --------------------------------------------------------------------------- #
# Stripe client boundary
# --------------------------------------------------------------------------- #
@runtime_checkable
class StripeClient(Protocol):
    """The Stripe side effects this integration needs. Real adapter in prod."""

    def create_customer(self, tenant_id: str, email: str) -> str: ...

    def create_subscription(self, customer_id: str, price_id: str) -> str: ...

    def update_subscription(self, subscription_id: str, price_id: str) -> None: ...

    def cancel_subscription(self, subscription_id: str) -> None: ...

    def report_usage(
        self, subscription_id: str, meter: Meter, quantity: int, idempotency_key: str
    ) -> bool: ...


class FakeStripeClient:
    """In-memory Stripe simulator so the full path runs without network.

    Records customers/subscriptions and deduplicates usage by idempotency key.
    ``reported_quantity`` lets tests assert the metered total that reached
    "Stripe".
    """

    __slots__ = ("_customers", "_subscriptions", "_usage", "_seen_keys", "_seq")

    def __init__(self) -> None:
        self._customers: dict[str, dict[str, str]] = {}
        self._subscriptions: dict[str, dict[str, str]] = {}
        self._usage: dict[tuple[str, Meter], int] = {}
        self._seen_keys: set[str] = set()
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:06d}"

    def create_customer(self, tenant_id: str, email: str) -> str:
        cid = self._next_id("cus")
        self._customers[cid] = {"tenant_id": tenant_id, "email": email}
        return cid

    def create_subscription(self, customer_id: str, price_id: str) -> str:
        sid = self._next_id("sub")
        self._subscriptions[sid] = {"customer_id": customer_id, "price_id": price_id}
        return sid

    def update_subscription(self, subscription_id: str, price_id: str) -> None:
        if subscription_id in self._subscriptions:
            self._subscriptions[subscription_id]["price_id"] = price_id

    def cancel_subscription(self, subscription_id: str) -> None:
        self._subscriptions.pop(subscription_id, None)

    def report_usage(
        self, subscription_id: str, meter: Meter, quantity: int, idempotency_key: str
    ) -> bool:
        if idempotency_key in self._seen_keys:
            return False  # duplicate — already counted
        self._seen_keys.add(idempotency_key)
        key = (subscription_id, meter)
        self._usage[key] = self._usage.get(key, 0) + quantity
        return True

    # --- test/inspection helpers ---
    def reported_quantity(self, subscription_id: str, meter: Meter) -> int:
        return self._usage.get((subscription_id, meter), 0)

    def customer_count(self) -> int:
        return len(self._customers)

    def has_subscription(self, subscription_id: str) -> bool:
        return subscription_id in self._subscriptions


# --------------------------------------------------------------------------- #
# Subscription store
# --------------------------------------------------------------------------- #
@runtime_checkable
class SubscriptionStore(Protocol):
    """Persists the current subscription per tenant."""

    def get(self, tenant_id: str) -> Subscription | None: ...

    def put(self, subscription: Subscription) -> None: ...


class InMemorySubscriptionStore:
    """Default in-memory subscription store."""

    __slots__ = ("_by_tenant",)

    def __init__(self) -> None:
        self._by_tenant: dict[str, Subscription] = {}

    def get(self, tenant_id: str) -> Subscription | None:
        return self._by_tenant.get(tenant_id)

    def put(self, subscription: Subscription) -> None:
        self._by_tenant[subscription.tenant_id] = subscription


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
class PaymentEvent(str, Enum):
    """Inbound Stripe webhook events we react to."""

    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"


class BillingService:
    """Orchestrates signup → usage reporting → invoicing → payment lifecycle.

    Holds a :class:`StripeClient`, a :class:`SubscriptionStore`, a plan
    catalog, and a :class:`DunningPolicy`. All default to in-memory/fake impls
    so a test can drive the whole loop with zero configuration.
    """

    __slots__ = ("_client", "_store", "_plans", "_dunning", "_usage", "_invoice_seq")

    def __init__(
        self,
        client: StripeClient | None = None,
        store: SubscriptionStore | None = None,
        plans: Iterable[BillingPlan] = (),
        dunning: DunningPolicy | None = None,
    ) -> None:
        self._client: StripeClient = client if client is not None else FakeStripeClient()
        self._store: SubscriptionStore = store if store is not None else InMemorySubscriptionStore()
        self._plans: dict[str, BillingPlan] = {p.name: p for p in plans}
        self._dunning = dunning if dunning is not None else DunningPolicy()
        # accumulated reported usage per (tenant, meter) for invoicing
        self._usage: dict[tuple[str, Meter], int] = {}
        self._invoice_seq = 0

    def register_plan(self, plan: BillingPlan) -> None:
        self._plans[plan.name] = plan

    def _plan(self, name: str) -> BillingPlan:
        if name not in self._plans:
            raise ValueError(f"unknown plan: {name!r}")
        return self._plans[name]

    def signup(
        self,
        tenant_id: str,
        email: str,
        plan_name: str,
        *,
        period_start: datetime,
        period_end: datetime,
        trial: bool = False,
    ) -> Subscription:
        """Provision a customer + subscription for a new tenant."""
        plan = self._plan(plan_name)
        customer_id = self._client.create_customer(tenant_id, email)
        sub_id = self._client.create_subscription(customer_id, plan.price_id)
        status = SubscriptionStatus.TRIALING if trial else SubscriptionStatus.ACTIVE
        sub = Subscription(
            id=sub_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            plan_name=plan_name,
            status=status,
            current_period_start=_as_utc(period_start),
            current_period_end=_as_utc(period_end),
        )
        self._store.put(sub)
        return sub

    def report_usage(self, usage: BillableUsage) -> bool:
        """Report one usage record to Stripe (idempotent) and accumulate it.

        Returns True if newly counted, False if the idempotency key was a
        duplicate. Billable data is never dropped: an unknown tenant still
        accumulates locally so a later invoice is complete.
        """
        sub = self._store.get(usage.tenant_id)
        sub_id = sub.id if sub is not None else f"unbound:{usage.tenant_id}"
        counted = self._client.report_usage(
            sub_id, usage.meter, usage.quantity, usage.idempotency_key
        )
        if counted:
            key = (usage.tenant_id, usage.meter)
            self._usage[key] = self._usage.get(key, 0) + usage.quantity
        return counted

    def usage_for(self, tenant_id: str) -> dict[Meter, int]:
        """Accumulated reported usage per meter for a tenant."""
        return {
            meter: qty for (tid, meter), qty in self._usage.items() if tid == tenant_id and qty
        }

    def finalize_invoice(self, tenant_id: str) -> Invoice:
        """Price the tenant's accumulated usage into an invoice."""
        sub = self._store.get(tenant_id)
        if sub is None:
            raise ValueError(f"no subscription for tenant {tenant_id!r}")
        plan = self._plan(sub.plan_name)
        self._invoice_seq += 1
        return build_invoice(
            invoice_id=f"in_{self._invoice_seq:06d}",
            tenant_id=tenant_id,
            customer_id=sub.customer_id,
            usage_by_meter=self.usage_for(tenant_id),
            plan=plan,
        )

    def change_plan(self, tenant_id: str, new_plan_name: str) -> Subscription:
        """Upgrade/downgrade a tenant; new metered prices take effect at once."""
        sub = self._require_sub(tenant_id)
        plan = self._plan(new_plan_name)
        self._client.update_subscription(sub.id, plan.price_id)
        updated = change_plan(sub, new_plan_name)
        self._store.put(updated)
        return updated

    def cancel(self, tenant_id: str) -> Subscription:
        """Cancel a tenant's subscription."""
        sub = self._require_sub(tenant_id)
        self._client.cancel_subscription(sub.id)
        updated = cancel(sub)
        self._store.put(updated)
        return updated

    def handle_payment_event(self, tenant_id: str, event: PaymentEvent) -> Subscription:
        """Drive the lifecycle from an inbound payment webhook.

        Success → ACTIVE (dunning cleared). Failure → dunning, which either
        retries (PAST_DUE) or cancels once attempts are exhausted.
        """
        sub = self._require_sub(tenant_id)
        if event is PaymentEvent.PAYMENT_SUCCEEDED:
            updated = record_payment_success(sub)
        else:
            outcome = record_payment_failure(sub, self._dunning)
            updated = outcome.subscription
            if outcome.action is DunningAction.CANCEL and updated.is_canceled:
                self._client.cancel_subscription(sub.id)
        self._store.put(updated)
        return updated

    def _require_sub(self, tenant_id: str) -> Subscription:
        sub = self._store.get(tenant_id)
        if sub is None:
            raise ValueError(f"no subscription for tenant {tenant_id!r}")
        return sub


def _as_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC; normalise aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


#: A small default catalog usable in dev/tests (prices in micro-USD per unit).
DEFAULT_PLANS: tuple[BillingPlan, ...] = (
    BillingPlan(
        name="pro",
        price_id="price_pro",
        metered={Meter.TRACE_COUNT: 50, Meter.FEATURE_COUNT: 1_000_000},
    ),
)
