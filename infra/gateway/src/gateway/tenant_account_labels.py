# SPDX-License-Identifier: Apache-2.0
"""Optional human-readable names for accounts (CTO-186, B7).

WHY this exists. The cost-per-customer tab groups spend by ``AccountIdHash`` (CTO-180), so an
account with no label renders as 64 characters of hex. This table is how a tenant makes the tab
readable. See ``db/postgres/0023_tenant_account_labels.sql`` for the full rationale on why the
label lives in Postgres and never on the span: it is mutable metadata, and putting customer names
in the telemetry store is exactly what the hash exists to prevent.

WHY there is no audit log here, unlike :mod:`gateway.tenant_feature_value_events`. Deleting a label
reverts the account to its hash, and that is the escape hatch for a tenant who decides they want no
customer names in our system. An audit row holding a ``before`` snapshot would keep the deleted
name on disk forever and turn the escape hatch into a fiction. Deletion is therefore a real
``DELETE`` and the only history retained is ``updated_at``, which holds no name.

WHY the tenant identifier gets resolved twice over. Two separate spellings problems collide here
and they have different answers:

* ``tenant_id`` is a UUID foreign key to ``tenants(id)``, but the dashboard identifies a tenant by
  NAME (``local-dev``). :func:`_resolve_tenant_uuid` folds the name onto the UUID exactly the way
  :mod:`gateway.connectors.config_admin` already does for the other config tables. One tenant, one
  row, whichever door the request came through.

* ``account_id_hash`` cannot be folded, because for HMAC the tenant identifier is *key material*.
  As :mod:`gateway.tenant_identity` explains, ``local-dev`` and its UUID derive two unrelated key
  spaces and therefore two different digests for the same account id. This module refuses to guess
  which one a span was stamped under. It stores the digest it is handed, verbatim, and the endpoint
  above it writes one row per spelling that :func:`gateway.account_lookup.hash_account_id` returns.
  Labelling by plaintext account id therefore attaches the name under every key space the tenant
  could have emitted under, so the join succeeds regardless of which auth mode produced the span.

Reads and writes both go through ``GET/POST/DELETE /v1/tenant/account-labels``. The web app never
touches Postgres directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from gateway.config import Settings
from gateway.tenant_lookup import TenantNotFoundError, resolve_tenant_uuid

# A label is a display string for one table cell, not a description field. The cap is generous
# enough for any real company name and tight enough that the tab cannot be broken by pasting a
# document into the box.
MAX_LABEL_CHARS = 200

# ``AccountIdHash`` is a hex-encoded HMAC digest. Validating the shape here keeps obvious junk
# (a plaintext account id pasted into the wrong field, most likely) out of the table, where it
# would sit forever matching nothing.
MIN_ACCOUNT_ID_HASH_CHARS = 32
MAX_ACCOUNT_ID_HASH_CHARS = 128


class AccountLabelError(ValueError):
    """Invalid label input. Messages never quote the submitted value.

    Same discipline as :class:`gateway.account_lookup.AccountLookupError`: a rejection message can
    end up in a log line, and the values passing through here are customer names.
    """


class TenantNotFound(AccountLabelError):
    """The caller's tenant identifier matches no ``tenants`` row."""


@dataclass(frozen=True, slots=True)
class AccountLabel:
    """One ``(tenant, account_id_hash) -> label`` mapping."""

    account_id_hash: str
    label: str
    updated_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "account_id_hash": self.account_id_hash,
            "label": self.label,
            "updated_at": self.updated_at,
        }


def normalize_label(value: object) -> str:
    """Validate and trim a label.

    Trimming only, no case folding and no other rewriting: this is the tenant's own display string
    and it should come back out of the API exactly as it went in.

    Raises :class:`AccountLabelError` with a message that never contains ``value``.
    """
    if not isinstance(value, str):
        raise AccountLabelError("label must be a string")
    trimmed = value.strip()
    if not trimmed:
        # An empty label is not the same as no label. Clearing a name is a delete, which actually
        # removes the row, so accepting "" here would create a second, weaker kind of deletion that
        # leaves a row behind.
        raise AccountLabelError("label must be non-empty; delete the label to clear it")
    if len(trimmed) > MAX_LABEL_CHARS:
        raise AccountLabelError(f"label must be at most {MAX_LABEL_CHARS} characters")
    return trimmed


def normalize_account_id_hash(value: object) -> str:
    """Validate an ``AccountIdHash`` as produced by the SDK or by ``/v1/tenant/account-lookup``.

    Deliberately shape-only. We do not check the digest against anything, because there is nothing
    to check it against: hashes are one-way and there is no registry of the valid ones. Labelling
    an account that has not spent anything yet is legitimate.
    """
    if not isinstance(value, str):
        raise AccountLabelError("account_id_hash must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise AccountLabelError("account_id_hash must be a non-empty string")
    if not (MIN_ACCOUNT_ID_HASH_CHARS <= len(trimmed) <= MAX_ACCOUNT_ID_HASH_CHARS):
        raise AccountLabelError(
            "account_id_hash must be a hex digest of "
            f"{MIN_ACCOUNT_ID_HASH_CHARS} to {MAX_ACCOUNT_ID_HASH_CHARS} characters"
        )
    if any(c not in "0123456789abcdefABCDEF" for c in trimmed):
        raise AccountLabelError("account_id_hash must be hexadecimal")
    return trimmed.lower()


def _resolve_tenant_uuid(cur: psycopg.Cursor, tenant_id: str) -> str:
    """Map the caller's tenant identifier onto ``tenants.id``.

    The rule now lives in :mod:`gateway.tenant_lookup` so every control-plane store shares one copy
    (CTO-201). This wrapper only re-types the failure as this module's :class:`TenantNotFound`
    subclass, which the endpoint above already catches, and keeps the non-quoting message this
    module deliberately uses.

    Note this is the *row identity* question only. It is NOT the same question as which spelling
    hashed the account id, which is key material and cannot be folded this way. See the module
    docstring.
    """
    try:
        return resolve_tenant_uuid(cur, tenant_id)
    except TenantNotFoundError as exc:
        raise TenantNotFound("no tenant matches the supplied identifier") from exc


def _row_to_label(row: tuple) -> AccountLabel:
    return AccountLabel(
        account_id_hash=str(row[0]),
        label=str(row[1]),
        updated_at=row[2].isoformat() if row[2] is not None else "",
    )


class TenantAccountLabelStore:
    """Postgres-backed CRUD over ``tenant_account_labels``.

    Every method takes the ``tenant_id`` resolved by upstream auth, so the SQL never crosses
    tenants. Writes are last-write-wins on ``(tenant_id, account_id_hash)``: a label is a display
    string with a single current value, so there is no idempotency token to carry. Replaying the
    same upsert twice is naturally a no-op beyond bumping ``updated_at``.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def list(self, tenant_id: str) -> list[AccountLabel]:
        """Every label this tenant has set.

        The tab fetches this once and joins it in memory against a page of account rows, so an
        account with no row here falls back to its shortened hash without a second round trip.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                SELECT account_id_hash, label, updated_at
                FROM tenant_account_labels
                WHERE tenant_id = %s
                ORDER BY label, account_id_hash
                """,
                (resolved,),
            )
            return [_row_to_label(row) for row in cur.fetchall()]

    def get(self, tenant_id: str, account_id_hash: str) -> AccountLabel | None:
        """One label, or None when the account is unlabelled.

        None is a normal answer, not an error: an unlabelled account is a fully supported state.
        """
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            return self._fetch(cur, resolved, account_id_hash)

    def upsert(self, tenant_id: str, account_id_hash: str, *, label: str) -> AccountLabel:
        """Set or replace the label for one account hash.

        ``ON CONFLICT ... DO UPDATE`` is what makes setting and renaming the same operation. The
        caller does not have to know whether a label already exists, and two concurrent writers
        cannot produce a duplicate-key error or two rows for one account.

        ``updated_at`` is refreshed explicitly because the column default only applies on INSERT,
        so a rename would otherwise keep reporting the timestamp of the original naming.
        """
        label = normalize_label(label)
        account_id_hash = normalize_account_id_hash(account_id_hash)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                """
                INSERT INTO tenant_account_labels (tenant_id, account_id_hash, label)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, account_id_hash) DO UPDATE
                  SET label = EXCLUDED.label,
                      updated_at = now()
                RETURNING account_id_hash, label, updated_at
                """,
                (resolved, account_id_hash, label),
            )
            row = cur.fetchone()
            assert row is not None  # RETURNING on an upsert that cannot no-op
            conn.commit()
            return _row_to_label(row)

    def upsert_many(
        self, tenant_id: str, account_id_hashes: list[str], *, label: str
    ) -> list[AccountLabel]:
        """Apply one label to several hashes of the SAME account, in one transaction.

        This is not a bulk-labelling convenience. It exists for the key-material problem: the same
        account id hashes to a different digest under each spelling of the tenant identifier (see
        :mod:`gateway.tenant_identity`), so labelling by plaintext account id has to write the name
        under every spelling or the join silently misses spans emitted through the other door.

        One transaction so a tenant can never end up labelled under one key space and not the
        other, which would look like a partially applied rename.
        """
        label = normalize_label(label)
        hashes = [normalize_account_id_hash(h) for h in account_id_hashes]
        if not hashes:
            raise AccountLabelError("at least one account_id_hash required")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            out: list[AccountLabel] = []
            for account_id_hash in hashes:
                cur.execute(
                    """
                    INSERT INTO tenant_account_labels (tenant_id, account_id_hash, label)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, account_id_hash) DO UPDATE
                      SET label = EXCLUDED.label,
                          updated_at = now()
                    RETURNING account_id_hash, label, updated_at
                    """,
                    (resolved, account_id_hash, label),
                )
                row = cur.fetchone()
                assert row is not None
                out.append(_row_to_label(row))
            conn.commit()
            return out

    def delete(self, tenant_id: str, account_id_hash: str) -> bool:
        """Remove the label, reverting the account to its hash in the tab.

        A real DELETE. See the module docstring: a tombstone would keep the customer name on disk
        after the tenant asked us to forget it.

        Returns True if this call removed a row, False if the account was already unlabelled.
        Deleting an absent label is not an error, so a double-click cannot produce a 404.
        """
        account_id_hash = normalize_account_id_hash(account_id_hash)
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "DELETE FROM tenant_account_labels WHERE tenant_id = %s AND account_id_hash = %s",
                (resolved, account_id_hash),
            )
            removed = cur.rowcount > 0
            conn.commit()
            return removed

    def delete_many(self, tenant_id: str, account_id_hashes: list[str]) -> int:
        """Remove the label from several hashes of the same account. Returns rows removed.

        The mirror of :meth:`upsert_many`, and the reason it has to exist: if labelling by
        plaintext writes a row per tenant spelling, unlabelling by plaintext must clear all of them
        or the escape hatch leaves the name behind under the other key space.
        """
        hashes = [normalize_account_id_hash(h) for h in account_id_hashes]
        if not hashes:
            raise AccountLabelError("at least one account_id_hash required")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            resolved = _resolve_tenant_uuid(cur, tenant_id)
            cur.execute(
                "DELETE FROM tenant_account_labels "
                "WHERE tenant_id = %s AND account_id_hash = ANY(%s)",
                (resolved, hashes),
            )
            removed = cur.rowcount
            conn.commit()
            return removed

    def record_observed_label(
        self, tenant_id: str, account_id_hash: str, label: object
    ) -> AccountLabel | None:
        """Opportunistic upsert from the wire-only ``gen_ai.account_label`` SDK attribute.

        THIS IS THE SEAM FOR CTO-182 (B3). B2 (CTO-181) added the attribute to the SDK as wire-only,
        and B3 adds a hook in :mod:`gateway.mapping` that accepts the label without persisting it to
        ClickHouse. When that hook lands it should call this method and nothing else. As of this
        commit the B3 seam is not present on this branch, so nothing calls this yet; it is written
        and tested so that wiring it up is a one-line change with no design decisions left in it.

        Fail-soft on purpose, and this is the important part. An unusable label on a span must never
        turn into a failed ingest: telemetry is the product and a customer's display name is not
        worth dropping a span over. A malformed or oversized value is discarded and None is
        returned. The caller ignores the result.

        Note it does not clear an existing label when the attribute is absent. Absence on one span
        means that span did not carry the attribute, which is not the same statement as "this
        account should no longer have a name". Only an explicit DELETE removes a label.
        """
        if label is None:
            return None
        try:
            return self.upsert(tenant_id, account_id_hash, label=label)
        except AccountLabelError:
            return None

    @staticmethod
    def _fetch(
        cur: psycopg.Cursor, tenant_uuid: str, account_id_hash: str
    ) -> AccountLabel | None:
        cur.execute(
            """
            SELECT account_id_hash, label, updated_at
            FROM tenant_account_labels
            WHERE tenant_id = %s AND account_id_hash = %s
            """,
            (tenant_uuid, account_id_hash),
        )
        row = cur.fetchone()
        return _row_to_label(row) if row else None
