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
                seen.add(neighbour)
                frontier.append((neighbour, depth + 1))
        return seen

    # Back-compat alias: the stitcher (CTO-69) and its tests call ``resolve``.
    resolve = resolve_identity

    def edge_type_between(self, tenant_id: str, a: str, b: str) -> IdentityType | None:
        """If a single edge links ``a`` and ``b`` directly, return the *other side*'s type."""
        for neighbour, edge in self._adj.get((tenant_id, a), set()):
            if neighbour == b:
                return edge.b_type if edge.a == a else edge.a_type
        return None
