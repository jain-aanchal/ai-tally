-- Manifest of uploaded revenue snapshots (CTO-198, plan item E5).
--
-- Revenue connectors cover Stripe and HubSpot. Plenty of B2B companies bill through Chargebee,
-- Recurly, Zuora, NetSuite or plain invoices, and the revenue truth sits in a spreadsheet finance
-- already maintains. The upload endpoint maps `account_id, period, amount, currency` onto the same
-- `business_events` rows every other revenue source produces, so nothing downstream special-cases
-- it. The money itself therefore lives in ClickHouse, not here.
--
-- What lives here is the thing ClickHouse cannot answer honestly: WHEN the snapshot was taken.
--
-- Why a manifest at all
-- --------------------
-- An uploaded figure is a point-in-time snapshot, not a live feed. Revenue changes monthly and an
-- upload nobody refreshes goes stale silently, quietly corrupting every margin number that reads
-- it. `business_events.IngestedAt` is close to an answer but not one: it is per row, it is rewritten
-- by any partial re-ingest, and it cannot say that a period the tenant used to upload has stopped
-- being uploaded. One row per snapshot, carrying uploaded_at, is what lets the dashboard render an
-- "as of" date and a staleness badge instead of presenting a six-month-old number as current.
--
-- Why the primary key is (tenant_id, period)
-- ------------------------------------------
-- Re-uploading the same period must REPLACE, not append. If it appends, revenue doubles on the
-- second upload and nobody notices until margin looks impossibly good. Making that a convention
-- ("remember to delete the old rows first") guarantees someone eventually forgets, so it is a
-- primary key instead: a period can only ever have one snapshot row, and the upsert that writes it
-- is the same statement that deletes the period's previous ClickHouse rows. There is no code path
-- that can accumulate two snapshots for one period.
--
-- Reads/writes go through GET/POST/DELETE /v1/tenant/revenue-uploads. The web app never touches
-- Postgres directly (same rule as tenant_revenue_source_config / tenant_unit_economics_config).

CREATE TABLE IF NOT EXISTS tenant_revenue_uploads (
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Calendar month the snapshot covers, 'YYYY-MM'. A month is the granularity finance actually
    -- closes on, and it is what makes the "has this period been refreshed?" question answerable.
    period              TEXT NOT NULL,
    -- business_events.Source stamped on every row this snapshot produced. Stored so the delete
    -- half of a replace can never reach rows a real connector wrote.
    source              TEXT NOT NULL DEFAULT 'csv_upload',
    -- Accounts in the snapshot, and their summed amount in micro-units of `currency`. Both are
    -- reported back to the operator after an upload so a truncated paste is visible immediately.
    account_count       INTEGER NOT NULL,
    total_amount_micro  BIGINT NOT NULL,
    -- Single currency per snapshot: summing mixed currencies without an FX rate we do not have
    -- would fabricate a number, so the upload rejects a mixed file rather than guess.
    currency            TEXT NOT NULL,
    filename            TEXT,
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_by         TEXT,
    PRIMARY KEY (tenant_id, period),
    CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CHECK (account_count >= 1),
    CHECK (char_length(currency) = 3)
);

CREATE INDEX IF NOT EXISTS idx_tenant_revenue_uploads_recent
    ON tenant_revenue_uploads(tenant_id, uploaded_at DESC);
