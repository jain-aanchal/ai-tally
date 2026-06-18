-- Per-tenant monthly CAC inputs for the unit-economics view (CTO-111).
--
-- WHY POSTGRES, NOT CLICKHOUSE: CAC is not span-shaped. Finance edits one row per month, locks it
-- when the period closes, and ~12 rows per tenant per year is the steady-state working set. This
-- is a tenant-scoped monthly aggregate — exactly the shape the control-plane Postgres already
-- holds (tenants, connectors, replay config). ClickHouse would give us nothing here except a more
-- annoying upsert story.
--
-- LOCKING: a period is editable until the NEXT period starts. The frontend disables the form for
-- months whose successor exists; the backend checks ``closed_at IS NULL`` on write. We never delete.
--
-- Money: stored as ``micro_usd`` (1/1,000,000 USD) to match the rest of the wire/store contract
-- (see ``BusinessEvent.value_amount_micro``). Currency is fixed at 'USD' for v1 — multi-currency is
-- a later ticket once a tenant actually asks. Adding the column now means we don't have to migrate
-- when that lands.
--
-- Sanity guard: ``new_customers_total >= new_customers_paid`` — a paying customer is by definition
-- a customer, so paid count can never exceed total. CHECK fails loudly at insert time rather than
-- producing silently-broken ratios in the dashboard.

CREATE TABLE IF NOT EXISTS cac_periods (
    tenant_id                 UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period_start              DATE        NOT NULL,
    period_end                DATE        NOT NULL,
    currency                  TEXT        NOT NULL DEFAULT 'USD'
                                          CHECK (currency = 'USD'),
    paid_spend_micro_usd      BIGINT      NOT NULL DEFAULT 0
                                          CHECK (paid_spend_micro_usd >= 0),
    sales_spend_micro_usd     BIGINT      NOT NULL DEFAULT 0
                                          CHECK (sales_spend_micro_usd >= 0),
    content_spend_micro_usd   BIGINT      NOT NULL DEFAULT 0
                                          CHECK (content_spend_micro_usd >= 0),
    overhead_micro_usd        BIGINT      NOT NULL DEFAULT 0
                                          CHECK (overhead_micro_usd >= 0),
    new_customers_paid        INTEGER     NOT NULL DEFAULT 0
                                          CHECK (new_customers_paid >= 0),
    new_customers_total       INTEGER     NOT NULL DEFAULT 0
                                          CHECK (new_customers_total >= 0),
    notes                     TEXT,
    closed_at                 TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start),
    CHECK (period_end >= period_start),
    CHECK (new_customers_total >= new_customers_paid)
);

-- Read pattern is "last 12 months for a tenant, ordered desc" — the PK index already covers it
-- because (tenant_id, period_start) is a sorted prefix scan. No extra index needed for v1.
