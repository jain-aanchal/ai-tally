-- Optional human-readable names for accounts (CTO-186, B7).
--
-- WHY this table exists at all, and why it is HERE rather than on the span.
--
-- The cost-per-customer tab groups spend by `AccountIdHash` (CTO-180), a one-way HMAC of the
-- tenant's own account identifier. That makes the tab correct and private, and completely
-- unreadable: every row is 64 characters of hex. A label is how a tenant makes it readable again.
--
-- The label deliberately does NOT live on the span, for three reasons that all point the same way:
--   1. A label is mutable metadata. Stamping it on every span means a rename leaves two spellings
--      in the telemetry and no principled answer to which of them wins.
--   2. It would be written once per span for a value that changes approximately never, which is
--      pure storage waste on the hottest table in the system.
--   3. It would put the tenant's customer names into the telemetry store, which is the exact thing
--      `AccountIdHash` was introduced to avoid. ClickHouse must never hold a customer name.
--
-- So the name lives here, in the control plane, keyed on the hash, and is joined at render time.
-- Telemetry stays name-free no matter how many labels a tenant sets.
--
-- INVARIANT this table upholds: a label is optional per account, and its absence is a valid,
-- fully supported state rather than missing data. A tenant that wants no customer names in our
-- system sets none, and the tab still works by falling back to a shortened hash. Consequently
-- there is no NOT NULL label column anywhere else, no backfill, and no placeholder row: an account
-- with no label simply has no row here.
--
-- WHY deletion is a real DELETE and not a tombstone. Reverting an account to its hash is the
-- escape hatch for a tenant who changes their mind about having customer names in our system.
-- A soft delete would keep the name in our storage after they asked us to forget it, which would
-- make the escape hatch a lie. For the same reason this table has no companion `_changes` audit
-- log, unlike `tenant_feature_value_events` (0014): an audit row carrying a `before` snapshot
-- would preserve the deleted label indefinitely and defeat the whole point. `updated_at` is the
-- only history we keep, and it holds no name.
--
-- Reads and writes both go through GET/POST/DELETE /v1/tenant/account-labels. The web app never
-- touches Postgres directly, same as the rest of the control plane.
--
-- On `account_id_hash` being TEXT and not a foreign key: it is a digest computed elsewhere (by the
-- SDK on the emitting path, or by /v1/tenant/account-lookup on demand), and there is no table of
-- valid account hashes to reference. A label for a hash that has never appeared on a span is
-- harmless and is allowed: a tenant may label an account before it has spent anything.

CREATE TABLE IF NOT EXISTS tenant_account_labels (
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    account_id_hash  TEXT NOT NULL,
    label            TEXT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, account_id_hash)
);

-- The tab's read pattern is "every label this tenant has", fetched once and joined in memory
-- against a page of account rows. The primary key already leads with tenant_id and serves that
-- prefix scan, so no additional index is needed here.
