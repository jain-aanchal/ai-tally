# Audit: what happens when someone clicks signup

**Status: audit of `main` at `92a7bf4`, 2026-09-09.** This traces the real path a new customer takes
from a signup click to a dashboard with their own numbers in it, names what they see at each step,
and lists what is missing. It is not a redesign.

Two things bound what could be checked. There is **no marketing site in this repository** (no
`site/`, `www/`, `marketing/` or `landing/`), so ai-tally.com is hosted elsewhere and nothing here
can say what it currently contains. I did not fetch it. And the local stack runs pinned to a tenant
via `TALLY_DEV_TENANT`, which bypasses Clerk entirely, so **I saw the onboarding page render but did
not watch the Clerk signup path**. Everything about Clerk below is read from code, not observed.

## The finding that reframes the rest: there is nowhere to land

Nothing has ever been deployed. `deploy/aws/terraform/README.md:11` says it plainly: "This has never
been applied against an AWS account. Not once, not in a scratch account, not partially." No
`terraform plan` has run against a real provider, there is no `.tfstate` in the tree, no image has
been pushed to the registry the Helm charts name, and no ECS task has ever been scheduled.
`docs/hosted-version-scope.md` still reads "Status: proposal, not built."

So the honest answer to "when someone clicks signup, what happens" is: **today, nothing, because
there is no running instance for the button to point at.** Everything below describes what the code
would do once something is deployed. That is worth knowing precisely, because the gaps compound: a
customer who gets past signup then hits a page that hands them placeholder credentials for a proxy
hostname that no deployment config in this repo stands up.

The one thing that is deployed is `deploy/demo/`, and it is deliberately not a template. It sets
`TALLY_DEV_TENANT` **and** `TALLY_ALLOW_INSECURE_NO_AUTH=1` (`deploy/demo/deploy.sh`, the
`pin_dashboard_tenant` call), which turns authentication off entirely, and it serves synthetic
backfilled data behind Caddy basic auth. It is a showroom, not a product instance.

## The path, step by step

### 1. The click

`web/middleware.ts` protects every route with `clerkMiddleware()` and `auth.protect()`. Exactly three
routes are public, from the matcher:

- `/sign-in(.*)`
- `/sign-up(.*)`
- `/api/webhooks/clerk`

An unauthenticated visitor to anything else is sent to Clerk's hosted sign-in. `web/app/sign-up/[[...sign-up]]/page.tsx`
and `web/app/sign-in/[[...sign-in]]/page.tsx` are thin wrappers around Clerk's `<SignUp />` and
`<SignIn />`, both `force-dynamic` so the keyless CI build does not try to prerender them.

**What a marketing site has to do.** The app expects to receive a browser at `/sign-up` on whatever
host the dashboard is deployed to, with a Clerk publishable key configured on that deployment. That
is the entire contract. A "Sign up" button on ai-tally.com is a link to `https://<dashboard-host>/sign-up`.
Nothing in this repo configures that hostname, and nothing here validates that the link exists. Clerk
also owns whether signup is open or invite-only, which `docs/initiatives/01-organizations-users-access.md`
§12.6 records as an undecided product question, not code.

### 2. No organization yet

The product has no personal workspace. A signed-in user with no active org is redirected by the
middleware to `/select-org`, which renders Clerk's `<OrganizationList hidePersonal>` with
`afterCreateOrganizationUrl="/"`. So the user creates an org and is sent straight to the dashboard
home.

**They are not sent to onboarding.** See the gap list.

### 3. Provisioning, and the race

Creating the org fires Clerk's `organization.created` webhook at `web/app/api/webhooks/clerk/route.ts`.
That route verifies the svix signature (rejecting with 401 before touching the body), then forwards
the verified event to the gateway's service-token-authed `POST /v1/tenant/provision`. On a gateway
failure it returns 502 so Clerk's own retry and backoff apply.

The gateway half is `infra/gateway/src/gateway/tenant_provisioning.py`. It is careful work: idempotent
on redelivery, race-safe via `INSERT ... ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS NOT NULL`,
mints the per-org HMAC key set **before** the insert so a tenant that cannot hash is never created,
and deletes the key set if it loses the race so no orphaned material survives.

**The timing problem.** The webhook is asynchronous. The redirect is immediate. Clerk delivers
`organization.created` on its own schedule while the browser is already loading `/`.

Here is what happens when the browser wins the race. `web/lib/getTenant.ts` resolves the active Clerk
org to a tenant UUID through `GET /v1/tenant/by-clerk-org/{orgId}`. The gateway returns **404** when
no tenant exists for that org (`app.py:2139`, deliberately: "A 404 when the org has no tenant, never
a silent fallback"). `resolveOrgToTenant` then throws:

```ts
if (!res.ok) {
  throw new Error(`by-clerk-org resolve failed: HTTP ${res.status}`);
}
```

That throw is correct. What happens to it is not. **No caller anywhere in `web/` catches it.**
`grep -rn "NoActiveOrgError" web` outside `getTenant.ts` and `clickhouse.ts` returns nothing, and
there is **no `error.tsx` or `global-error.tsx` anywhere under `web/app`** (I checked; the count is
zero). `web/lib/clickhouse.ts` explicitly routes resolution failures *around* its mock fallback,
which is the right call, and then nothing downstream handles them.

So the throw reaches Next's built-in error boundary. In a production build that is the generic
"Application error: a server-side exception has occurred while loading (see more info in server
logs)" page with an opaque digest. The home page also fetches `/api/home`, whose route handler hits
the same resolution and 500s, so `apiGet` throws first with `API /api/home failed: 500`. Either way
the first screen a brand-new customer sees, if their browser beats Clerk's webhook, is an unstyled
crash page with no explanation and no retry affordance.

The window is small and self-healing (the next reload succeeds once the webhook lands), but it sits
at the single highest-stakes moment in the funnel, and if provisioning fails for real (the HMAC key
provider is unreachable, so the gateway returns 503) the customer sits on that same crash page
permanently with no way to tell the two apart.

The docstring on `NoActiveOrgError` says "Callers redirect to select-or-create-org (§7)." No caller
does.

### 4. What credentials the new tenant gets

Provisioning mints **one** thing: the per-org HMAC key set, stored only as a reference in
`tenants.hash_salt_kek_ref`. That key is for hashing account and user identifiers. It is not an
ingest credential and the customer never sees it.

Provisioning mints **no ingest API key**. `docs/initiatives/01-organizations-users-access.md` §2
lists "its own ingest API keys" in the target end state, and §7's provisioning flow does not include
one, and the code does not create one. A brand-new tenant has an empty key list. The customer has to
go to Settings, API Keys, and mint one by hand (`web/app/settings/keys`, through `/api/keys` to the
gateway's `POST /v1/tenant/keys`, admin role required).

Which provider backs the HMAC key depends on `TALLY_HMAC_KEY_PROVIDER`. It defaults to `local`
(`infra/gateway/src/gateway/config.py:65`), whose material is derived from a root secret sitting in
configuration. That is what the credentials-by-reference invariant forbids on a multi-tenant
instance. `SecretManagerKeyProvider` (`TALLY_HMAC_KEY_PROVIDER=kms`) only became real in #352; before
that every method raised `NotImplementedError`. A real deployment must set it to `kms`, install the
gateway's `[secrets]` extra so boto3 is present, and give the task role the Secrets Manager grant.
The Terraform variable already defaults to `kms` (`deploy/aws/terraform/variables.tf:148`), which is
the right default, and has never been applied.

### 5. The onboarding page

`web/app/onboarding/page.tsx` renders two steps and a coverage panel. Step 1, point your app at the
proxy. Step 2, send your first request. Underneath, `CoveragePanel` reports which of five layers a
real span proves.

The page is honest about uncertainty in a way most products are not. Step 2 and the panel read the
**same** coverage poll, so they cannot contradict each other (#320), and a probe that could not be
read renders the blank with a reason rather than a definite "no trace yet". The account layer's count
is labelled "rollup rows" rather than "spans", because that is what it counts (#329).

And step 1 hands the customer credentials that are not theirs.

## The example-credentials gap

Confirmed on the running app. `GET /api/onboarding` returns:

```json
{"tenantKey":"tk_example_replace_me",
 "proxyBaseUrl":"https://proxy.example.ai-tally.dev/v1",
 "isExample":true}
```

and the page renders a warning banner above the snippet reading "EXAMPLE VALUES. Example values, not
this tenant's provisioned credentials. Replace them with the key and proxy URL from your workspace
settings before running this." The same sentence is prepended as a comment inside the copied text, so
a developer who pastes into a terminal still sees it.

The honesty is exemplary. The gap is that **the highest-friction step in the funnel hands the
customer homework instead of a working command.** They must go find a page that the onboarding page
does not link to, work out that they need to mint a key there, and separately discover a proxy URL.

### Why it is like this

The source is `web/lib/onboardingStore.ts`, a single in-process record on `globalThis` with the
placeholder values hard-coded in `freshState()`. Its own comment says "The provisioning path is
control-plane work; in the meantime the values carry `isExample`".

Three separate reasons, and they need different fixes:

**The key.** It is not retrievable at that point, and correctly so. The gateway stores only
`sha256(token)`; the raw token exists exactly once, in the response to `POST /v1/tenant/keys`. There
is no endpoint that can hand back an existing key, and there should not be. So onboarding cannot
*fetch* the tenant's key. It can only *mint* one, or send the customer somewhere that does.

**The proxy URL.** The app does not know it. The only place a proxy URL is derived is
`web/lib/connectSnippets.ts:26-28`, which reads `NEXT_PUBLIC_TALLY_OPENAI_PROXY_URL` and falls back
to `https://openai.proxy.ai-tally.com/v1`. **Nothing in this repo sets that variable.** Not
`web/.env.example`, not `deploy/vercel/`, not the ECS task definitions, not Terraform. The fallback
hostname is a guess written into a default, and no deployment config in this repository stands up
DNS for it. So even the "real" snippets on the API Keys page point at a host that does not exist.

**The wiring already exists, one page over.** `web/app/settings/keys/ConnectPanel.tsx` does exactly
the right thing: it renders immediately after a mint, inlines the real token into per-provider
snippets, and never stores it. Onboarding just does not use it.

### What closes it

In rough order of size:

1. **Set the endpoint variables in the deployment**, or stop defaulting to a hostname nothing
   provisions. Until an actual proxy URL exists, `isExample` on the URL is telling the truth and
   should stay. This is a deployment task, not a code task, and it blocks the other two.
2. **Link `/onboarding` from somewhere.** It is currently unreachable through the UI (see below).
3. **Move the mint into step 1.** Give onboarding a "Create your first ingest key" button that posts
   to the existing `/api/keys`, then render `ConnectPanel` with the returned token, exactly as the
   settings page already does. That replaces the placeholder block with a working one, needs no new
   gateway endpoint, and keeps the show-once property intact. Non-admin members would still see the
   placeholder with an accurate reason, which is the honest outcome.
4. **Optionally, mint a default key at provision time** so the tenant is never keyless. This is the
   `docs/initiatives/01` §2 promise. It is more invasive: a token minted at provision has to be
   shown to a human at some point, and provisioning happens on a webhook with no human present, so
   it would need a "reveal once on first login" mechanism that does not exist. Item 3 avoids that
   problem entirely and I would do it first.

## The three ingest paths

| Path | Where | What it needs |
| --- | --- | --- |
| Python SDK | `sdk/python/`, `tally.init(key)` | An ingest key. Posts to `DEFAULT_ENDPOINT = "https://ingest.ai-tally.com"` (`transport.py:81`), a hostname nothing in this repo provisions either. Bootstraps the tenant HMAC key over `GET /v1/tenant/hmac-key`, so it is the only path that gets per-customer hashing for free. |
| Edge proxy | `infra/edge-proxy/` | The customer runs it themselves. Resolves `X-Tenant-Key` against an in-memory map fed by the gateway's `/v1/edge/keys` delta feed, refreshed every 45s. |
| OTLP | `POST /v1/otlp/traces` (`app.py:680`) | An ingest key with write scope. JSON only, no protobuf. |

**Onboarding steers the customer at the proxy.** Step 1 is titled "Point your app at the proxy" and
the snippet sets `OPENAI_BASE_URL`. The API Keys page's `ConnectPanel` presents proxy and SDK as
co-equal tabs with the proxy first. OTLP is absent from both.

That is the wrong recommendation, and the repo's own scope doc says so.
`docs/self-hosted-scope.md:217-231` is explicit: "For a first install, use the SDK", because it has
zero deployment surface, it is the only path that gets the HMAC bootstrap for free, and it fails
safe. It then says of the proxy: "Do not make the first install depend on it."

The proxy is worse than "one more thing to deploy" for a new customer:

- **Real provider traffic has never gone through it** (#350). It is exercised by tests and by
  `make aider-demo`, not by production traffic anyone depends on.
- **It does not parse usage from streamed responses** (#349). `extractMeta` in
  `internal/proxy/provider.go` does a single `json.Unmarshal` over the buffered body. An SSE body is
  not one JSON document, so the unmarshal fails and the span carries no model and no token counts.
  This is pinned by deliberate tests (`TestOpenAIStreamedUsageIsNotParsed`,
  `TestAnthropicProxyStreamingSSE`) which assert the counts stay nil even when
  `stream_options.include_usage` puts them right there in the final chunk.

So a new customer whose app streams (which is most chat apps) follows onboarding's recommended path
and lands on a dashboard of honest blanks. The product would be telling the truth and the customer
would conclude it is broken. **This is the single worst combination in the audit**: the recommended
path is the least proven one, and it fails in exactly the shape that looks like a product defect.

## What "done" looks like

A number is theirs once a span has arrived, been priced, and been attributed. Those are three
separate dependencies with three separate failure modes.

**Arrived.** `/v1/batches` or `/v1/otlp/traces`, tenant taken from the key. This is the only one that
works reliably today, and it is what the onboarding coverage probe measures.

**Priced.** The catalog is `sdk/python/src/tally/pricing.py`, hand-maintained seed data
(`_SEED_VERSION = "seed-2026-06-15"`) covering a couple of dozen OpenAI, Anthropic, Google and
Bedrock models plus some tool and vector vendors. Its own docstring says to treat the shape as
authoritative and the numbers as placeholders; most rates are tagged `[unverified at implementation
time]`. A model the catalog does not know is **not rejected**: the span lands with
`EstimatedCost = NULL` and `CostSource = 'unpriced'`, and the dashboard carries an `unpriced` count
beside every total so a partial sum renders as an honest blank rather than a low number
(`web/lib/clickhouse.ts:410`). Per-tenant price overrides exist as a full versioned ledger
(`sdk/python/src/tally/overrides.py`) that **nothing calls**: the gateway builds a bare
`seed_catalog()`, there is no endpoint and no UI, so a customer on a negotiated rate would have to
patch the gateway. A customer on a model released after the seed date sees blanks with no way to fix
it themselves.

**Attributed.** Per-customer cost reads `daily_account_rollup`, populated by a ClickHouse materialized
view that is an INSERT trigger on `otel_spans`, not a scheduled job. It is forward-only, so it
captures nothing that predates its creation, and the DDL only fires on a first boot against an empty
volume (an existing stack needs `make ch-migrate`). For rows to carry an account the customer's code
must call `with_account(...)`, which needs the SDK, or pre-hash and send `X-Tally-Account-Id-Hash`.
The feature-level attribution path is different again: `attribution_records` is built by the stitcher
job, which is registered only when `scheduler_enabled` is true, and that defaults to **false**
(`config.py:241`).

**The five-layer coverage readout overstates readiness.** On the running local stack the probe
reports four layers covered (llm 358,065 spans, tools 28,586, vector 40,000, embeddings 3,989) and
the account layer `not_wired`. That is 4/5 on a stack where per-customer attribution, the thing the
product is actually differentiated on, is not wired at all. A customer reading "4 of 5 layers
proven" would reasonably conclude they are nearly done.

## What is missing, ordered by how likely it is to lose a customer

1. **There is no deployed instance.** Signup has nowhere to land. Nothing else on this list matters
   until an environment exists.
2. **`/onboarding` is unreachable.** It is not in `web/components/Shell.tsx`'s `NAV_GROUPS`, nothing
   links to it (`grep -rn "/onboarding" web/app web/components web/lib` excluding the API routes and
   the lib module returns nothing), `afterCreateOrganizationUrl` sends new orgs to `/`, and neither
   `README.md` nor `RUNNING.md` mentions it. The entire onboarding experience exists and no customer
   will find it. This is a one-line nav entry plus a redirect target and it is the cheapest item on
   this list.
3. **Onboarding hands out placeholder credentials** for a proxy URL the app does not know, and does
   not link to the page that would give real ones. Detailed above.
4. **The recommended path is the least proven one.** Onboarding steers at the edge proxy; the proxy
   has never carried real provider traffic and drops usage on streamed responses. A streaming
   customer sees blanks.
5. **The provisioning race renders a generic crash page.** No `error.tsx`, no catch, no retry. First
   impression, worst case, at the worst moment.
6. **A new tenant has no ingest key** and nothing tells them to mint one.
7. **The onboarding store is a single process-global record, not per-tenant.**
   `web/lib/onboardingStore.ts` keeps one `globalThis.__tallyOnboarding` for the whole server. On a
   multi-tenant deployment every organization shares one onboarding progress record, one funnel, and
   one set of credentials, and all of it resets on every deploy. `signedUpAt` is the time the server
   process first served the page, not anyone's signup. This is a correctness bug in a multi-tenant
   product, not a prototype shortcut, and the file's own comment ("In production these are
   control-plane rows") acknowledges it is not finished.
8. **The checklist can never complete.** Nothing anywhere posts the `first_dashboard` funnel stage
   (`grep -rn "first_dashboard" web` finds only the type definition, the checklist row and the store
   branch). Step 4 never ticks, so the counter maxes at 3/4 forever.
9. **The coverage readout overstates readiness** at 4/5 on a stack with no per-customer attribution.
10. **No per-tenant pricing overrides are reachable**, so a customer on a negotiated rate or a new
    model gets blanks with no self-serve fix.
11. **The scheduler is off by default**, so feature-level attribution, cost connectors and
    reconciliation never run unless someone knows to set `TALLY_SCHEDULER_ENABLED`.
12. **The handoff from ai-tally.com is unspecified.** Nothing in this repo records what host the
    dashboard lives on, what the signup link should be, or that Clerk signup mode (open vs
    invite-only) is an unmade decision.

## Worse than expected

Three things surprised me.

`/onboarding` being completely unreachable. I expected the example credentials to be the top finding
and instead found that the page they are on is not linked from anywhere in the product.

The onboarding store being process-global rather than tenant-scoped. Every other part of this
codebase is scrupulous about tenant scoping, to the point of a `server-only` guard on `getTenant.ts`
and a test that allowlists the one ClickHouse read that omits `FINAL` by name. This one file quietly
serves the same record to every organization.

The proxy hostnames. `openai.proxy.ai-tally.com` and `ingest.ai-tally.com` are written into code as
defaults, appear in generated customer-facing snippets, and are provisioned by nothing anywhere in
this repository. A customer who copies the snippet from the API Keys page today gets a command that
fails DNS.

## Open questions and what I could not verify

- **Anything about ai-tally.com.** I did not fetch it and cannot say what it contains, whether it has
  a signup button, or where that button points.
- **The Clerk path, observed.** The local dev server runs under `TALLY_DEV_TENANT`, which makes the
  middleware a no-op and short-circuits `getTenant()`. I read the sign-up, select-org, webhook and
  resolution code and traced the race by reading it. I did not create a Clerk org and watch it
  provision, and the crash page described in step 3 is derived from the absence of any error boundary
  or catch, not from watching it render.
- **How wide the race window actually is.** That is Clerk webhook delivery latency against a page
  load, which needs a deployed environment to measure. It could be routinely fine or routinely
  visible; I cannot say which.
- **Whether the 502-and-retry path works end to end.** The webhook route returns 502 so Clerk retries,
  and provisioning is idempotent, but nothing has exercised it against a real Clerk instance.
- **Whether `SecretManagerKeyProvider` works against real AWS.** It has unit tests against an injected
  fake. It has never run against Secrets Manager.
- **Whether the migration set is complete for a fresh deployment.** `docs/self-hosted-scope.md`
  records that `db/postgres/` has a numbering collision and that four migrations are unmounted and
  "have never run anywhere". A brand-new tenant on a fresh RDS instance may hit a missing table; I did
  not verify which.
- **Whether the ai-tally.com DNS could point at the demo instance.** If it does, strangers reach an
  instance with authentication deliberately off. I could not check.

## Corrections to existing docs

`docs/hosted-version-scope.md` says the dashboard has no authentication at all. That was true when
written and is not true now: Clerk auth, middleware, orgs, roles and the tenant seam all landed in
Initiative 1. `docs/self-hosted-scope.md` already carries this correction; the hosted doc does not.
Tracked as #342.
