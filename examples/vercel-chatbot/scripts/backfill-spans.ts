// SPDX-License-Identifier: Apache-2.0
// Synthetic 30-day backfill for the chatbot demo, and the repo's all-layers test corpus.
//
// SCRIPTED SEED DATA, NOT REAL USERS AND NOT REAL API CALLS. This script POSTs
// backdated spans + business_events straight to the ai-tally gateway's
// /v1/batches endpoint. It makes NO LLM calls and needs NO OpenAI/Anthropic
// key, so a screenshot run costs $0. The point is to give a freshly-seeded
// stack a month of history that exercises EVERY dimension the dashboard reads,
// instead of a fraction-of-a-cent live run.
//
// What it now covers (CTO-243), and what each part is there to exercise:
//
//   * All six cost layers. `llm` (chat spans), `tools` (per-call tool spans),
//     `embeddings`, `vector` (per-query vector-DB spans), and daily `compute`
//     and `egress` rows in the shape the cloud-billing connectors emit. This is
//     what the Cost tab's layer breakdown and the accounts tab's excluded-infra
//     pot read.
//   * Account attribution. Spans and revenue events carry `AccountIdHash`, the
//     tenant's own paying customer, so Cost per Account and the margin views
//     have real rows. The hash is computed BY THE GATEWAY under the tenant's own
//     HMAC key (POST /v1/tenant/account-lookup); no raw customer id is ever put
//     in a span, and a locally-invented digest would be well-formed and match
//     nothing the product can look up. A slice of traffic is deliberately left
//     unattributed ('') so the unattributed bucket is exercised too.
//   * Several features on several agents (ServiceName), across three providers
//     and six models, so Cost Explorer breakdowns and Model Comparison have
//     something to compare.
//   * Failed runs (billed input tokens, no output) and failed-then-retried runs,
//     so the Recoverable Cost detectors ("Failed but billed", "Duplicated work")
//     have real findings rather than an empty page.
//   * Business events (monetary conversions + count-typed positive feedback) so
//     ROI, attribution and the margin columns are not blank.
//
// HONESTY POSTURE. Every number here is either drawn from a seeded RNG (volumes,
// timings, outcomes) or DERIVED from the seed price catalog (all money). Money is
// integer micro-USD in BigInt end to end; there is no float dollar anywhere in
// this file. Rates mirror sdk/python/src/tally/pricing.py:seed_catalog, and the
// gateway still recomputes the authoritative cost from (provider, model, tokens)
// against that same catalog, so a rate that drifts here shows up as drift rather
// than as a wrong dashboard. Nothing is inflated to make a tile non-blank: where a
// layer is genuinely cheap per call (vector serving is $0.0004/query) its bar is
// honestly small, and a failed call reports 0 output tokens because that is what a
// failed call produced.
//
// Idempotent: batch_ids are derived deterministically from (seed, batch-index)
// so re-running with the same --seed hits the gateway's (tenant_id, batch_id)
// dedup cache (24h TTL) and does not double-count. Use a fresh --seed to layer
// in a second independent month.

import crypto from "node:crypto";

// ---------------------------------------------------------------------------
// Config / CLI
// ---------------------------------------------------------------------------

interface Args {
  gatewayUrl: string;
  controlPlaneUrl: string;
  serviceToken: string;
  tenant: string;
  seed: number;
  days: number;
  targetUsd: number;
  accounts: number;
  dryRun: boolean;
}

const UUID_RE =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** Derive the control-plane origin from the ingest URL (…/v1/batches -> …). */
function originOf(ingestUrl: string): string {
  try {
    return new URL(ingestUrl).origin;
  } catch {
    return "http://localhost:8080";
  }
}

function parseArgs(argv: string[]): Args {
  const gatewayUrl =
    process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080/v1/batches";
  const defaults: Args = {
    gatewayUrl,
    controlPlaneUrl: process.env.TALLY_GATEWAY_BASE_URL ?? originOf(gatewayUrl),
    serviceToken: process.env.GATEWAY_SERVICE_TOKEN ?? "",
    // No default tenant. There used to be one ("local-dev", the tenant NAME), and it was a trap:
    // ingest writes TenantId verbatim while the dashboard reads by UUID (Initiative 1, §8), so the
    // default silently produced 30 days of rows that nothing renders. Unset is a loud failure below.
    tenant: process.env.TALLY_TENANT ?? "",
    seed: 138,
    days: 30,
    targetUsd: 52_400,
    accounts: 12,
    dryRun: false,
  };
  const out = { ...defaults };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    const eq = a.indexOf("=");
    const flag = eq === -1 ? a : a.slice(0, eq);
    const inlineVal = eq === -1 ? undefined : a.slice(eq + 1);
    const take = (): string => {
      if (inlineVal !== undefined) return inlineVal;
      i++;
      return next;
    };
    switch (flag) {
      case "--gateway-url":
        out.gatewayUrl = take();
        out.controlPlaneUrl = originOf(out.gatewayUrl);
        break;
      case "--gateway-base-url":
        out.controlPlaneUrl = take();
        break;
      case "--service-token":
        out.serviceToken = take();
        break;
      case "--tenant":
        out.tenant = take();
        break;
      case "--seed":
        out.seed = parseInt(take(), 10);
        break;
      case "--days":
        out.days = parseInt(take(), 10);
        break;
      case "--target-usd":
        out.targetUsd = parseFloat(take());
        break;
      case "--accounts":
        out.accounts = parseInt(take(), 10);
        break;
      case "--dry-run":
        out.dryRun = true;
        break;
      case "--help":
      case "-h":
        console.log(
          "usage: tsx backfill-spans.ts --tenant <uuid> [--gateway-url URL] " +
            "[--gateway-base-url URL] [--service-token T] [--seed N] [--days N] " +
            "[--target-usd 52400] [--accounts 12] [--dry-run]\n" +
            "  Posts backdated synthetic spans + conversions to the gateway. " +
            "No LLM calls, no API key, $0.\n" +
            "  --tenant is the tenant UUID (make seed prints it); the NAME is refused.",
        );
        process.exit(0);
    }
  }
  if (!Number.isFinite(out.days) || out.days <= 0) {
    throw new Error("--days must be a positive integer");
  }
  if (!Number.isFinite(out.targetUsd) || out.targetUsd <= 0) {
    throw new Error("--target-usd must be a positive number");
  }
  if (!Number.isFinite(out.accounts) || out.accounts < 0) {
    throw new Error("--accounts must be a non-negative integer");
  }
  // Honest under uncertainty: refuse a missing or name-shaped tenant rather than write invisible
  // rows. Mirrors deploy/demo/lib-tenant.sh's resolve_tenant_uuid and infra/Makefile's guard.
  if (!UUID_RE.test(out.tenant)) {
    throw new Error(
      `--tenant must be the tenant UUID, got '${out.tenant || "<empty>"}'.\n` +
        "  The canonical TenantId is the tenant UUID (Initiative 1, §8): ingest writes TenantId\n" +
        "  verbatim and the dashboard binds the UUID into the ClickHouse read filter, so a NAME\n" +
        "  lands rows that nothing ever renders. There is deliberately no fallback.\n" +
        "  Get it with:  make -C infra seed        (it prints the UUID)\n" +
        "  or:           make -C infra psql  ->  SELECT id, name FROM tenants;",
    );
  }
  return out;
}

// Mulberry32: the same deterministic PRNG the live driver uses.
function makeRng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Deterministic hex id of `bytes` bytes, drawn from the seeded RNG so a run is reproducible. */
function makeHexId(rng: () => number): (bytes: number) => string {
  return (bytes: number) => {
    let s = "";
    for (let i = 0; i < bytes * 2; i++) s += Math.floor(rng() * 16).toString(16);
    return s;
  };
}

// Deterministic batch_id so re-runs at the same --seed dedup on the gateway.
function batchId(seed: number, n: number): string {
  return crypto
    .createHash("sha256")
    .update(`tally-backfill:${seed}:${n}`)
    .digest("hex")
    .slice(0, 32);
}

// Deterministic 64-char user hash for a synthetic user bucket.
//
// WHY a plain digest is right here but NOT for the account dimension: a user hash is never looked
// up by plaintext anywhere in the product, so any stable 64-hex value is a faithful stand-in for
// "some end user". An ACCOUNT hash is looked up (POST /v1/tenant/account-lookup backs the
// cost-per-customer search box and the label store keys on the digest), so it must be the digest
// the tenant's own HMAC key produces. See resolveAccounts below.
function userHash(seed: number, n: number): string {
  return crypto
    .createHash("sha256")
    .update(`tally-backfill-user:${seed}:${n}`)
    .digest("hex");
}

/** Deterministic (traceId, spanId) for a daily connector-shaped row, mirroring
 *  gateway.connectors.base.synthetic_span_id so a re-run re-derives the same ids.
 *
 *  The seed is part of the key on purpose. /v1/batches has no per-span existence guard (that lives
 *  in the connector path), so a second backfill at a FRESH seed would otherwise re-emit rows with
 *  identical span ids and silently double the cloud-bill layers. Keying on the seed keeps a
 *  layered month a genuinely separate set of rows, while a same-seed re-run is still a no-op via
 *  the gateway's (tenant_id, batch_id) dedup. */
function syntheticSpanId(
  tenant: string,
  seed: number,
  provider: string,
  operation: string,
  day: string,
  feature: string,
): { traceId: string; spanId: string } {
  const digest = crypto
    .createHash("sha256")
    .update(`${tenant}|${seed}|${provider}|${operation}|${day}|${feature}`)
    .digest("hex");
  return { traceId: digest.slice(0, 32), spanId: digest.slice(32, 48) };
}

// ---------------------------------------------------------------------------
// Money. Integer micro-USD in BigInt, never float dollars (CLAUDE.md).
// ---------------------------------------------------------------------------

const MICRO_PER_USD = 1_000_000n;
const TOKENS_PER_MTOK = 1_000_000n;

/** micro-USD for `tokens` at `microPerMtok` (micro-USD per million tokens). */
function tokenCost(tokens: number, microPerMtok: bigint): bigint {
  return (BigInt(tokens) * microPerMtok) / TOKENS_PER_MTOK;
}

function fmtUsd(micro: bigint): string {
  const neg = micro < 0n;
  const abs = neg ? -micro : micro;
  const whole = abs / MICRO_PER_USD;
  const cents = (abs % MICRO_PER_USD) / 10_000n;
  return `${neg ? "-" : ""}$${whole.toLocaleString("en-US")}.${cents.toString().padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Seed story: features, agents, providers, models, and per-call rates.
//
// Every rate below mirrors sdk/python/src/tally/pricing.py:seed_catalog. The gateway recomputes the
// authoritative cost from the same catalog, so these are used only to SIZE token volumes and to
// report an expectation the ClickHouse total can be checked against.
// ---------------------------------------------------------------------------

// Feature mix (share of the LLM-layer spend) plus the agent (ServiceName) that runs it. Distinct
// agents matter: /agents groups by ServiceName, and the waste detectors scope a finding to a
// feature when tagged and to its agent otherwise, so a single ServiceName would collapse the whole
// corpus into one row and prove nothing.
interface Feature {
  tag: string;
  agent: string;
  share: number;
  /** Context-size multiplier: research runs big RAG windows, the chatbot is light. */
  scale: number;
  /** Whether this feature does retrieval (drives vector + embedding spans). */
  retrieval: boolean;
}
const FEATURES: Feature[] = [
  { tag: "research_agent", agent: "research-agent", share: 0.54, scale: 1.8, retrieval: true },
  { tag: "support_triage", agent: "support-bot", share: 0.17, scale: 1.0, retrieval: false },
  { tag: "inline_writer", agent: "inline-writer", share: 0.12, scale: 1.0, retrieval: false },
  { tag: "smart_search", agent: "search-service", share: 0.1, scale: 0.9, retrieval: true },
  { tag: "chatbot", agent: "vercel-chatbot", share: 0.07, scale: 0.6, retrieval: true },
];

interface ChatModel {
  provider: "openai" | "anthropic" | "google";
  model: string;
  /** micro-USD per million input / output tokens (seed catalog). */
  inMicroPerMtok: bigint;
  outMicroPerMtok: bigint;
}

// Three providers so /compare and the provider breakdown have more than a binary to show.
const MODELS_BY_PROVIDER: Record<string, ChatModel[]> = {
  openai: [
    { provider: "openai", model: "gpt-5", inMicroPerMtok: 2_500_000n, outMicroPerMtok: 10_000_000n },
    { provider: "openai", model: "gpt-4o-mini", inMicroPerMtok: 150_000n, outMicroPerMtok: 600_000n },
  ],
  anthropic: [
    { provider: "anthropic", model: "claude-sonnet-4-5", inMicroPerMtok: 3_000_000n, outMicroPerMtok: 15_000_000n },
    { provider: "anthropic", model: "claude-haiku-4-5", inMicroPerMtok: 1_000_000n, outMicroPerMtok: 5_000_000n },
  ],
  google: [
    { provider: "google", model: "gemini-2.5-pro", inMicroPerMtok: 1_250_000n, outMicroPerMtok: 10_000_000n },
    { provider: "google", model: "gemini-2.5-flash", inMicroPerMtok: 300_000n, outMicroPerMtok: 2_500_000n },
  ],
};
// Provider mix. Cumulative thresholds against one rng draw.
const PROVIDER_MIX: { provider: string; share: number }[] = [
  { provider: "openai", share: 0.5 },
  { provider: "anthropic", share: 0.35 },
  { provider: "google", share: 0.15 },
];

// Embeddings, priced under PriceType.EMBEDDING in the seed catalog.
const EMBED_MODELS: { model: string; microPerMtok: bigint }[] = [
  { model: "text-embedding-3-small", microPerMtok: 20_000n }, // $0.02 / Mtok
  { model: "text-embedding-3-large", microPerMtok: 130_000n }, // $0.13 / Mtok
];

// Per-call tool rates, _TOOL_SEEDS in the seed catalog, in micro-USD per call.
const TOOLS: { provider: string; name: string; microPerCall: bigint }[] = [
  { provider: "tavily", name: "search", microPerCall: 10_000n },
  { provider: "serpapi", name: "search", microPerCall: 15_000n },
  { provider: "brave", name: "search", microPerCall: 5_000n },
  { provider: "firecrawl", name: "scrape", microPerCall: 20_000n },
  { provider: "exa", name: "search", microPerCall: 10_000n },
  { provider: "openai", name: "code_interpreter", microPerCall: 30_000n },
];

// Per-query vector rates, _VECTOR_SEEDS, in micro-USD per call. This is the SERVING portion only:
// the deployed-index node-hours that dominate a real vector bill are a COMPUTE cost by design (see
// the cost-split note on _VECTOR_SEEDS in pricing.py), and are carried by the compute rows below.
// So the Vector layer here is honestly small; it is not padded to look like the mock's 13%.
const VECTORS: { provider: string; index: string; op: string; microPerCall: bigint }[] = [
  { provider: "pinecone", index: "docs-v3", op: "query", microPerCall: 400n },
  { provider: "pinecone", index: "docs-v3", op: "upsert", microPerCall: 200n },
  { provider: "weaviate", index: "kb-main", op: "query", microPerCall: 300n },
  { provider: "qdrant", index: "tickets", op: "query", microPerCall: 250n },
];

// Daily cloud-bill rows, in the shape gateway.connectors.base.emit_cost_span writes. These are
// aggregate BILL figures, not per-call prices, which is exactly what a cloud-billing connector
// lands: one row per (day, provider, operation, feature). Sized as a share of the LLM headline so
// the all-in story stays plausible for a startup at this spend level.
const COMPUTE_SHARE_OF_LLM = 0.045;
const EGRESS_SHARE_OF_LLM = 0.004;
const COMPUTE_PROVIDERS = ["gcp", "aws"];
const EGRESS_PROVIDERS = ["vercel", "aws"];
const CONNECTOR_SERVICE_NAME = "cloud-billing";

// How much non-LLM per-call activity a run carries. Span-count shares, NOT dollar shares: these
// layers are micro-priced, so sizing them by dollars would need millions of spans. Every one of
// them still costs exactly what the catalog says.
const TOOL_CALL_RATE = 0.22; // fraction of runs that call a paid tool
const RETRIEVAL_VECTOR_QUERIES = 3; // up to N vector queries on a retrieval run
const RETRIEVAL_EMBED_RATE = 0.45; // fraction of retrieval runs that re-embed a document batch

// Outcomes. A failed run is billed for its input tokens and returns nothing.
const FAILED_RUN_RATE = 0.035; // failed, never retried -> "Failed but billed"
const RETRIED_RUN_RATE = 0.025; // failed, then a same-shape retry succeeds -> "Duplicated work"
const RETRY_GAP_MIN_S = 15; // well inside the detector's 5-minute retry window
const RETRY_GAP_MAX_S = 120;

// Accounts. A realistic book of business is top-heavy: a few large customers carry most of the
// spend. `UNATTRIBUTED_RATE` of runs carry no account at all, which is the honest '' bucket the
// otel_spans DDL documents, not a customer named "unknown".
const ACCOUNT_NAMES: [string, string][] = [
  ["acct-northwind", "Northwind Traders"],
  ["acct-initech", "Initech"],
  ["acct-globex", "Globex"],
  ["acct-umbrella", "Umbrella Health"],
  ["acct-hooli", "Hooli"],
  ["acct-soylent", "Soylent Corp"],
  ["acct-vehement", "Vehement Capital"],
  ["acct-massive-dynamic", "Massive Dynamic"],
  ["acct-cyberdyne", "Cyberdyne Systems"],
  ["acct-stark", "Stark Industries"],
  ["acct-wayne", "Wayne Enterprises"],
  ["acct-acme", "Acme Robotics"],
];
const UNATTRIBUTED_RATE = 0.08;

// Conversion rates per provider, and the synthetic deal size.
const CONVERSION_RATE: Record<string, number> = {
  openai: 0.13,
  anthropic: 0.15,
  google: 0.12,
};
const POSITIVE_FEEDBACK_RATE = 0.75;
const CONVERSION_MIN_MICRO = 40_000_000n; // $40
const CONVERSION_MAX_MICRO = 200_000_000n; // $200

// ---------------------------------------------------------------------------
// Account resolution (real HMAC digests, from the gateway)
// ---------------------------------------------------------------------------

interface Account {
  id: string;
  label: string;
  hash: string;
  keyVersion: string;
  /** Relative share of account-attributed traffic. */
  weight: number;
}

/**
 * Resolve each synthetic account id to the digest the tenant's OWN HMAC key produces, and label it.
 *
 * WHY the round-trip instead of hashing here: `AccountIdHash` is HMAC-SHA256 under a per-tenant key
 * held by the gateway (CTO-180/CTO-185). A digest invented in this script would be a well-formed
 * 64-hex string that the cost-per-customer search box could never resolve and the label store could
 * never key on, so the account dimension would look populated and be unusable. The plaintext id is
 * sent once, used for one HMAC call, and never stored (see the endpoint's docstring).
 *
 * `hashes[0]` is the digest for the UUID spelling of the tenant, which is the spelling we tag spans
 * with; the endpoint also returns the name-spelling digest, and the label upsert covers both.
 *
 * Fails loudly. An unreachable control plane here would otherwise silently produce a corpus with an
 * empty account dimension: every tile blank, no error.
 */
async function resolveAccounts(args: Args): Promise<Account[]> {
  const wanted = ACCOUNT_NAMES.slice(0, args.accounts);
  const out: Account[] = [];
  for (let i = 0; i < wanted.length; i++) {
    const [id, label] = wanted[i];
    const res = await fetch(`${args.controlPlaneUrl}/v1/tenant/account-lookup`, {
      method: "POST",
      headers: controlPlaneHeaders(args),
      body: JSON.stringify({ account_id: id }),
    });
    if (!res.ok) {
      throw new Error(
        `account-lookup for one of the synthetic accounts failed (${res.status}): ${await res.text()}\n` +
          `  Endpoint: ${args.controlPlaneUrl}/v1/tenant/account-lookup\n` +
          "  The account dimension needs the tenant's own HMAC digest; there is no safe local\n" +
          "  substitute. Check the gateway is up, and pass --service-token (or GATEWAY_SERVICE_TOKEN)\n" +
          "  when TALLY_REQUIRE_API_KEY is on.",
      );
    }
    const body = (await res.json()) as { account_id_hash: string; key_version: string };
    out.push({
      id,
      label,
      hash: body.account_id_hash,
      keyVersion: body.key_version,
      // Zipf-ish book of business: account 1 carries ~8x what account 12 does.
      weight: 1 / (i + 1),
    });
  }
  return out;
}

function controlPlaneHeaders(args: Args): Record<string, string> {
  const h: Record<string, string> = {
    "Content-Type": "application/json",
    "x-tenant-id": args.tenant,
  };
  if (args.serviceToken) h.Authorization = `Bearer ${args.serviceToken}`;
  return h;
}

/**
 * Upsert the human-readable label for each account.
 *
 * Fail-SOFT, unlike the hash resolution above, and the asymmetry is deliberate: a label is optional
 * cosmetic metadata that lives in Postgres and is joined at render time. Without it the tab renders
 * a shortened hash, which is a designed state (CTO-186), not a broken one. A missing HASH, by
 * contrast, silently empties a whole dimension.
 */
async function labelAccounts(args: Args, accounts: Account[]): Promise<number> {
  let ok = 0;
  for (const a of accounts) {
    try {
      const res = await fetch(`${args.controlPlaneUrl}/v1/tenant/account-labels`, {
        method: "POST",
        headers: controlPlaneHeaders(args),
        body: JSON.stringify({ account_id: a.id, label: a.label }),
      });
      if (res.ok) ok++;
      else console.warn(`  ! label for ${a.id} not set (${res.status}); it will render as a hash`);
    } catch (err) {
      console.warn(`  ! label for ${a.id} not set (${String(err)}); it will render as a hash`);
    }
  }
  return ok;
}

// ---------------------------------------------------------------------------
// Span / event builders (same wire shape as app/lib/tally.ts helpers).
// ---------------------------------------------------------------------------

type Span = Record<string, unknown>;

/** Identity every span in a run shares. */
interface RunIdentity {
  traceId: string;
  agentRunId: string;
  sessionId: string;
  userHash: string;
  account: Account | null;
  feature: Feature;
}

/** Account attributes, or nothing at all. An absent account writes '' server-side, which is the
 *  documented unattributed bucket; we never stamp a placeholder id. */
function accountAttrs(account: Account | null): Span {
  if (!account) return {};
  return {
    "gen_ai.account_id_hash": account.hash,
    "gen_ai.account_id_hash_key_version": account.keyVersion,
    // Wire-only (CTO-181): accepted, validated, and deliberately never persisted to ClickHouse.
    // Sent so this corpus exercises that drop path too.
    "gen_ai.account_label": account.label,
  };
}

function baseSpan(id: RunIdentity, spanId: string, tsNs: number, statusCode: number): Span {
  return {
    ServiceName: id.feature.agent,
    trace_id: id.traceId,
    span_id: spanId,
    timestamp_ns: tsNs,
    duration_ns: 0,
    status_code: statusCode,
    "gen_ai.feature_tag": id.feature.tag,
    "gen_ai.session_id": id.sessionId,
    "gen_ai.user_id_hash": id.userHash,
    "gen_ai.agent.run_id": id.agentRunId,
    "chatbot.run_id": "backfill",
    ...accountAttrs(id.account),
  };
}

function chatSpan(
  id: RunIdentity,
  spanId: string,
  tsNs: number,
  m: ChatModel,
  inTok: number,
  outTok: number,
  stepIndex: number,
  statusCode: number,
): Span {
  return {
    ...baseSpan(id, spanId, tsNs, statusCode),
    SpanName: "chat.completion",
    "gen_ai.system": m.provider,
    "gen_ai.request.model": m.model,
    // A failed call returned no response, so it names no response model. Leaving it off is the
    // honest record; the model expression falls back to the request model.
    ...(statusCode === 2 ? {} : { "gen_ai.response.model": m.model }),
    "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": inTok,
    "gen_ai.usage.output_tokens": outTok,
    "gen_ai.agent.step.index": stepIndex,
  };
}

function toolSpan(
  id: RunIdentity,
  spanId: string,
  tsNs: number,
  tool: { provider: string; name: string; microPerCall: bigint },
): Span {
  const micro = Number(tool.microPerCall);
  return {
    ...baseSpan(id, spanId, tsNs, 1),
    SpanName: "tool.execution",
    "gen_ai.system": tool.provider,
    "gen_ai.operation.name": "tool",
    "gen_ai.tool.name": tool.name,
    // One carrier, the same one the SDK's record_tool_call emits, so this corpus is wire-identical
    // to a real tool span and exercises the gateway's promotion rather than bypassing it (CTO-243).
    // This previously also set `gen_ai.cost.estimated_micro_usd` directly, because nothing promoted
    // the tool carrier and the Tools layer bar read a fabricated $0. Now that enrich_cost prices
    // tool and vector calls, writing the cost column here would mask a regression in that promotion:
    // the corpus would look correct even if the gateway stopped resolving these costs.
    "gen_ai.tool.cost_micro_usd": micro,
    "gen_ai.cost.currency": "USD",
  };
}

function vectorSpan(
  id: RunIdentity,
  spanId: string,
  tsNs: number,
  v: { provider: string; index: string; op: string; microPerCall: bigint },
): Span {
  const micro = Number(v.microPerCall);
  return {
    ...baseSpan(id, spanId, tsNs, 1),
    SpanName: "vector.query",
    "gen_ai.system": v.provider,
    "gen_ai.operation.name": "vector",
    // Same {provider}.{index}.{operation} slot the SDK's record_vector_call uses. The gateway
    // prices off the last dot-segment, so an index name containing dots still resolves.
    "gen_ai.tool.name": `${v.provider}.${v.index}.${v.op}`,
    // Single carrier, for the reason spelled out in toolSpan above (CTO-243).
    "gen_ai.tool.cost_micro_usd": micro,
    "gen_ai.cost.currency": "USD",
  };
}

function embeddingSpan(
  id: RunIdentity,
  spanId: string,
  tsNs: number,
  e: { model: string; microPerMtok: bigint },
  inTok: number,
): Span {
  return {
    ...baseSpan(id, spanId, tsNs, 1),
    SpanName: "embeddings",
    "gen_ai.system": "openai",
    "gen_ai.operation.name": "embeddings",
    "gen_ai.request.model": e.model,
    "gen_ai.usage.input_tokens": inTok,
    // Sent as a hint; the gateway recomputes it from the catalog's EMBEDDING tier and its value wins.
    "gen_ai.cost.estimated_micro_usd": Number(tokenCost(inTok, e.microPerMtok)),
    "gen_ai.cost.currency": "USD",
  };
}

/** A daily cloud-bill row, shaped like gateway.connectors.base.emit_cost_span. No user and no
 *  account: this is tenant-level infrastructure spend, which is exactly why the dashboard excludes
 *  compute/egress from run-shaped reads and reports them as a separate excluded-infra pot. */
function cloudBillSpan(
  tenant: string,
  seed: number,
  operation: "compute" | "egress",
  provider: string,
  day: string,
  feature: string,
  tsNs: number,
  costMicro: bigint,
): Span {
  const { traceId, spanId } = syntheticSpanId(tenant, seed, provider, operation, day, feature);
  return {
    ServiceName: CONNECTOR_SERVICE_NAME,
    SpanName: `${operation}.daily`,
    trace_id: traceId,
    span_id: spanId,
    timestamp_ns: tsNs,
    duration_ns: 0,
    status_code: 0,
    "gen_ai.system": provider,
    "gen_ai.operation.name": operation,
    "gen_ai.cost.estimated_micro_usd": Number(costMicro),
    "gen_ai.cost.currency": "USD",
    "gen_ai.feature_tag": feature,
  };
}

function conversionEvent(
  occurredNs: number,
  uHash: string,
  accountHash: string,
  valueMicro: bigint,
): Record<string, unknown> {
  return {
    business_event_id: crypto.randomUUID(),
    event_name: "conversion",
    user_id_hash: uHash,
    account_id_hash: accountHash,
    occurred_at_ns: occurredNs,
    value_amount_micro: Number(valueMicro),
    value_currency: "USD",
    value_type: "monetary",
    source: "vercel-chatbot-backfill",
  };
}

function feedbackEvent(
  occurredNs: number,
  uHash: string,
  accountHash: string,
): Record<string, unknown> {
  return {
    business_event_id: crypto.randomUUID(),
    event_name: "positive_feedback",
    user_id_hash: uHash,
    account_id_hash: accountHash,
    occurred_at_ns: occurredNs,
    // An engagement signal carries no money. null, not 0: a value we do not have is not a value of
    // zero, and value_type 'count' is the discriminator the revenue policy reads.
    value_amount_micro: null,
    value_currency: "USD",
    value_type: "count",
    source: "vercel-chatbot-backfill",
  };
}

// ---------------------------------------------------------------------------
// Generation
// ---------------------------------------------------------------------------

interface LayerTotals {
  llm: bigint;
  tools: bigint;
  vector: bigint;
  embeddings: bigint;
  compute: bigint;
  egress: bigint;
}

interface Generated {
  spans: Span[];
  events: Record<string, unknown>[];
  layers: LayerTotals;
  runs: number;
  failedRuns: number;
  retriedRuns: number;
  unattributedRuns: number;
  conversions: number;
  feedback: number;
}

function pickWeighted<T>(rng: () => number, items: T[], weightOf: (t: T) => number): T {
  const total = items.reduce((s, i) => s + weightOf(i), 0);
  let r = rng() * total;
  for (const i of items) {
    r -= weightOf(i);
    if (r <= 0) return i;
  }
  return items[items.length - 1];
}

function generate(args: Args, accounts: Account[]): Generated {
  const rng = makeRng(args.seed);
  const hexId = makeHexId(rng);
  const nowNs = Date.now() * 1_000_000;
  const windowNs = args.days * 24 * 60 * 60 * 1_000_000_000;
  const startNs = nowNs - windowNs;

  // Spread a timestamp across the window, skewed toward the recent end so the trend line rises.
  function randTsNs(): number {
    const u = rng();
    const skew = 1 - (1 - u) * (1 - u);
    return Math.round(startNs + skew * windowNs);
  }

  const spans: Span[] = [];
  const events: Record<string, unknown>[] = [];
  const layers: LayerTotals = {
    llm: 0n, tools: 0n, vector: 0n, embeddings: 0n, compute: 0n, egress: 0n,
  };
  let runs = 0;
  let failedRuns = 0;
  let retriedRuns = 0;
  let unattributedRuns = 0;
  let conversions = 0;
  let feedback = 0;

  let userCounter = 0;
  const nextUser = (): string => userHash(args.seed, userCounter++);

  function pickProvider(): string {
    const r = rng();
    let acc = 0;
    for (const p of PROVIDER_MIX) {
      acc += p.share;
      if (r <= acc) return p.provider;
    }
    return PROVIDER_MIX[PROVIDER_MIX.length - 1].provider;
  }

  /**
   * Emit one run (one TraceId): its chat turns plus whatever retrieval and tool work it did.
   * Returns the LLM micro-USD it booked, so the caller can drive a feature to its dollar target.
   *
   * `failed` bills the input tokens and returns none, which is what a call that errored actually
   * cost. The terminal error goes on the LAST span so max(StatusCode) over the trace reads 2, the
   * same derivation queryAgents and both waste detectors use.
   */
  function emitRun(
    feat: Feature,
    m: ChatModel,
    id: RunIdentity,
    tsBaseNs: number,
    failed: boolean,
  ): bigint {
    let llmMicro = 0n;
    const turns = failed ? 1 : 1 + Math.floor(rng() * 4);

    // Retrieval first, the way a RAG run actually orders its work.
    if (feat.retrieval) {
      const queries = 1 + Math.floor(rng() * RETRIEVAL_VECTOR_QUERIES);
      for (let q = 0; q < queries; q++) {
        const v = VECTORS[Math.floor(rng() * VECTORS.length)];
        spans.push(vectorSpan(id, hexId(8), tsBaseNs - 1_000_000_000 + q * 50_000_000, v));
        layers.vector += v.microPerCall;
      }
      if (rng() < RETRIEVAL_EMBED_RATE) {
        const e = EMBED_MODELS[rng() < 0.8 ? 0 : 1];
        const inTok = 20_000 + Math.floor(rng() * 80_000);
        spans.push(embeddingSpan(id, hexId(8), tsBaseNs - 2_000_000_000, e, inTok));
        layers.embeddings += tokenCost(inTok, e.microPerMtok);
      }
    }

    for (let t = 0; t < turns; t++) {
      const inTok = Math.round((18_000 + rng() * 42_000) * feat.scale);
      const isLast = t === turns - 1;
      const outTok = failed && isLast ? 0 : Math.round((3_000 + rng() * 9_000) * feat.scale);
      const status = failed && isLast ? 2 : 1;
      const tsNs = tsBaseNs + t * 2_000_000_000;
      spans.push(chatSpan(id, hexId(8), tsNs, m, inTok, outTok, t, status));
      llmMicro += tokenCost(inTok, m.inMicroPerMtok) + tokenCost(outTok, m.outMicroPerMtok);
    }

    if (rng() < TOOL_CALL_RATE) {
      const tool = TOOLS[Math.floor(rng() * TOOLS.length)];
      spans.push(toolSpan(id, hexId(8), tsBaseNs + turns * 2_000_000_000 - 500_000_000, tool));
      layers.tools += tool.microPerCall;
    }

    layers.llm += llmMicro;
    runs++;
    if (!id.account) unattributedRuns++;
    return llmMicro;
  }

  // --- Runs, per feature, until each feature hits its LLM dollar target ------
  const llmTargetMicro = BigInt(Math.round(args.targetUsd * 1_000_000));
  for (const feat of FEATURES) {
    const featTargetMicro = (llmTargetMicro * BigInt(Math.round(feat.share * 10_000))) / 10_000n;
    let featMicro = 0n;
    while (featMicro < featTargetMicro) {
      const provider = pickProvider();
      const pool = MODELS_BY_PROVIDER[provider];
      // Bias toward the more capable (pricier) model so the span count stays believable: a startup
      // at this spend is making ~100k substantial calls, not millions of micro-calls.
      const m = rng() < 0.75 ? pool[0] : pool[1];
      const account =
        accounts.length === 0 || rng() < UNATTRIBUTED_RATE
          ? null
          : pickWeighted(rng, accounts, (a) => a.weight);
      const uHash = nextUser();
      const tsBaseNs = randTsNs();
      const roll = rng();
      const isRetried = roll < RETRIED_RUN_RATE;
      const isFailed = !isRetried && roll < RETRIED_RUN_RATE + FAILED_RUN_RATE;

      const mkIdentity = (): RunIdentity => ({
        traceId: hexId(16),
        agentRunId: hexId(8),
        sessionId: `backfill-${args.seed}-${hexId(4)}`,
        userHash: uHash,
        account,
        feature: feat,
      });

      if (isRetried) {
        // Duplicated work: a failed attempt, then a same-shape retry that succeeds. The detector
        // clusters on (feature, agent, model, user) and needs the pair inside its 5-minute window
        // and on DIFFERENT TraceIds, so the two runs share everything but their trace.
        const sess = `backfill-${args.seed}-${hexId(4)}`;
        const first: RunIdentity = { ...mkIdentity(), sessionId: sess };
        featMicro += emitRun(feat, m, first, tsBaseNs, true);
        const gapS = RETRY_GAP_MIN_S + Math.floor(rng() * (RETRY_GAP_MAX_S - RETRY_GAP_MIN_S));
        const second: RunIdentity = { ...mkIdentity(), sessionId: sess };
        featMicro += emitRun(feat, m, second, tsBaseNs + gapS * 1_000_000_000, false);
        retriedRuns++;
      } else {
        featMicro += emitRun(feat, m, mkIdentity(), tsBaseNs, isFailed);
        if (isFailed) failedRuns++;
      }

      // Outcome events for the session. A failed-and-never-retried run converts nothing, which is
      // both realistic and keeps the ROI view from being flattered by spend that produced nothing.
      if (!isFailed) {
        const occurredNs = tsBaseNs + 12_000_000_000;
        const acctHash = account ? account.hash : "";
        if (rng() < POSITIVE_FEEDBACK_RATE) {
          events.push(feedbackEvent(occurredNs, uHash, acctHash));
          feedback++;
        }
        if (rng() < (CONVERSION_RATE[m.provider] ?? 0.12)) {
          const span = CONVERSION_MAX_MICRO - CONVERSION_MIN_MICRO;
          const valueMicro =
            CONVERSION_MIN_MICRO + (BigInt(Math.floor(rng() * 1_000_000)) * span) / 1_000_000n;
          events.push(conversionEvent(occurredNs, uHash, acctHash, valueMicro));
          conversions++;
        }
      }
    }
  }

  // --- Daily compute + egress rows (cloud-billing connector shape) -----------
  // One row per (day, provider, feature). Each day's total is the monthly target spread with a
  // mild day-to-day wobble, so the Compute and Egress layers have a real daily series rather than
  // a flat line, and the excluded-infra pot on the accounts tab is non-empty.
  const computeTotal = (layers.llm * BigInt(Math.round(COMPUTE_SHARE_OF_LLM * 10_000))) / 10_000n;
  const egressTotal = (layers.llm * BigInt(Math.round(EGRESS_SHARE_OF_LLM * 10_000))) / 10_000n;
  const dayMs = 24 * 60 * 60 * 1000;
  for (let d = 0; d < args.days; d++) {
    // Noon UTC on the day, matching the connector, so toDate(Timestamp) partitions it correctly
    // regardless of the reader's timezone.
    const dayDate = new Date(Date.now() - (args.days - 1 - d) * dayMs);
    const day = dayDate.toISOString().slice(0, 10);
    const tsNs = Date.parse(`${day}T12:00:00.000Z`) * 1_000_000;
    for (const feat of FEATURES) {
      const share = BigInt(Math.round(feat.share * 10_000));
      const wobble = BigInt(850 + Math.floor(rng() * 300)); // 0.85x .. 1.15x
      const compute =
        (computeTotal * share * wobble) / (10_000n * 1_000n * BigInt(args.days));
      const egress = (egressTotal * share * wobble) / (10_000n * 1_000n * BigInt(args.days));
      if (compute > 0n) {
        const p = COMPUTE_PROVIDERS[d % COMPUTE_PROVIDERS.length];
        spans.push(cloudBillSpan(args.tenant, args.seed, "compute", p, day, feat.tag, tsNs, compute));
        layers.compute += compute;
      }
      if (egress > 0n) {
        const p = EGRESS_PROVIDERS[d % EGRESS_PROVIDERS.length];
        spans.push(cloudBillSpan(args.tenant, args.seed, "egress", p, day, feat.tag, tsNs, egress));
        layers.egress += egress;
      }
    }
  }

  return {
    spans,
    events,
    layers,
    runs,
    failedRuns,
    retriedRuns,
    unattributedRuns,
    conversions,
    feedback,
  };
}

// ---------------------------------------------------------------------------
// POST batching
// ---------------------------------------------------------------------------

const SPANS_PER_BATCH = 500;
const EVENTS_PER_BATCH = 500;
const RETRY_MAX_ATTEMPTS = 60;
const DEFAULT_RETRY_AFTER_MS = 250;
const MAX_RETRY_WAIT_MS = 5_000;

/**
 * Rows this run metered and could not ship. Shed-and-COUNT is the terminal case of the retry loop
 * (#315): a bounded budget can be spent, and when it is, the run says so in numbers rather than
 * reporting a clean finish over a hole in the data.
 */
const shed = { spans: 0, events: 0, batches: 0 };

/**
 * What the gateway asked us to wait, in ms, or null when it named nothing.
 *
 * `Retry-After` (delay-seconds) is the header form the rate limiter and the overload shed both send
 * (gateway/app.py). The body states it at the top level on a 429 and under `server_hints` on a 503.
 * A hinted 0 is a REAL value: the overload shed sends `retry_after_ms: 0` meaning "retry, we have
 * no specific delay for you", which is not licence to hammer a gateway that is already shedding, so
 * the caller answers a 0 with its own backoff. A malformed header or body yields null the same way.
 */
function hintedWaitMs(res: Response | null, text: string): number | null {
  const header = res?.headers.get("retry-after");
  if (header) {
    const secs = Number.parseInt(header.trim(), 10);
    if (Number.isFinite(secs) && secs >= 0) return secs * 1000;
  }
  try {
    const parsed = JSON.parse(text) as {
      retry_after_ms?: number;
      server_hints?: { retry_after_ms?: number };
    };
    for (const v of [parsed.retry_after_ms, parsed.server_hints?.retry_after_ms]) {
      if (typeof v === "number" && Number.isFinite(v) && v >= 0) return v;
    }
  } catch {
    // body was not the shape we expected; it carries no hint
  }
  return null;
}

/** The gateway's own wait when it named a positive one, clamped, else capped exponential backoff. */
function retryWaitMs(attempt: number, hinted: number | null): number {
  if (hinted !== null && hinted > 0) return Math.min(hinted, MAX_RETRY_WAIT_MS);
  return Math.min(DEFAULT_RETRY_AFTER_MS * 2 ** Math.min(attempt, 5), MAX_RETRY_WAIT_MS);
}

async function postBatch(
  args: Args,
  n: number,
  spans: Span[],
  events: Record<string, unknown>[],
): Promise<void> {
  const batch = {
    tenant_id: args.tenant,
    sdk_version: "vercel-chatbot-backfill/0.2",
    batch_id: batchId(args.seed, n),
    resource_spans: spans,
    business_events: events,
  };
  if (args.dryRun) return;
  const body = JSON.stringify(batch);
  // A 30-day corpus is ~500k spans, which is far more than a flat-out loop can push past the
  // gateway's two load guards: the per-tenant rate limit (429, CTO-33) and backpressure shedding
  // (503 `status: retry`, CTO-36). Both are the gateway working as designed and both say "come
  // back shortly", so honour them rather than failing a half-loaded backfill: this run died at
  // 450,000 of 512,056 spans on a 503 it treated as fatal (#315).
  //
  // `body` is serialized ONCE, above, and every attempt resends those same bytes. That is what
  // makes a resend a replay rather than a duplicate: batch_id is stable, so the gateway's
  // (tenant_id, batch_id) dedup absorbs it. A retry that rebuilt the payload would instead write
  // duplicate spans, which permanently pollute the SummingMergeTree rollups (#311). The same
  // property is why a whole re-run at the same --seed is idempotent.
  //
  // The policy mirrors the edge proxy (infra/edge-proxy/internal/telemetry/telemetry.go): retry a
  // transport error, a 429 or any 5xx; honour the gateway's stated wait, clamped; bound the
  // attempts; then shed and COUNT. The budget is far longer than the proxy's four attempts because
  // this is a one-shot corpus load with no hot path behind it, and riding out a minute of shedding
  // is worth more here than failing fast.
  for (let attempt = 0; ; attempt++) {
    let res: Response | null = null;
    let text = "";
    let why = "";
    try {
      res = await fetch(args.gatewayUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (res.ok) return;
      text = await res.text();
      why = `${res.status}: ${text}`;
    } catch (err) {
      // A transport failure is the most retryable failure there is: nothing says the gateway even
      // saw the batch, and the stable batch_id makes a blind resend safe.
      why = `transport error: ${err instanceof Error ? err.message : String(err)}`;
    }
    // Backpressure and server faults are transient by definition. Every other status is the gateway
    // saying this batch (or this configuration) is wrong, and resending identical bytes would only
    // buy a second refusal, so it aborts the run: unlike the proxy, a CLI has an operator who can
    // fix the credential or the tenant and re-run, and every later batch would fail identically.
    const retryable = res === null || res.status === 429 || res.status >= 500;
    if (!retryable) {
      throw new Error(`POST ${args.gatewayUrl} ${why}`);
    }
    if (attempt >= RETRY_MAX_ATTEMPTS) {
      shed.spans += spans.length;
      shed.events += events.length;
      shed.batches++;
      console.warn(
        `  ! batch ${n} shed after ${attempt + 1} attempts (${spans.length} spans, ` +
          `${events.length} events lost) - last failure ${why}`,
      );
      return;
    }
    await new Promise((r) => setTimeout(r, retryWaitMs(attempt, hintedWaitMs(res, text)) + 25));
  }
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  console.log(
    `Backfilling ~${args.days}d of synthetic traffic ` +
      `(LLM target $${args.targetUsd.toLocaleString("en-US")}, seed=${args.seed}, tenant=${args.tenant})…`,
  );
  console.log(
    "  NOTE: synthetic-seed data: backdated spans, no LLM calls, $0 API spend.",
  );

  const accounts = await resolveAccounts(args);
  console.log(`  · resolved ${accounts.length} account hashes from the tenant's HMAC key`);
  if (!args.dryRun && accounts.length > 0) {
    const labelled = await labelAccounts(args, accounts);
    console.log(`  · labelled ${labelled}/${accounts.length} accounts`);
  }

  const gen = generate(args, accounts);
  const allIn =
    gen.layers.llm + gen.layers.tools + gen.layers.vector +
    gen.layers.embeddings + gen.layers.compute + gen.layers.egress;
  console.log(
    `  · generated ${gen.spans.length} spans across ${gen.runs} runs ` +
      `(${gen.failedRuns} failed-and-not-retried, ${gen.retriedRuns} failed-then-retried, ` +
      `${gen.unattributedRuns} unattributed) + ${gen.events.length} events ` +
      `(${gen.conversions} conversions, ${gen.feedback} positive_feedback)`,
  );
  console.log(
    `  · LLM ${fmtUsd(gen.layers.llm)} · Vector ${fmtUsd(gen.layers.vector)} ` +
      `· Tools ${fmtUsd(gen.layers.tools)} · Compute ${fmtUsd(gen.layers.compute)} ` +
      `· Embeddings ${fmtUsd(gen.layers.embeddings)} · Egress ${fmtUsd(gen.layers.egress)} ` +
      `· all-in ${fmtUsd(allIn)}`,
  );
  console.log(
    "    (an expectation computed from the seed catalog, not the source of truth: the gateway " +
      "recomputes cost from (provider, model, tokens) and its value is what lands)",
  );

  let batchN = 0;
  let posted = 0;
  for (let i = 0; i < gen.spans.length; i += SPANS_PER_BATCH) {
    const chunk = gen.spans.slice(i, i + SPANS_PER_BATCH);
    await postBatch(args, batchN++, chunk, []);
    posted += chunk.length;
    if (batchN % 25 === 0) {
      console.log(`  · posted ${posted}/${gen.spans.length} spans`);
    }
  }
  for (let i = 0; i < gen.events.length; i += EVENTS_PER_BATCH) {
    const chunk = gen.events.slice(i, i + EVENTS_PER_BATCH);
    await postBatch(args, batchN++, [], chunk);
  }

  console.log("");
  if (shed.batches > 0) {
    // Honest under uncertainty (CLAUDE.md): a run that lost rows says how many and exits non-zero,
    // rather than printing a tick over a hole. Re-running at the same --seed is safe and refills it.
    console.error(
      `! Backfill INCOMPLETE: ${shed.spans} spans and ${shed.events} events in ${shed.batches} ` +
        `batch(es) were shed after ${RETRY_MAX_ATTEMPTS + 1} failed attempts each. ` +
        `Delivered ${gen.spans.length - shed.spans}/${gen.spans.length} spans and ` +
        `${gen.events.length - shed.events}/${gen.events.length} events in ${batchN} batches. ` +
        `Re-run at --seed ${args.seed} to fill the gap: it is idempotent.`,
    );
    process.exitCode = 1;
    return;
  }
  console.log(
    `✓ Backfill ${args.dryRun ? "(dry-run) " : ""}done. ` +
      `${gen.spans.length} spans + ${gen.events.length} events in ${batchN} batches. ` +
      `Re-run at --seed ${args.seed} is idempotent.`,
  );
}

main().catch((err) => {
  console.error("backfill-spans failed:", err instanceof Error ? err.message : err);
  process.exit(1);
});
