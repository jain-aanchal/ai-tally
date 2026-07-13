-- Revenue-side inputs on cac_periods so the unit-economics page can compute payback + LTV live
-- (CTO-145).
--
-- WHY HERE: CAC periods already carry the *cost* side (spend + customer counts). Payback and LTV
-- additionally need the *revenue* side — ARPA (average revenue per account / month) and gross
-- margin. Option A: finance enters these alongside the spend figures on the same monthly row, so
-- they live on cac_periods rather than in a separate table. One row per tenant per month still holds.
--
-- NULLABLE ON PURPOSE: existing rows (entered before this migration) have no ARPA / margin. Leaving
-- the columns NULL lets the page render payback/LTV as "—" (honest-null) for those months instead
-- of fabricating a number. A month is "complete" for payback/LTV only once both are filled.
--
-- Money: ARPA is ``micro_usd`` (1/1,000,000 USD), matching the spend columns and the rest of the
-- wire/store contract. Gross margin is a fraction in [0,1] (0.78 = 78%), constrained by CHECK.
ALTER TABLE cac_periods
    ADD COLUMN IF NOT EXISTS arpa_micro_usd   BIGINT
        CHECK (arpa_micro_usd IS NULL OR arpa_micro_usd >= 0),
    ADD COLUMN IF NOT EXISTS gross_margin_pct NUMERIC(5, 4)
        CHECK (gross_margin_pct IS NULL OR (gross_margin_pct >= 0 AND gross_margin_pct <= 1));
