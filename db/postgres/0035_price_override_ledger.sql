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

-- ONE TRANSACTION, and this is not boilerplate (CTO-416 review). psql autocommits statement by
-- statement, so the first version of this file aborted halfway on any database that already held
-- rows: the columns were added and NOT NULL was dropped, and then the unique index failed, leaving
-- a database with no unique index and NO APPEND-ONLY TRIGGER while looking migrated. Both the
-- audit guarantee and the concurrent-append race guard live in those two objects. All or nothing.
BEGIN;

ALTER TABLE price_catalog_overrides
    ADD COLUMN IF NOT EXISTS version     INTEGER     NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS supersedes  INTEGER,
    ADD COLUMN IF NOT EXISTS actor       TEXT        NOT NULL DEFAULT 'pre-ledger',
    ADD COLUMN IF NOT EXISTS reason      TEXT        NOT NULL DEFAULT 'pre-ledger row, provenance unknown',
    ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- A tombstone is a row with no price. That is what makes a withdrawal auditable instead of a gap.
ALTER TABLE price_catalog_overrides ALTER COLUMN price_per_unit DROP NOT NULL;

-- BACKFILL the version per slot, rather than leaving every existing row at the DEFAULT of 1.
--
-- The 0001 table has no slot uniqueness and its whole point was several TIME-WINDOWED rows per
-- slot, so a constant 1 collides the moment any tenant has two windows for one rate, and the unique
-- index below then refuses to build. Numbering by valid_from is also the honest reading of what
-- those rows are: the earliest window is version 1 and each later one supersedes it, which is
-- exactly the chain the ledger would have recorded had it existed when they were written. `id`
-- breaks a tie between two rows with the same valid_from so the numbering is deterministic.
-- The append-only trigger is dropped for the duration, because the backfill below is itself an
-- UPDATE and a RE-RUN of this file would otherwise be refused by the trigger its own first run
-- created. Every other migration here is safe to replay, and this one has to be too: the whole file
-- is one transaction, so a failure anywhere rolls the drop back with everything else.
DROP TRIGGER IF EXISTS trg_price_overrides_append_only ON price_catalog_overrides;

-- SCOPED to slots that are entirely pre-ledger. Once the control plane has appended to a slot, its
-- versions are assigned by the INSERT and are referenced by `supersedes` chains, so renumbering
-- them on a replay would rewrite history and could collide with the unique index below (a backdated
-- append sorts before an older row and would want its number). A slot nobody has appended to is
-- unambiguous and is exactly what the backfill is for.
WITH pre_ledger_slots AS (
    SELECT tenant_id, provider, model, price_type
      FROM price_catalog_overrides
     GROUP BY tenant_id, provider, model, price_type
    HAVING bool_and(actor = 'pre-ledger')
), numbered AS (
    SELECT p.id,
           row_number() OVER (
               PARTITION BY p.tenant_id, p.provider, p.model, p.price_type
               ORDER BY p.valid_from, p.id
           ) AS slot_version
      FROM price_catalog_overrides p
      JOIN pre_ledger_slots s
        ON (s.tenant_id, s.provider, s.model, s.price_type)
         = (p.tenant_id, p.provider, p.model, p.price_type)
)
-- Deliberately UNCONDITIONAL within that scope: a replay rewrites the same numbers rather than
-- skipping the statement. Skipping made the DROP TRIGGER above dead weight that a later edit could
-- remove without any test noticing, and re-deriving a value that is a pure function of the rows is
-- not a write worth avoiding.
UPDATE price_catalog_overrides AS p
   SET version = numbered.slot_version,
       supersedes = NULLIF(numbered.slot_version - 1, 0)
  FROM numbered
 WHERE p.id = numbered.id;

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

-- The unique index above also serves the load path's read (a tenant's history in ledger order), so
-- there is deliberately no second index on the same columns.

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
-- Created LAST, after the backfill above, which is itself an UPDATE and would otherwise be refused
-- by the very rule it is preparing the table for.
CREATE TRIGGER trg_price_overrides_append_only
    BEFORE UPDATE ON price_catalog_overrides
    FOR EACH ROW EXECUTE FUNCTION price_overrides_refuse_update();

COMMIT;
