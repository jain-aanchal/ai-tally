# SPDX-License-Identifier: Apache-2.0
"""Plaintext account id -> ``AccountIdHash``, for the cost-per-customer search box (CTO-185, B6).

WHY this exists. The cost-per-customer tab lists accounts by ``AccountIdHash`` (CTO-180). A label
is optional per account by design (see ``docs/cost-per-customer-plan.md``, Decision 1), so a tenant
who sets none gets a table of 64-character hex strings. Those rows are not merely ugly, they are
unfindable: an operator who knows their customer as ``acme-corp`` has no way to reach the row,
because the hash is one-way and nothing anywhere maps back. This module is the forward direction,
computed on demand, which is the only direction that exists.

WHY the plaintext is never kept. Storing an ``account_id -> hash`` table would rebuild the reverse
map that hashing exists to prevent, and would put the tenant's customer identifiers in our storage.
The plaintext lives for the length of one HMAC call and is then gone: not persisted, not logged,
not echoed in an error. :func:`hash_account_id` therefore takes a value and returns digests, and
every failure it can produce is described without quoting the input.

WHY it reuses ``HmacKeyRegistry``. Parity with the SDK is the whole point: a hash that does not
equal the one the emitting path stamped on the span matches nothing. So this calls the SDK's own
``tally.hmac_keys`` derivation (provision, then hash under the tenant's active key version), the
same way ``gateway.stripe_ingest.hash_customer_email`` and ``gateway.integration_workers``
``build_hasher`` already do. There is no second implementation to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from tally.hmac_keys import HmacKeyRegistry

from gateway.tenant_identity import TenantKey

# Guards against a caller pasting a document into the search box. Real account ids are short;
# anything past this is a mistake and is rejected without echoing what was sent.
MAX_ACCOUNT_ID_BYTES = 512


class AccountLookupError(ValueError):
    """Invalid lookup input. Messages here are written to be safe to log and safe to return."""


@dataclass(frozen=True, slots=True)
class AccountHash:
    """One ``AccountIdHash`` plus the tenant spelling and key version that produced it."""

    account_id_hash: str
    key_version: str
    tenant_key_form: str

    def as_dict(self) -> dict[str, str]:
        return {
            "account_id_hash": self.account_id_hash,
            "key_version": self.key_version,
            "tenant_key_form": self.tenant_key_form,
        }


def normalize_account_id(value: object) -> str:
    """Validate and trim a plaintext account id.

    Trimming only. No lowercasing, unlike the Stripe email hasher: an account id is an opaque
    tenant-side identifier and ``Acme`` may well be a different customer from ``acme``, so folding
    case here would merge two accounts into one row.

    Raises :class:`AccountLookupError` with a message that never contains ``value``.
    """
    if not isinstance(value, str):
        raise AccountLookupError("account_id must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise AccountLookupError("account_id must be a non-empty string")
    if len(trimmed.encode("utf-8")) > MAX_ACCOUNT_ID_BYTES:
        raise AccountLookupError(f"account_id must be at most {MAX_ACCOUNT_ID_BYTES} bytes")
    return trimmed


def hash_account_id(
    registry: HmacKeyRegistry,
    tenant_keys: Sequence[TenantKey],
    account_id: str,
) -> tuple[AccountHash, ...]:
    """Hash ``account_id`` under each spelling of the tenant, in the order given.

    One digest per spelling because the tenant identifier is key material: see
    :mod:`gateway.tenant_identity`. The caller matches spans against the whole set, so a dashboard
    that addresses the gateway by name still finds spans emitted under the UUID and vice versa.

    Returns an empty tuple only when ``tenant_keys`` is empty, which the endpoint prevents.
    """
    out: list[AccountHash] = []
    seen: set[str] = set()
    for key in tenant_keys:
        if not key.value or key.value in seen:
            continue
        seen.add(key.value)
        registry.provision(key.value)
        stamped = registry.hash(key.value, account_id)
        out.append(
            AccountHash(
                account_id_hash=stamped.value,
                key_version=stamped.key_version,
                tenant_key_form=key.form,
            )
        )
    return tuple(out)
