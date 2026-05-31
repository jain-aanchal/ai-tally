# ai-tally edge proxy (CTO-39)

The zero-code ingestion path. A customer changes one env var — points `OPENAI_BASE_URL` at this
proxy and adds an `X-Tenant-Key` header — and every OpenAI call now flows through ai-tally with
**no SDK, no code change**. Requests are forwarded to the real provider byte-for-byte; the only
thing we keep is a metadata-only telemetry record.

This is the proxy **core**. SSE token reconstruction ([CTO-40]), response extractors ([CTO-41]),
and the customer-key vault ([CTO-42]) are deliberately out of scope here and layer on top via the
`Sink` interface and transport.

## Why a separate Go binary (not the Python gateway)

The proxy sits in the *synchronous* request path of every customer LLM call, so its latency is
added to theirs. The budget is **p99 < 3ms added overhead** — miss it and customers route around
us. That rules out a GC-pause-prone interpreted hot path; a small, stdlib-only Go binary with a hot
connection pool holds the budget with two orders of magnitude to spare (measured ~100µs p99 on
loopback; see below). The async ingest gateway (`../gateway`, FastAPI) stays in Python because it's
off the hot path.

## Design invariants

These are enforced by tests, not just documented:

1. **Bodies are never mutated.** Request and response bodies stream straight through
   `httputil.ReverseProxy`. We never buffer, rewrite, or persist them.
   (`TestTransparentForwarding`, `TestLargeBodyForwardedByteForByte` — 1 MiB exact-echo.)
2. **Telemetry is metadata only.** `TraceRecord` carries tenant key, method, path, status, byte
   *counts*, and timing — never content. Byte counts come from counting readers/writers that tally
   as bytes pass, without copying them. There is structurally no field that could hold a prompt,
   completion, or provider key. (`TestTraceRecordCarriesNoBodyContent`.)
3. **The customer's provider key is in-flight only.** The inbound `Authorization` header is
   forwarded to the upstream unchanged and is never read into any struct, log, or sink. Our own
   `X-Tenant-Key` control header is *stripped* before the request leaves for the provider.
   (`TestTransparentForwarding` asserts both.)
4. **Stateless / horizontally scalable.** No per-request state survives the response; all config is
   env-driven. Run N instances behind any load balancer.
5. **Streaming-safe.** `FlushInterval = -1` flushes each write immediately, so SSE completions pass
   through token-by-token with no added buffering latency. (`TestStreamingPassThrough`.)
6. **Never hangs or panics on a bad upstream.** An unreachable provider yields a clean `502` whose
   body never leaks the upstream address. (`TestUpstreamUnavailableReturns502`.)

## Configuration (all via env)

| Var | Default | Meaning |
|-----|---------|---------|
| `EDGE_PROXY_LISTEN` | `:8088` | bind address |
| `EDGE_PROXY_UPSTREAM` | `https://api.openai.com` | provider origin |
| `EDGE_PROXY_TENANT_HEADER` | `X-Tenant-Key` | control header carrying the tenant id (stripped before upstream) |
| `EDGE_PROXY_REQUIRE_TENANT` | `false` | reject requests missing the tenant header with `400` |
| `EDGE_PROXY_UPSTREAM_TIMEOUT` | `10m` | per-request bound (generous: completions stream for minutes) |
| `EDGE_PROXY_MODE` | `passthrough` | `passthrough` (app sends the provider key) or `broker` (provider key stays in KMS — see below) |
| `EDGE_PROXY_BROKER_FILE` | — | path to the KMS-export JSON; **required** when `EDGE_PROXY_MODE=broker` |
| `EDGE_PROXY_BROKER_TTL` | `5m` | how long a minted credential is reused before re-minting |
| `EDGE_PROXY_SELF_HOSTED` | `false` | label emitted telemetry as `self-host` vs `cloud` |
| `EDGE_PROXY_TELEMETRY_URL` | — | collector endpoint for metadata-only `TraceRecord`s; empty disables shipping |

`/healthz` is the one path the proxy owns (liveness); everything else is forwarded.

## Self-host & BYO-deployment (CTO-43)

The same single binary runs inside a regulated customer's own VPC. Two things change for that
audience: **how the provider key is handled**, and **how telemetry is shipped**.

### Key-broker mode

In the cloud default (`passthrough`) the customer's app sends the provider key on each request and
the proxy forwards it untouched. Regulated customers don't want that key in their application code
at all. **Broker mode** keeps the provider key in the customer's KMS: the app sends *only* an
ai-tally tenant key, and the proxy mints a short-lived provider credential and injects it on the way
upstream. The minted token is applied to the outgoing request header only — never logged, never put
in a `TraceRecord` — preserving the same in-memory-only guarantee as passthrough. An unknown tenant
fails closed with `403`; a broker outage fails closed with `502` (never an unauthenticated upstream
call).

The broker reads a KMS-export file mapping tenant key → provider `Authorization` header:

```json
{
  "tenants": {
    "tk_live_acme":   "Bearer sk-acmes-real-provider-key",
    "tk_live_globex": "Bearer sk-globexs-real-provider-key"
  }
}
```

See [`deploy/keys.example.json`](deploy/keys.example.json). In production this is rendered from your
KMS at deploy time (init container / mounted secret), never committed.

```bash
cd infra/edge-proxy
EDGE_PROXY_MODE=broker \
EDGE_PROXY_BROKER_FILE=./deploy/keys.example.json \
EDGE_PROXY_REQUIRE_TENANT=true \
EDGE_PROXY_SELF_HOSTED=true \
go run ./cmd/edge-proxy
# the app now sends ONLY:  X-Tenant-Key: tk_live_acme  (no Authorization header)
```

### Telemetry parity

A self-hosted proxy emits the **same** metadata-only records as the cloud proxy — same fields, same
wire shape — differing only in a `deployment` label (`self-host` vs `cloud`). Set
`EDGE_PROXY_TELEMETRY_URL` to your ai-tally collector to enable shipping; leave it empty to run the
proxy fully dark. Parity is guaranteed by a single serialization path (`telemetry.Encode`) asserted
by `TestParitySelfHostMatchesCloud`; `TestEncodeCarriesNoBodyContent` whitelists the allowed fields
so no prompt/completion/key field can ever sneak into the envelope.

### Container image

A multi-stage [`Dockerfile`](Dockerfile) builds a fully static binary and ships it on `scratch` — no
shell, package manager, or OS surface — running as a non-root numeric uid (~10 MB image):

```bash
docker build -t ai-tally-edge-proxy:dev infra/edge-proxy
docker run --rm -p 8088:8088 \
  -e EDGE_PROXY_MODE=broker \
  -e EDGE_PROXY_BROKER_FILE=/etc/edge-proxy/keys.json \
  -v "$PWD/infra/edge-proxy/deploy/keys.example.json:/etc/edge-proxy/keys.json:ro" \
  ai-tally-edge-proxy:dev
```

### Helm chart

[`deploy/helm/edge-proxy`](deploy/helm/edge-proxy) packages the proxy for Kubernetes (Deployment +
Service + ConfigMap, optional HPA, locked-down non-root pod, `/healthz` probes). Broker keys come
from either an inline value (dev) or — recommended — a Secret you render from your KMS out-of-band:

```bash
# dev / quick trial: inline keys (renders a Secret for you)
helm install edge-proxy infra/edge-proxy/deploy/helm/edge-proxy \
  --set proxy.telemetryURL=https://ingest.ai-tally.com/v1/traces \
  --set-file broker.inline=infra/edge-proxy/deploy/keys.example.json

# production: reference a Secret you manage (provider keys never touch values.yaml or git)
kubectl create secret generic edge-proxy-broker --from-file=keys.json=./from-kms.json
helm install edge-proxy infra/edge-proxy/deploy/helm/edge-proxy \
  --set broker.existingSecret=edge-proxy-broker \
  --set proxy.telemetryURL=https://ingest.ai-tally.com/v1/traces
```

Every `proxy.*` value maps onto one `EDGE_PROXY_*` env var from the table above; see
[`values.yaml`](deploy/helm/edge-proxy/values.yaml) for the full set.

## Run

```bash
cd infra/edge-proxy
go run ./cmd/edge-proxy
# then, from the customer side:
#   export OPENAI_BASE_URL=http://localhost:8088/v1
#   export-style header on each request: X-Tenant-Key: tk_live_yourtenant
```

## Test & benchmark

```bash
go test ./...                                   # unit + the p99<3ms budget test
go test -race ./...                             # concurrency safety
go test -bench=ProxyOverhead -benchmem ./internal/proxy/
```

`TestOverheadBudget` runs on every `go test`: it measures p99 added latency against a loopback
upstream and fails if it crosses 3ms, so a hot-path regression (an accidental body buffer, a new
sync allocation) trips CI rather than shipping silently.

### Measured (Apple M-series, loopback, Go 1.26)

```
direct  p50=29µs   p99=62µs
proxied p50=63µs   p99=129µs
added overhead p99 ≈ 100µs            (budget 3ms)
BenchmarkProxyOverhead   ~64µs/op
```

Real deployments add network RTT to the provider on top of this, but that RTT exists with or
without the proxy — the *added* overhead is the proxy's own processing, which is what the budget
governs.

[CTO-40]: https://linear.app/cto-assist/issue/CTO-40
[CTO-41]: https://linear.app/cto-assist/issue/CTO-41
[CTO-42]: https://linear.app/cto-assist/issue/CTO-42
