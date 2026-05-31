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

`/healthz` is the one path the proxy owns (liveness); everything else is forwarded.

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
