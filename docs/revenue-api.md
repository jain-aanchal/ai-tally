# Generic revenue API (CTO-199)

`POST /v1/revenue/events` accepts one revenue event from any billing system. It exists for tenants
whose biller we have no connector for: Chargebee, Recurly, Zuora, or something home-grown. It is the
automatable alternative to a CSV upload, and everything it writes is an ordinary `business_events`
row, so revenue that arrives this way is indistinguishable downstream from revenue that arrives
through the Stripe or HubSpot connectors.

- Endpoint: `infra/gateway/src/gateway/app.py` (`ingest_revenue_event`)
- Payload contract and validation: `tally.cdp_connectors.GenericRevenueConnector`
- Hashing and the wire mapping: `infra/gateway/src/gateway/revenue_api.py`

Not in scope, deliberately: a data-warehouse connector (Snowflake, BigQuery) that queries revenue
where it is already modelled. That is the higher-fidelity answer for a large company and it is its
own project.

## Request

```
POST /v1/revenue/events
Authorization: Bearer <tenant api key>     # or X-Tenant-Id when auth is disabled (local dev)
Content-Type: application/json
```

| Field         | Required | Type   | Notes                                                                           |
| ------------- | -------- | ------ | ------------------------------------------------------------------------------- |
| `event_id`    | yes      | string | Your stable id for this event. The idempotency key. Max 200 chars.              |
| `account_id`  | yes      | string | Your paying customer. Hashed on ingest, never stored raw. Max 512 chars.        |
| `amount`      | yes\*    | string or number | Major units of `currency`, non-negative. A string avoids float rounding. |
| `currency`    | no       | string | 3-letter ISO-4217. Defaults to `USD`. Recorded, never converted.                |
| `occurred_at` | yes      | string | ISO-8601. When the money moved, not when you posted it. Naive is read as UTC.   |
| `event_name`  | yes      | string | Your label, e.g. `invoice_paid`, `subscription_renewed`. Max 128 chars.         |
| `value_type`  | no       | string | `monetary` (default), `mrr`, `refund`, or `count`.                              |
| `user_id`     | no       | string | The individual end user, when you have one. Hashed like `account_id`.           |
| `properties`  | no       | object | Free-form. Kept out of storage; see "What is not stored" below.                 |

\* Required except when `value_type` is `count`, which must not carry an amount.

`occurred_at` is the event's own timestamp and is what every window is computed against. Posting a
July invoice in August puts the revenue in July, which is what makes late arrivals and reconciliation
work.

### value_type

Mirrors the `business_events.ValueType` enum, which is what the attribution reader discriminates
revenue on (CTO-194):

- `monetary` for one-off money, `mrr` for a recurring amount. If your biller emits both for the same
  subscription, set `include_mrr: false` on `POST /v1/tenant/revenue-sources/config` so they are not
  counted twice.
- `refund` nets off. Send it as a **positive** amount with `value_type: "refund"`. A negative
  `amount` is rejected rather than guessed at.
- `count` is an engagement signal with no money. Its `ValueAmountMicro` is stored NULL, never 0: an
  event that says nothing about revenue must not read back as a customer who generated none.

## Idempotency

**A retry never double-counts.** `event_id` becomes `business_events.BusinessEventId`, and the table
is a `ReplacingMergeTree` sorted by `(TenantId, BusinessEventId)`. Two guards sit in front of it:

1. an in-process deduplicator shared with the connector webhooks, and
2. a ClickHouse lookup on that key, which is what still holds after a gateway restart or across
   replicas. The attribution revenue sum reads `business_events` without `FINAL`, so waiting for the
   engine's own merge to collapse a duplicate would leave a window where the money is counted twice.

Retrying a stored `event_id` returns `200` with `"deduplicated": true` instead of `201`. The body of
the retry is ignored: the id decides, so re-posting the same id with a different amount does not
change the stored row. To correct an amount, post a `refund` for the difference under a new
`event_id`.

`event_id` is required and the endpoint will not mint one. An auto-generated id would turn every
network-timeout retry into a second payment.

A write that fails returns `503` and leaves the `event_id` retryable. Losing revenue to a swallowed
retry is the same size of bug as double counting it.

## Response

`201` on a stored event:

```json
{
  "ok": true,
  "deduplicated": false,
  "stored": true,
  "event_id": "cb_inv_2026_08_0042",
  "event_name": "invoice_paid",
  "account_id_hash": "eb9ecb6d055e7213...",
  "value_amount_micro": 1499000000,
  "currency": "USD",
  "value_type": "monetary",
  "source": "revenue-api",
  "counted_as_revenue": true
}
```

`counted_as_revenue` answers whether the tenant's own revenue source configuration (CTO-194) will
count this event on `/attribution`. A tenant who has narrowed `revenue_sources` to `["stripe"]` and
then posts here would otherwise watch their revenue land in ClickHouse and never appear on the
dashboard, with nothing anywhere saying why. Fix it by adding `revenue-api` to the list:

```bash
curl -X POST http://localhost:8080/v1/tenant/revenue-sources/config \
  -H 'X-Tenant-Id: local-dev' -H 'Content-Type: application/json' \
  -d '{"revenue_sources": ["stripe", "revenue-api"], "change_id": "'"$(uuidgen)"'"}'
```

`counted_as_revenue` is `null`, not `false`, when the configuration could not be read. An unknown
answer is reported as unknown; the event is still stored either way, because the configuration
decides what is *counted*, not what is *accepted*.

`422` names the field at fault (`{"detail": "event_id is required and must be a non-empty string"}`).
`503` means storage was unavailable: retry, it is safe.

## Worked example

Verified against the local stack (`docker compose -f infra/docker-compose.yml up -d`):

```bash
curl -X POST http://localhost:8080/v1/revenue/events \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: local-dev' \
  -d '{
        "event_id": "cb_inv_2026_08_0042",
        "account_id": "acct_northwind",
        "amount": "1499.00",
        "currency": "USD",
        "occurred_at": "2026-08-25T12:00:00Z",
        "event_name": "invoice_paid"
      }'
```

```
HTTP 201
{"ok":true,"deduplicated":false,"stored":true,"event_id":"cb_inv_2026_08_0042",
 "event_name":"invoice_paid","account_id_hash":"eb9ecb6d055e72136a09468e7cabfa4b6ae966521c7da05868b72f3e4b3988af",
 "value_amount_micro":1499000000,"currency":"USD","value_type":"monetary","source":"revenue-api",
 "counted_as_revenue":true}
```

Run the identical command again, and again after `docker compose restart gateway`:

```
HTTP 200  {"ok":true,"deduplicated":true,"event_id":"cb_inv_2026_08_0042","stored":false}
HTTP 200  {"ok":true,"deduplicated":true,"event_id":"cb_inv_2026_08_0042","stored":true}
```

```sql
SELECT count(), sum(ValueAmountMicro)
FROM business_events
WHERE TenantId = 'local-dev' AND BusinessEventId = 'cb_inv_2026_08_0042';
-- 1   1499000000
```

In production, replace `-H 'X-Tenant-Id: local-dev'` with `-H "Authorization: Bearer $TALLY_API_KEY"`.

## Identity and the account dimension

`account_id` is HMAC-SHA256'd under the tenant's own key (`tally.hmac_keys`) and stored in
`business_events.AccountIdHash`, the account dimension the cost-per-customer tab is built on
(CTO-180). The raw id never reaches ClickHouse and an account hash cannot be reversed or correlated
across tenants.

When `user_id` is omitted, the account id also fills `UserIdHash`. That mirrors the Stripe connector,
which puts the Stripe *customer* id there, a customer being the account in B2B SaaS, and it is what
makes revenue posted here join in the attribution query exactly like connector-sourced revenue rather
than behaving as a special case. Both columns are filled from the same deterministic hash, so they
agree.

### What is not stored

`RawPayload` is left empty. The payload's distinguishing content is the raw `account_id`, which is
the one thing hashing exists to keep out of the telemetry store, and `properties` is not persisted
for the same reason.

## Related

- `docs/cost-per-customer-plan.md` (workstream E) for where this sits.
- `POST /v1/tenant/revenue-sources/config` (CTO-194) for the revenue policy.
- This source does not yet appear as a card on `/connectors`: the `tenant_integration_runs` connector
  column has a CHECK constraint listing the five webhook integrations, so adding it needs a migration
  and belongs with that change.
