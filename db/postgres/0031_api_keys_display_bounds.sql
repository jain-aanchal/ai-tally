-- 0031_api_keys_display_bounds.sql
-- Length-bounded CHECKs on the api_keys display columns (Initiative 1 §11 hardening).
--
-- 0029_orgs_and_access.sql added name / token_prefix / created_by as unbounded TEXT. The gateway
-- store (gateway/tenant_api_keys.py: normalize_name, normalize_created_by) already caps name at 120
-- and created_by at 255 before insert, but that is application-side only: anything that writes
-- api_keys outside that path (a fixture, a repair script, a future endpoint) can still park an
-- unbounded blob in a column the dashboard renders. The CLAUDE.md invariant is that credential-
-- adjacent metadata is bounded by a CHECK, the same way tenants.hash_salt_kek_ref already is, so the
-- database enforces it rather than trusting every caller.
--
-- These bound DISPLAY metadata only. The secret is still stored solely as key_hash (SHA-256) and is
-- untouched here. token_prefix is a non-secret leading slice (TOKEN_PREFIX + 6 suffix chars = 20
-- characters today); 64 leaves room to lengthen the display slice without another migration, while
-- still refusing a blob. NULL passes every CHECK: all three columns are legitimately absent on the
-- pre-existing local-dev keys, and an unknown value stays NULL rather than being back-filled with a
-- fabricated one.
--
-- Bounds match the gateway constants exactly. If MAX_KEY_NAME_CHARS / MAX_CREATED_BY_CHARS move,
-- move them here too, or the store will accept a value the database then rejects.
--
-- NOT VALID + VALIDATE is deliberately NOT used: these are additive constraints on columns that only
-- 0029 could have populated, and the table is small. A plain ADD CONSTRAINT takes a brief ACCESS
-- EXCLUSIVE lock, which is acceptable on api_keys.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_keys_name_len'
    ) THEN
        ALTER TABLE api_keys
            ADD CONSTRAINT api_keys_name_len
            CHECK (name IS NULL OR length(name) <= 120);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_keys_token_prefix_len'
    ) THEN
        ALTER TABLE api_keys
            ADD CONSTRAINT api_keys_token_prefix_len
            CHECK (token_prefix IS NULL OR length(token_prefix) <= 64);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'api_keys_created_by_len'
    ) THEN
        ALTER TABLE api_keys
            ADD CONSTRAINT api_keys_created_by_len
            CHECK (created_by IS NULL OR length(created_by) <= 255);
    END IF;
END
$$;
