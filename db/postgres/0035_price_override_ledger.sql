-- SPDX-License-Identifier: Apache-2.0
-- CTO-416: turn price_catalog_overrides into the append-only, audited ledger the code already
-- assumes it is.
--
-- WHY. `price_catalog_overrides` has existed since 0001 and nothing has ever read or written it.
-- The governance model lives in tally.overrides.OverrideLedger (CTO-54): every change to a slot
-- (tenant, provider, model, price_type) is a NEW versioned row that records who changed it, why,
-- and when, and a withdrawal is a tombstone rather than a DELETE. The 0001 table cannot express any
-- of that: it has no version, no actor, no reason, no recorded_at, and a NOT NULL price, so the
-- only way to re-price a slot would be an UPDATE that silently rewrites what a past invoice was
-- computed from. This migration adds the missing columns so the stored shape matches the ledger.
--
-- WHY THE DEFAULTS ON A NOT NULL AUDIT COLUMN. Any row already sitting here predates the ledger and
-- has no recorded actor or reason. Backfilling a literal 'pre-ledger' marker is honest about that
-- (it says the provenance is unknown) where a made-up actor would not be. New rows always carry a
-- real actor and reason: the gateway control plane requires both before it will append.
--
-- NO SECRETS LIVE HERE. A price is a contract term, not a credential. This table holds no key
-- material and no reference to any, and nothing in the write path accepts one.

ALTER TABLE price_catalog_overrides
    ADD COLUMN IF NOT EXISTS version     INTEGER     NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS supersedes  INTEGER,
    ADD COLUMN IF NOT EXISTS actor       TEXT        NOT NULL DEFAULT 'pre-ledger',
    ADD COLUMN IF NOT EXISTS reason      TEXT        NOT NULL DEFAULT 'pre-ledger row, provenance unknown',
    ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- A tombstone is a row with no price. That is what makes a withdrawal auditable instead of a gap.
ALTER TABLE price_catalog_overrides ALTER COLUMN price_per_unit DROP NOT NULL;

-- Money sanity. A negative rate would compute a negative cost and quietly credit a tenant for
-- spending; a version below 1 would break the supersedes chain.
ALTER TABLE price_catalog_overrides
    DROP CONSTRAINT IF EXISTS price_overrides_price_non_negative;
ALTER TABLE price_catalog_overrides
    ADD CONSTRAINT price_overrides_price_non_negative
    CHECK (price_per_unit IS NULL OR price_per_unit >= 0);

ALTER TABLE price_catalog_overrides
    DROP CONSTRAINT IF EXISTS price_overrides_version_positive;
ALTER TABLE price_catalog_overrides
    ADD CONSTRAINT price_overrides_version_positive
    CHECK (version >= 1 AND (supersedes IS NULL OR supersedes < version));

-- Length bounds, same posture as every other control-plane text column: an audit field is a short
-- human sentence, not a place to park a document.
ALTER TABLE price_catalog_overrides
    DROP CONSTRAINT IF EXISTS price_overrides_audit_bounds;
ALTER TABLE price_catalog_overrides
    ADD CONSTRAINT price_overrides_audit_bounds
    CHECK (
        length(actor) BETWEEN 1 AND 200
        AND length(reason) BETWEEN 1 AND 500
        AND length(provider) BETWEEN 1 AND 100
        AND length(model) BETWEEN 1 AND 200
    );

-- One version per slot. This is what makes the append safe on more than one replica: two gateways
-- computing the same next version race, and exactly one of them commits.
CREATE UNIQUE INDEX IF NOT EXISTS uq_price_overrides_slot_version
    ON price_catalog_overrides (tenant_id, provider, model, price_type, version);

-- The load path reads a tenant's whole history in ledger order.
CREATE INDEX IF NOT EXISTS idx_price_overrides_tenant_order
    ON price_catalog_overrides (tenant_id, provider, model, price_type, version);

-- APPEND-ONLY, enforced by the database rather than by convention. An UPDATE here would rewrite the
-- rate a past cost was computed from, which is the one thing the version column exists to prevent.
-- DELETE is deliberately NOT blocked: the only delete is the ON DELETE CASCADE from `tenants`, and
-- blocking that would make removing a tenant fail. Ordinary deletes have no code path.
CREATE OR REPLACE FUNCTION price_overrides_refuse_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'price_catalog_overrides is append-only (CTO-416): append a new version or a tombstone';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_price_overrides_append_only ON price_catalog_overrides;
CREATE TRIGGER trg_price_overrides_append_only
    BEFORE UPDATE ON price_catalog_overrides
    FOR EACH ROW EXECUTE FUNCTION price_overrides_refuse_update();
