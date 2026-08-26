-- What a tenant intends to SPEND on AI, per period and per scope (CTO-205, F1).
--
-- WHY this table exists. Nothing in this schema has ever recorded a customer's intent. The two
-- existing `daily_budget_usd` columns (`tenant_replay_config`, `tenant_eval_config`) are caps on
-- what ai-tally itself is allowed to spend running replays and evals on a tenant's behalf. They
-- are operational safety valves for our own machinery, not a statement by the customer about their
-- own AI bill. Every "versus budget" number in the spend-forecasting epic (CTO-204) is meaningless
-- until that intent is written down somewhere, and this is that somewhere. See
-- docs/spend-forecasting-scope.md, "The budget model".
--
-- WHY money is BIGINT micro-USD and not NUMERIC dollars. Same reason as everywhere else in this
-- system: a budget is compared against summed span costs which are already micro-USD integers, and
-- mixing a float budget into an integer comparison reintroduces the rounding the integers exist to
-- avoid. A budget of $30,000 is 30000000000. There is no float anywhere on this path.
--
-- WHY the budget is SCOPED and not just a tenant total. "The research agent gets $30k a month" is
-- how teams actually govern AI spend, and it is the difference between a burn-down chart finance
-- reads once a month and one a feature owner reads every day. `scope_kind` names the dimension and
-- `scope_value` the member of it:
--   * tenant  -> the whole bill.        scope_value = '' (the ONLY legal value, see CHECK below).
--   * feature -> one `FeatureId`, matching the feature dimension on daily_feature_rollup.
--   * model   -> one model id, e.g. 'gpt-4o'.
--   * layer   -> one cost layer, e.g. 'compute', matching ALLOWED_LAYERS in tenant_connectors.
-- The four kinds mirror the dimensions the cost queries in web/lib/clickhouse.ts already group by,
-- so a scoped budget is always comparable against a series that actually exists.
--
-- WHY scope_value is '' and never NULL for a tenant-wide budget. It is part of the overlap
-- identity below, and NULL is not equal to NULL. A NULLable scope_value would let a tenant create
-- unlimited duplicate tenant-wide budgets for the same month, each invisible to the other. The
-- empty string compares as an ordinary value and the CHECK constraints keep the two spellings from
-- ever both being legal for the same row.
--
-- WHY OVERLAP IS REJECTED AT WRITE TIME (the central decision of this ticket).
--
-- Two budgets for the same scope and period whose date ranges overlap pose a question with no good
-- answer at read time: which one is the number the burn-down is drawn against? Every tie-break
-- available is arbitrary. Newest-wins silently disables a budget somebody deliberately set.
-- Smallest-wins turns an accidental duplicate into a false breach alert. Summing them invents a
-- budget nobody approved. Worse, whichever rule is chosen has to be re-implemented identically in
-- the projection, the burn-down chart, and the future breach alerts, and the first place it drifts
-- produces an alert that disagrees with the chart on the same screen.
--
-- So an overlapping write is refused, and the tenant is told which existing budget it collided
-- with. The invariant every downstream consumer may rely on is: for a given
-- (tenant, period, scope_kind, scope_value) and a given day, AT MOST ONE budget row applies. That
-- makes budget resolution a lookup rather than a policy, and there is nothing to keep in sync.
--
-- The EXCLUDE constraint is what actually enforces it. An application-level pre-check races: two
-- concurrent POSTs both read no conflict and both insert. The gateway still pre-checks so it can
-- return a useful message naming the colliding budget_id, but correctness rests here, not there.
--
-- An open-ended budget (`ends_on IS NULL`) means "until further notice", which is the common case:
-- a monthly budget is usually a standing figure, not a fixed-term one. It is modelled as a range
-- ending at 'infinity', so a second open-ended budget for the same scope necessarily overlaps it
-- and is refused. Ranges are INCLUSIVE at both ends ('[]'): a budget ending on the 31st covers the
-- 31st, and a successor budget therefore has to start on the 1st, not on the 31st.
--
-- INVARIANT, and it is load-bearing for the whole epic: A TENANT WITH NO ROW HERE IS NORMAL. Every
-- tenant on this system is in that state right now. There is no backfill, no placeholder row, and
-- no implicit budget of zero. A zero budget is a real and different claim ("this feature may spend
-- nothing"), and rendering an absent budget as 0 would report every tenant as infinitely over
-- budget. Downstream renders "no budget set" and omits the variance entirely, per the
-- honest-under-uncertainty rule: unknown is null, never 0.
--
-- Reads and writes go through GET/POST/DELETE /v1/tenant/budgets. The web app never touches
-- Postgres directly, same as the rest of the control plane.

-- Required for the EXCLUDE constraint below: gist needs equality operators for the scalar columns
-- (UUID and TEXT) it combines with the range overlap test. Ships with the postgres image.
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS tenant_budgets (
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Caller-supplied stable handle, so a UI can rename or re-target a budget without the row
    -- identity moving. Not a UUID by fiat: 'research-agent-2026' is a perfectly good budget_id and
    -- reads better in an audit than a random uuid.
    budget_id     TEXT NOT NULL,
    period        TEXT NOT NULL,
    -- Micro-USD. >= 0 rather than > 0 on purpose: a deliberate zero budget for a scope that must
    -- not spend is a legitimate and meaningful thing to record. It is the ABSENCE of a row, not a
    -- zero, that means "no budget".
    amount_micro  BIGINT NOT NULL,
    scope_kind    TEXT NOT NULL,
    scope_value   TEXT NOT NULL DEFAULT '',
    starts_on     DATE NOT NULL,
    -- NULL means open-ended, i.e. "until further notice". Not a sentinel far-future date: the
    -- distinction between "runs until we say otherwise" and "ends on 2099-12-31" is real, and a
    -- sentinel would show up in a UI as a date somebody typed.
    ends_on       DATE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, budget_id),

    CONSTRAINT tenant_budgets_period_check
        CHECK (period IN ('month', 'quarter')),
    CONSTRAINT tenant_budgets_amount_check
        CHECK (amount_micro >= 0),
    CONSTRAINT tenant_budgets_scope_kind_check
        CHECK (scope_kind IN ('tenant', 'feature', 'model', 'layer')),
    -- The two halves of the scope have to agree. A 'tenant' budget naming a feature would be
    -- ambiguous about what it covers, and a 'feature' budget naming nothing could never be matched
    -- against a series.
    CONSTRAINT tenant_budgets_scope_value_check
        CHECK (
            (scope_kind = 'tenant' AND scope_value = '')
            OR (scope_kind <> 'tenant' AND scope_value <> '')
        ),
    CONSTRAINT tenant_budgets_budget_id_check
        CHECK (budget_id <> ''),
    -- An end before the start is not a short budget, it is a typo that would silently cover no
    -- days at all and quietly disable the burn-down.
    CONSTRAINT tenant_budgets_dates_check
        CHECK (ends_on IS NULL OR ends_on >= starts_on),

    -- The overlap rule, enforced by the database rather than by hope. See the header.
    CONSTRAINT tenant_budgets_no_overlap EXCLUDE USING gist (
        tenant_id WITH =,
        period WITH =,
        scope_kind WITH =,
        scope_value WITH =,
        daterange(starts_on, COALESCE(ends_on, 'infinity'::date), '[]') WITH &&
    )
);

-- The read pattern is "every budget this tenant has", fetched once per page render and filtered in
-- memory against the scopes actually on screen. The primary key leads with tenant_id and serves
-- that prefix scan; the EXCLUDE constraint's gist index covers the effective-on-a-date lookup.
-- No further index is warranted at the row counts a budget table reaches.

COMMENT ON TABLE tenant_budgets IS
    'What a tenant intends to spend on AI, per period and scope (CTO-205). Absence of a row means '
    'no budget set, which is a normal state and never an implicit zero.';
