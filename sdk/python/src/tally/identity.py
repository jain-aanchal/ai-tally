# SPDX-License-Identifier: Apache-2.0
"""Identity graph + resolution — transitive, bounded depth (CTO-67 / spec §7 identity correction).

Attribution's highest-value case — anonymous→authenticated conversion — only works with an identity
graph. A naive ``user_id`` join silently loses the pre-login traces. This module is the canonical,
transport-agnostic home for that graph; the stitcher (CTO-69) consumes it.

The graph is **undirected, tenant-scoped, and over hashed IDs only** (no raw PII ever). It is
populated from two event kinds that any SDK or CDP emits:

* **identify** — ties an ``anonymous_id`` (and optional ``session_id``) to a ``user_id`` at login.
* **alias** — merges two ids the product knows are the same person (e.g. a CDP ``alias`` call).

:meth:`IdentityGraph.resolve_identity` does a bounded-depth (default 2) transitive walk that bridges
``anonymous_id ↔ user_id ↔ session_id`` and across **HMAC key versions** (CTO-74 rotates the user-id
hashing key; the same person hashes differently under v1 vs v2, so a key-rotation edge re-links
them). Bounded depth + a visited-set keep cycles and runaway fan-out in check.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IdentityType(str, Enum):
    USER_ID = "user_id"
    ANONYMOUS_ID = "anonymous_id"
    SESSION_ID = "session_id"
    EMAIL = "email"
    EXTERNAL_ID = "external_id"
    #: The tenant's own paying customer (CTO-184), hashed like every other identity here.
    #:
    #: Unlike the five above, an account is NOT a person. It is a container that many people
    #: belong to, which is why it is deliberately excluded from person-identity traversal in
    #: :meth:`IdentityGraph.resolve_identity` and has its own resolver,
    #: :meth:`IdentityGraph.resolve_account`. Values mirror the ClickHouse
    #: ``identity_graph.IdentityAType`` enum in ``db/clickhouse/attribution.sql``.
    ACCOUNT_ID = "account_id"


#: Identity types that denote a *person*. Traversal in :meth:`IdentityGraph.resolve_identity` is
#: confined to these, because the whole point of that walk is "which hashes are the same human".
PERSON_IDENTITY_TYPES = frozenset(
    {
        IdentityType.USER_ID,
        IdentityType.ANONYMOUS_ID,
        IdentityType.SESSION_ID,
        IdentityType.EMAIL,
        IdentityType.EXTERNAL_ID,
    }
)


# Source tag for a synthetic edge linking the same identity across HMAC key versions (CTO-74).
KEY_ROTATION_SOURCE = "key_rotation"


@dataclass(frozen=True, slots=True)
class IdentityEdge:
    """An edge in the identity graph (hashed identities only)."""

    a: str
    a_type: IdentityType
    b: str
    b_type: IdentityType
    observed_at: datetime
    source: str = "sdk"
    confidence: float = 1.0
    key_version: str = "v1"


@dataclass(frozen=True, slots=True)
class AccountResolution:
    """The outcome of asking "which account does this person belong to?" (CTO-184).

    Three states, and the caller must handle all three distinctly:

    * ``account_hash`` set                 -> exactly one account. Attribute to it, as *stitched*.
    * ``account_hash is None``, 0 candidates -> nobody has stitched this person yet. Normal.
    * ``account_hash is None``, 2+ candidates -> ``conflict``. Attribute NOTHING and raise a
      data-quality finding. Never split, duplicate, or pick first-seen.

    ``account_hash`` is deliberately ``None`` rather than an empty string in both no-attribution
    cases, so a caller cannot accidentally write it into a column whose empty value already means
    "unattributed" and lose the distinction between "not stitched" and "refused to guess".
    """

    user_hash: str
    account_hash: str | None
    candidates: tuple[str, ...] = ()

    @property
    def conflict(self) -> bool:
        """True when this person was observed against more than one account."""
        return len(self.candidates) > 1

    @property
    def attributable(self) -> bool:
        """True only when exactly one account resolved."""
        return self.account_hash is not None


@dataclass(frozen=True, slots=True)
class IdentifyEvent:
    """SDK/CDP ``identify``: an anonymous visitor logs in and becomes a known user.

    Produces the ``anonymous_id ↔ user_id`` link (and ``session_id ↔ user_id`` when present) — the
    edges that let a conversion attributed to ``user_id`` reach back to pre-login anonymous traces.
    """

    user_id: str
    observed_at: datetime
    anonymous_id: str | None = None
    session_id: str | None = None
    source: str = "sdk"
    key_version: str = "v1"


@dataclass(frozen=True, slots=True)
class AliasEvent:
    """CDP ``alias``: two ids the product asserts are the same person (e.g. cross-device merge)."""

    previous_id: str
    previous_type: IdentityType
    new_id: str
    new_type: IdentityType
    observed_at: datetime
    source: str = "cdp"
    key_version: str = "v1"


class IdentityGraph:
    """Undirected (symmetric) transitive identity graph over hashed IDs.

    Edges keyed by ``(tenant_id, identity)`` so traversal is tenant-scoped (no cross-tenant leak).
    """

    def __init__(self) -> None:
        self._adj: dict[tuple[str, str], set[tuple[str, IdentityEdge]]] = defaultdict(set)

    # --- population --------------------------------------------------------------------------

    def add_edge(self, tenant_id: str, edge: IdentityEdge) -> None:
        # store both directions so the graph is undirected at traversal time
        self._adj[(tenant_id, edge.a)].add((edge.b, edge))
        self._adj[(tenant_id, edge.b)].add((edge.a, edge))

    def ingest_identify(self, tenant_id: str, event: IdentifyEvent) -> int:
        """Populate edges from an identify event. Returns the number of edges added."""
        added = 0
        if event.anonymous_id:
            self.add_edge(
                tenant_id,
                IdentityEdge(
                    a=event.anonymous_id,
                    a_type=IdentityType.ANONYMOUS_ID,
                    b=event.user_id,
                    b_type=IdentityType.USER_ID,
                    observed_at=event.observed_at,
                    source=event.source,
                    key_version=event.key_version,
                ),
            )
            added += 1
        if event.session_id:
            self.add_edge(
                tenant_id,
                IdentityEdge(
                    a=event.session_id,
                    a_type=IdentityType.SESSION_ID,
                    b=event.user_id,
                    b_type=IdentityType.USER_ID,
                    observed_at=event.observed_at,
                    source=event.source,
                    key_version=event.key_version,
                ),
            )
            added += 1
        return added

    def ingest_alias(self, tenant_id: str, event: AliasEvent) -> None:
        """Populate an edge from a CDP alias event (an explicit same-person assertion)."""
        self.add_edge(
            tenant_id,
            IdentityEdge(
                a=event.previous_id,
                a_type=event.previous_type,
                b=event.new_id,
                b_type=event.new_type,
                observed_at=event.observed_at,
                source=event.source,
                key_version=event.key_version,
            ),
        )

    def bridge_key_versions(
        self,
        tenant_id: str,
        identity_old: str,
        identity_new: str,
        observed_at: datetime,
        *,
        identity_type: IdentityType = IdentityType.USER_ID,
        old_key_version: str = "v1",
        new_key_version: str = "v2",
    ) -> None:
        """Link the *same* logical identity hashed under two HMAC key versions (CTO-74 rotation).

        Without this, rotating the user-id hashing key would silently fork every user into a
        pre- and post-rotation identity and break attribution across the boundary.
        """
        self.add_edge(
            tenant_id,
            IdentityEdge(
                a=identity_old,
                a_type=identity_type,
                b=identity_new,
                b_type=identity_type,
                observed_at=observed_at,
                source=KEY_ROTATION_SOURCE,
                key_version=f"{old_key_version}->{new_key_version}",
            ),
        )

    # --- resolution --------------------------------------------------------------------------

    def resolve_identity(
        self,
        tenant_id: str,
        start: str,
        *,
        max_depth: int = 2,
        as_of: datetime | None = None,
    ) -> set[str]:
        """Return identities reachable from ``start`` within ``max_depth`` hops (incl. ``start``).

        Edges with ``observed_at > as_of`` are ignored so a historical attribution never "leaks" an
        identity link learned after the fact. Bounded depth + the visited-set make cycles and
        runaway fan-out safe.

        Account edges (:attr:`IdentityType.ACCOUNT_ID`, CTO-184) are skipped entirely. An account is
        a container, not a person: two colleagues share one account hash, so walking through an
        account node would fuse them into a single identity and let one person's trace be
        attributed to the other person's conversion. That is a silent mis-attribution of exactly
        the kind this codebase refuses to make, so accounts are neither traversed nor returned
        here. Use :meth:`resolve_account` for the account question.
        """
        seen: set[str] = {start}
        frontier: list[tuple[str, int]] = [(start, 0)]
        while frontier:
            node, depth = frontier.pop()
            if depth >= max_depth:
                continue
            for neighbour, edge in self._adj.get((tenant_id, node), set()):
                if as_of is not None and edge.observed_at > as_of:
                    continue
                if neighbour in seen:
                    continue
                if self._neighbour_type(node, neighbour, edge) not in PERSON_IDENTITY_TYPES:
                    continue
                seen.add(neighbour)
                frontier.append((neighbour, depth + 1))
        return seen

    @staticmethod
    def _neighbour_type(node: str, neighbour: str, edge: IdentityEdge) -> IdentityType:
        """The type of ``neighbour`` on ``edge``, given we arrived from ``node``.

        Edges are stored once and read from both ends, so which of ``edge.a_type`` / ``edge.b_type``
        describes the neighbour depends on the direction of travel.
        """
        if edge.a == node and edge.b == neighbour:
            return edge.b_type
        if edge.b == node and edge.a == neighbour:
            return edge.a_type
        # Self-edge or a malformed edge: fall back to the side that matches the neighbour's value.
        return edge.b_type if edge.b == neighbour else edge.a_type

    def resolve_account(
        self,
        tenant_id: str,
        start: str,
        *,
        max_depth: int = 2,
        as_of: datetime | None = None,
    ) -> AccountResolution:
        """Resolve the account a person belongs to, or refuse to (CTO-184).

        This is the stitching path for tenants who cannot stamp an ``account_id`` on every span: a
        CRM or CDP connector asserts ``user_id <-> account_id`` edges and the account is inferred
        from the user instead of stated at emit time.

        **One user belongs to one account. Multi-account users are not supported.** If this person
        is observed against more than one account, the result carries ``account_hash=None`` and
        ``conflict=True``, and the caller must attribute NOTHING for them. Not a split, not a
        duplicate, not first-seen. Duplicating a user's cost across two accounts inflates the
        tenant total, so per-account spend would stop summing to what ``/cost`` reports and the
        whole per-customer surface would become untrustworthy. See the "Constraints decided"
        section of ``docs/cost-per-customer-plan.md``.

        Resolution is transitive across *person* identities (so an account asserted against a
        user's email or external id still resolves from their anonymous id) but only ever one hop
        from a person to an account. Accounts are never traversed through, so a colleague's account
        edge can never reach this person.
        """
        person_identities = self.resolve_identity(
            tenant_id, start, max_depth=max_depth, as_of=as_of
        )
        accounts: set[str] = set()
        for node in person_identities:
            for neighbour, edge in self._adj.get((tenant_id, node), set()):
                if as_of is not None and edge.observed_at > as_of:
                    continue
                if self._neighbour_type(node, neighbour, edge) is IdentityType.ACCOUNT_ID:
                    accounts.add(neighbour)
        ordered = tuple(sorted(accounts))
        # Exactly one account resolves. Zero means "not stitched yet", which is a different and
        # entirely normal state from "conflicting", and both are reported as no attribution.
        return AccountResolution(
            user_hash=start,
            account_hash=ordered[0] if len(ordered) == 1 else None,
            candidates=ordered,
        )

    def account_conflicts(
        self,
        tenant_id: str,
        *,
        max_depth: int = 2,
        as_of: datetime | None = None,
    ) -> list[AccountResolution]:
        """Every person in this tenant's graph who resolves to more than one account.

        The data-quality feed for the constraint above: the multi-account case has to be *visible*
        rather than silently swallowed, because the fix lives in the tenant's CRM and nobody can
        make it if nobody can see it. Sorted by user hash so the output is stable to diff.
        """
        people: set[str] = set()
        for (tid, node), neighbours in self._adj.items():
            if tid != tenant_id:
                continue
            for _neighbour, edge in neighbours:
                # The node's OWN type, not the neighbour's: a user whose only edge is to an
                # account is still a person and still has to be checked.
                own = edge.a_type if edge.a == node else edge.b_type
                if own in PERSON_IDENTITY_TYPES:
                    people.add(node)
                    break
        out = [
            res
            for person in sorted(people)
            if (
                res := self.resolve_account(
                    tenant_id, person, max_depth=max_depth, as_of=as_of
                )
            ).conflict
        ]
        return out

    # Back-compat alias: the stitcher (CTO-69) and its tests call ``resolve``.
    resolve = resolve_identity

    def edge_type_between(self, tenant_id: str, a: str, b: str) -> IdentityType | None:
        """If a single edge links ``a`` and ``b`` directly, return the *other side*'s type."""
        for neighbour, edge in self._adj.get((tenant_id, a), set()):
            if neighbour == b:
                return edge.b_type if edge.a == a else edge.a_type
        return None
