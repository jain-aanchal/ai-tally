# SPDX-License-Identifier: Apache-2.0
"""Account identity for the revenue connectors, Stripe and HubSpot (CTO-195 / plan E2).

Why this module exists
----------------------
Account-level revenue has been arriving since CTO-68 and nothing has been able to use it.
``StripeConnector`` sets ``user_id`` to the Stripe **customer** id, and in B2B SaaS a Stripe
Customer *is* the account: the subscription, the invoices and the contract all hang off it.
``HubSpotConnector`` does the same with a deal or company ``objectId``. Both were being hashed
into ``UserIdHash`` as though they were end users.

That is a real attribution bug, not a cosmetic one. Cost spans hash an *end user* id; revenue
events hash a *customer* id. The two only join when a tenant happens to use one identifier for
both, which is why margin renders blank for everyone else. Routing the provider's account
identifier into ``AccountIdHash`` (CTO-180) is what makes revenue joinable to cost per customer,
and it needs no new connector and no new ingestion path.

What this module does NOT do
----------------------------
It does not change what ``UserIdHash`` means. Existing tenants may depend on the current
per-user attribution, and silently repurposing a populated column would corrupt attribution for
every one of them with no way to tell after the fact. ``AccountIdHash`` is a *separate*, additive
column defaulting to ``''``; both fields are populated and callers pick the one they mean.

One user belongs to one account
-------------------------------
Per ``docs/cost-per-customer-plan.md`` ("Constraints decided") multi-account users are not
supported. Two directions have to be told apart:

* **Stated.** An invoice names its Stripe customer; a closed-won deal names its company. The
  provider asserted the account for that event, so it is stamped as given. Nothing is guessed.
* **Inferred.** A revenue event that carries a user but no account can only get one by looking up
  what account that user was previously seen against. :class:`AccountLinker` holds that mapping,
  and the moment a user is observed against a second account it refuses to answer for that user
  forever after and records an :class:`AccountConflict`. The event lands unattributed and the
  conflict surfaces as a data-quality finding, which is the outcome the plan asks for: attribute
  nothing rather than guess.

Hashing goes through :mod:`tally.hmac_keys`, the same per-tenant versioned-key path used for
``UserIdHash`` (CTO-74). Raw account ids never reach storage and an account hash is not joinable
across tenants.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tally.hmac_keys import HmacKeyRegistry, StampedHash

#: The honest "no account" value. Matches ``AccountIdHash``'s DEFAULT '' in the ClickHouse DDL,
#: which the UI renders as the unattributed bucket rather than as a customer named "unknown".
UNATTRIBUTED = ""


# --------------------------------------------------------------------------- #
# Extracting the account identifier from a provider payload
# --------------------------------------------------------------------------- #
def _first_str(source: Mapping[str, object], *keys: str) -> str | None:
    """First key present with a non-empty scalar value, stringified. ``None`` otherwise.

    Ids arrive as strings from Stripe and as bare integers from HubSpot, so numbers are accepted
    and stringified. Booleans are rejected: ``True`` is never an id, and Python would otherwise
    happily render it as ``"True"`` and hash it.
    """
    for key in keys:
        value = source.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif isinstance(value, (int, float)):
            return str(value)
    return None


def stripe_account_id(obj: Mapping[str, object]) -> str | None:
    """The account identifier on a Stripe event's ``data.object``: the customer id.

    ``customer`` is the Stripe Customer the charge, invoice, checkout session or subscription
    belongs to, and that Customer is the tenant's account. Stripe expands the field to a nested
    object when the caller asks it to, so an expanded ``{"id": "cus_..."}`` is unwrapped rather
    than stringified into garbage.
    """
    if not isinstance(obj, Mapping):
        return None
    customer = obj.get("customer")
    if isinstance(customer, Mapping):
        return _first_str(customer, "id")
    return _first_str(obj, "customer")


#: HubSpot account-identifier keys in descending order of fidelity. A company is the account.
#: A deal is not, strictly: one company can win several deals, and hashing the deal id would mint
#: a fresh "account" per contract. So a company association is always preferred and the deal id is
#: only the last resort, for the tenant whose webhook carries no association at all. That fallback
#: is still a large improvement on today, where the deal id lands in ``UserIdHash``.
_HUBSPOT_COMPANY_KEYS: tuple[str, ...] = (
    "associatedcompanyid",
    "associatedCompanyId",
    "companyId",
    "company_id",
    "hs_object_id_company",
)


def hubspot_account_id(event: Mapping[str, object]) -> str | None:
    """The account identifier on a HubSpot deal/company webhook payload.

    Prefers, in order: an associated company id on ``properties``, one on the event itself, one
    under an ``associations`` envelope, and finally the deal ``objectId``. Returns ``None`` when
    the payload names nothing at all.
    """
    if not isinstance(event, Mapping):
        return None

    props = event.get("properties")
    if isinstance(props, Mapping):
        found = _first_str(props, *_HUBSPOT_COMPANY_KEYS)
        if found:
            return found

    found = _first_str(event, *_HUBSPOT_COMPANY_KEYS)
    if found:
        return found

    associations = event.get("associations")
    if isinstance(associations, Mapping):
        company = associations.get("company") or associations.get("companies")
        if isinstance(company, Mapping):
            found = _first_str(company, "id", "companyId")
            if found:
                return found

    # Last resort: the object the notification is about. For a dealstage change that is the deal.
    return _first_str(event, "objectId", "object_id")


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def hash_account_id(
    registry: HmacKeyRegistry, tenant_id: str, account_id: str | None
) -> StampedHash | None:
    """HMAC an account id under the tenant's active key. ``None`` in / unusable in → ``None`` out.

    Deliberately the same call the user-id path makes, so an account hash inherits versioning and
    Option B rotation for free (CTO-74). Whitespace is stripped; case is preserved, because a
    provider object id is case-sensitive (``cus_PaYiNg`` is not ``cus_paying``), unlike the email
    the Stripe path lowercases before hashing.
    """
    if not account_id:
        return None
    text = account_id.strip()
    if not text or not tenant_id:
        return None
    registry.provision(tenant_id)
    return registry.hash(tenant_id, text)


# --------------------------------------------------------------------------- #
# One user, one account
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AccountConflict:
    """A data-quality finding: one user observed against two different accounts.

    Carries hashes only. The raw ids are never held, so a finding is safe to log and to hand to
    the data-quality surface. ``source`` names the connector that produced the second observation
    so an operator knows where to look.
    """

    tenant_id: str
    user_id_hash: str
    known_account_id_hash: str
    conflicting_account_id_hash: str
    source: str

    @property
    def reason(self) -> str:
        return "user_maps_to_multiple_accounts"

    def as_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "tenant_id": self.tenant_id,
            "user_id_hash": self.user_id_hash,
            "known_account_id_hash": self.known_account_id_hash,
            "conflicting_account_id_hash": self.conflicting_account_id_hash,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class AccountResolution:
    """What to stamp on one revenue event, and whether resolving it raised a finding.

    ``account_id_hash`` is :data:`UNATTRIBUTED` when no account could be established honestly.
    ``inferred`` is True when the account came from the user→account map rather than from the
    provider payload, which is the distinction the UI needs in order to mark stitched accounts
    apart from stated ones (plan B5).
    """

    account_id_hash: str
    inferred: bool = False
    conflict: AccountConflict | None = None

    @property
    def is_attributed(self) -> bool:
        return self.account_id_hash != UNATTRIBUTED


class AccountLinker:
    """Per-tenant user→account map that refuses to guess (plan "Constraints decided").

    Learns from revenue events that state both a user and an account. The first account wins; a
    second, different account does not overwrite it and does not get averaged with it. Instead
    the user is marked ambiguous permanently and every later inference for that user returns
    :data:`UNATTRIBUTED`. Re-observing the same pairing is a no-op, so redelivered webhooks are
    harmless.

    In-process and per-tenant, in the same spirit as the Stripe webhook's in-memory dedup set: it
    accelerates and guards the common case within a worker's lifetime, and losing it on restart
    costs correctness nothing, because a lost mapping yields an honest blank rather than a wrong
    account.
    """

    __slots__ = ("_by_user", "_ambiguous", "_conflicts")

    def __init__(self) -> None:
        self._by_user: dict[str, dict[str, str]] = {}
        self._ambiguous: dict[str, set[str]] = {}
        self._conflicts: dict[str, list[AccountConflict]] = {}

    # --- learning -----------------------------------------------------------
    def observe(
        self, tenant_id: str, user_id_hash: str, account_id_hash: str, *, source: str = ""
    ) -> AccountConflict | None:
        """Record that ``user_id_hash`` belongs to ``account_id_hash``.

        Returns an :class:`AccountConflict` when this contradicts a previous observation, in which
        case the user becomes permanently ambiguous. Returns ``None`` otherwise, including when
        either hash is blank (nothing to learn from a half-identified event).
        """
        if not tenant_id or not user_id_hash or not account_id_hash:
            return None
        known = self._by_user.setdefault(tenant_id, {})
        previous = known.get(user_id_hash)
        if previous is None:
            if user_id_hash in self._ambiguous.get(tenant_id, frozenset()):
                # Already poisoned by an earlier conflict. Do not let a third observation
                # resurrect a mapping we have already decided we cannot trust.
                return None
            known[user_id_hash] = account_id_hash
            return None
        if previous == account_id_hash:
            return None

        conflict = AccountConflict(
            tenant_id=tenant_id,
            user_id_hash=user_id_hash,
            known_account_id_hash=previous,
            conflicting_account_id_hash=account_id_hash,
            source=source,
        )
        known.pop(user_id_hash, None)
        self._ambiguous.setdefault(tenant_id, set()).add(user_id_hash)
        self._conflicts.setdefault(tenant_id, []).append(conflict)
        return conflict

    # --- inference ----------------------------------------------------------
    def account_for(self, tenant_id: str, user_id_hash: str) -> str:
        """The account this user belongs to, or :data:`UNATTRIBUTED` if unknown or ambiguous."""
        if not tenant_id or not user_id_hash:
            return UNATTRIBUTED
        if user_id_hash in self._ambiguous.get(tenant_id, frozenset()):
            return UNATTRIBUTED
        return self._by_user.get(tenant_id, {}).get(user_id_hash, UNATTRIBUTED)

    def is_ambiguous(self, tenant_id: str, user_id_hash: str) -> bool:
        return user_id_hash in self._ambiguous.get(tenant_id, frozenset())

    def conflicts(self, tenant_id: str) -> tuple[AccountConflict, ...]:
        """Findings raised for this tenant, oldest first."""
        return tuple(self._conflicts.get(tenant_id, ()))

    # --- the one call a connector makes -------------------------------------
    def resolve(
        self,
        tenant_id: str,
        *,
        user_id_hash: str,
        stated_account_id_hash: str,
        source: str = "",
    ) -> AccountResolution:
        """Decide the ``AccountIdHash`` for one revenue event and learn from it.

        A stated account is always honoured and is also fed back into the map. With no stated
        account the map is consulted, and an ambiguous user yields :data:`UNATTRIBUTED` plus no
        finding (the finding was already raised when the ambiguity was discovered).
        """
        if stated_account_id_hash:
            conflict = self.observe(
                tenant_id, user_id_hash, stated_account_id_hash, source=source
            )
            return AccountResolution(stated_account_id_hash, inferred=False, conflict=conflict)
        inferred = self.account_for(tenant_id, user_id_hash)
        return AccountResolution(inferred, inferred=bool(inferred))
