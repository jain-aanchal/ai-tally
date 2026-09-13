# Verifying the edge proxy against real provider traffic (CTO-350)

The edge proxy's usage parsing (OpenAI, Anthropic, Gemini; streamed and not) has only been tested
against fixtures built from published schemas. Issue #350 closes when a human runs real traffic
through the proxy and checks what it recorded against each provider's own usage dashboard. This
page is that procedure. It needs your own API keys and costs a few cents.

## Why not `make chatbot-demo-realistic`

The issue named it as the vehicle, but it cannot do this job:

- The chatbot's `/api/demo-chat` route calls `api.openai.com` and `api.anthropic.com` directly.
  Its traffic never passes through the edge proxy, so it verifies nothing about the proxy.
- Its `--max-usd` cap is checked after each session, against a blended driver-side estimate
  ($1.50 in / $8.00 out per MTok), with parallel workers. It can overrun, and the default cap is
  $10 for about 5000 sessions.
- It needs the full stack, Postgres, pnpm, and both the OpenAI and Anthropic keys, and it has no
  Gemini, prompt-caching or thinking coverage.

So the verification lives in a dedicated harness, `infra/edge-proxy/cmd/verify-real-traffic`.

## What the harness does

It starts the real proxy handler in-process (configured through `config.FromEnv`, exactly like the
binary) and sends a fixed matrix of 14 tiny calls through it:

| Provider | Calls |
|---|---|
| OpenAI (`gpt-4o-mini`) | non-streaming; streaming with `include_usage`; a ~9k-token prompt sent twice so the second reads the prompt cache |
| Anthropic (`claude-haiku-4-5`) | non-streaming; streaming; a cache write (non-streaming) then a cache read (streaming) on a ~9k-token system prompt |
| Gemini (`gemini-2.5-flash`) | non-streaming; streaming; thinking (budget 512) non-streaming and streaming; a ~9k-token prompt sent twice for implicit caching |

For each call it reads the usage block the provider returned to the client (the proxy forwards
bodies byte-for-byte, so this is the provider's own report), decodes it with a parser separate from
the proxy's, derives what the `TraceRecord` should hold from each provider's documented semantics,
and compares prompt, completion and cached counts field by field. Unknown is `nil` on both sides and
`nil` against a number is a mismatch.

Keys are read from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` only. A provider with no
key is skipped and the output says so. Gemini's key is sent in the `x-goog-api-key` header, never in
the URL.

## Run it

Preview the plan and the worst-case spend first. This sends nothing and needs no keys:

```bash
cd infra/edge-proxy
go run ./cmd/verify-real-traffic --dry-run
```

Then the real run, capturing fixtures:

```bash
cd infra/edge-proxy
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
go run ./cmd/verify-real-traffic --max-usd 0.50 --capture
```

or from `infra/`: `make verify-real-traffic ARGS=--capture`.

Useful flags: `--only anthropic,gemini` to run a subset, `--openai-model gpt-5-mini` if a default
model has been retired (the model must be in the harness price table, which mirrors
`sdk/python/src/tally/pricing.py` and is test-checked against it).

Exit codes: `0` everything matched, `1` a mismatch or provider error, `2` usage error (no keys,
unpriced model), `3` the spend cap refused a call (incomplete run).

## Cost

- **Worst-case bound: $0.11** for the full matrix at the default models. The cap bounds each
  call before sending it: prompt tokens at most the request's byte length (every tokenizer emits
  at least one byte per token), output at most `max_tokens` plus any thinking budget, Anthropic
  cache writes at the 1.25x premium.
- **Expected actual spend: about 3 cents**, dominated by the Anthropic cache write.
- `--max-usd` defaults to `$0.50`. Before every call the harness checks
  `spent so far + this call's worst case` against the cap and refuses rather than overrun. Rates
  are the seed catalog's, which are marked unverified there, so treat the provider dashboard as
  the bill.

## What to do with the output

1. **Every row must say `MATCH`.** A `MISMATCH` names the field, both values and the delta. That is
   the finding #350 exists to surface: open a bug against the parser in
   `infra/edge-proxy/internal/proxy` with the row and the `raw:` line for that call.
2. **Read `COVERAGE GAPS`.** A cache pair that did not hit, or a thinking call that reported no
   thought tokens, did not exercise that path. Rerun (caches are timing dependent; Gemini implicit
   caching is best-effort) until each path has been seen at least once.
3. **Read `NOTES`.** A `gemini convention:` note records whether the live Generative Language API
   folds thinking tokens into `candidatesTokenCount`. Paste it into #350; it settles the open
   question in `docs/anthropic-cache-tokens.md`.
4. **Diff against each provider's usage dashboard.** This is the part no fixture can do. The
   `CHECK AGAINST PROVIDER DASHBOARDS` section prints the UTC time window, per-provider token totals
   in the provider's own field names, and each call's request id:
   - OpenAI: Usage page, filtered to the window and model. Compare input, cached input and output
     tokens.
   - Anthropic: Console Usage, filtered to the window and model. Compare input, cache write, cache
     read and output tokens.
   - Google: AI Studio usage or the Cloud console for the key's project. Compare input and output
     tokens (Google bills thinking tokens as output).
   Dashboards lag by minutes to hours and round, so look for agreement to within a handful of
   tokens, not byte equality. Any larger gap is a finding even if every row matched, because it
   means provider and proxy agree with each other but not with the bill.
5. **Commit the captured fixtures.** `--capture` writes
   `internal/proxy/testdata/<provider>/real_<call>.{json,sse}` with all content stripped (an
   allowlist keeps only ids, model, finish reasons and usage), plus a `real_<call>.expected.json`
   sidecar. Review a couple of files to confirm no prompt or completion text is present, then
   commit them. `TestCapturedRealTrafficFixtures` turns each one into a regression test. Once the
   Gemini `real_thinking_stream.sse` and `real_cache_read.json` captures exist, the hand-written
   `stream_thinking.sse` and `response_cached_context.json` can be retired in favour of them.
   Do not commit a capture whose row was a `MISMATCH` until the parser is fixed; its test fails by
   design.
6. **Close #350** with the table, the dashboard comparison, and the fixture commit linked.
