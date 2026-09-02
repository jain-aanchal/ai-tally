-- 0029_orgs_and_access.sql
-- Organizations, users & access (Initiative 1). Ticket: TODO (umbrella).
--
-- Clerk is the system of record for identity (users, orgs, memberships, roles,
-- invitations). ai-tally stores only the org-to-tenant mapping plus display-only
-- metadata on the ingest keys it already owns. No users/memberships tables here:
-- those live in Clerk by design.
--
-- All columns are ADDED nullable so the pre-existing `local-dev` tenant and its
-- keys survive unchanged. clerk_org_id is nullable for the same reason: the demo
-- tenant has no Clerk org, and a real org fills it in on provision.

-- Map a Clerk organization to an ai-tally tenant. Nullable: the local-dev demo
-- tenant has none. Partial UNIQUE index so at most one tenant maps to a given
-- Clerk org, while any number of tenants may have NULL (only local-dev should).
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS clerk_org_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_clerk_org_id
    ON tenants (clerk_org_id)
    WHERE clerk_org_id IS NOT NULL;

-- Display-only metadata on ingest keys. The secret is STILL only ever stored as
-- key_hash (SHA-256). token_prefix is a non-secret leading slice for the UI so a
-- human can tell two keys apart in a list; it is not sufficient to authenticate.
-- created_by is the Clerk user id that minted the key (audit only). last_used_at
-- is a best-effort UI convenience, updated off the hot path (see §5).
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS name         TEXT,
    ADD COLUMN IF NOT EXISTS token_prefix TEXT,
    ADD COLUMN IF NOT EXISTS created_by   TEXT,          -- Clerk user id (user_...)
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
