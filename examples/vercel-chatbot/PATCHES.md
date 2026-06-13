# Vercel AI Chatbot — vendored copy

This directory vendors the [Vercel AI Chatbot](https://github.com/vercel/ai-chatbot)
template at a pinned upstream SHA. We patch a small number of files to wire
each `/api/chat` completion (and a couple of UI signals) into the local ai-tally
gateway so the chatbot becomes a live demo for workflows 2/3/4.

## Pinned upstream

- **Repo:** https://github.com/vercel/ai-chatbot
- **SHA:** `2becdb4a56e7683ae08aef927cec1c6c52dfad5e`
- **Title at SHA:** *fix(chat): drop mistral models and harden title generation (#1498)*
- **Vendored:** `examples/vercel-chatbot/app/`
- **Removed before commit:** `.git/` (so the template is a normal directory, not a
  submodule).

## Refreshing the vendor

```bash
# from repo root
rm -rf examples/vercel-chatbot/app
git clone https://github.com/vercel/ai-chatbot examples/vercel-chatbot/app
cd examples/vercel-chatbot/app && git checkout <new-sha> && rm -rf .git
```

Then re-apply the patches below (every changed line is marked `// ai-tally:`
so they are grep-able) and bump the SHA in this file.

## Patches applied (Commit 2)

Each patched line is marked with the comment `// ai-tally:` (or `# ai-tally:`
where the file isn't TS/JS). To audit: `grep -rn "ai-tally:" examples/vercel-chatbot/app`.

### `app/lib/tally.ts` (new file)

A small helper module — POSTs spans and CDP events to the ai-tally gateway,
classifies prompts into feature tags (`chatbot.support` / `chatbot.brainstorm`
/ `chatbot.code`), and pins the model to `gpt-5-mini` on the outbound batch
so the gateway's seed price catalog can compute cost authoritatively. The real
provider/model travel on long-tail attributes (`chatbot.real_provider`,
`chatbot.real_model`). See **gateway-side workaround** below.

### `app/(chat)/api/chat/route.ts`

After each completion succeeds, POST a span to the gateway with the prompt's
feature tag, token usage, and the gpt-5-mini-pinned cost. Lines added:
roughly 4 (an `import` and a `void postSpan(...)` call after the LLM call
finalizes). All marked `// ai-tally:`.

### `app/(chat)/api/vote/route.ts`

When a user upvotes a message, POST a `positive_feedback` CDP event. Same
pattern: 2 lines, both marked `// ai-tally:`.

### Auth disabled

The upstream template hard-requires NextAuth + Postgres. For the demo we run
without auth — `app/(auth)/auth.ts` is patched to return a stable synthetic
session object, and the middleware that would redirect to `/login` is gated
behind `TALLY_DEMO_DISABLE_AUTH=1`. Every patched line is marked `// ai-tally:`.

## Gateway-side workaround (CTO-104 carryover)

The gateway's seed price catalog (`sdk/python/src/tally/pricing.py`) currently
knows only `gpt-5-mini`, `gpt-5`, and `text-embedding-3-small`. Anything else
→ catalog miss → `EstimatedCost` drops to $0 in ClickHouse. CTO-104 worked
around this by pinning emitted spans to `gpt-5-mini` and back-computing the
tokens from the real provider's cost. We do the same here:

- `gen_ai.system` → `"openai"`
- `gen_ai.request.model` / `gen_ai.response.model` → `"gpt-5-mini"`
- real provider+model → `chatbot.real_provider` / `chatbot.real_model`
  (long-tail string attributes, persisted into `SpanAttributes` map)

This drops out cleanly when [CTO-106](https://linear.app/cto-assist/issue/CTO-106)
lands a real per-provider catalog.

## Buildability

The upstream template depends on Postgres, Vercel Blob, NextAuth, Redis and a
handful of paid services. We patch enough of it to run `next dev` against the
local gateway with `TALLY_DEMO_STANDALONE=1` (no DB, no Blob, no NextAuth),
which is what `run.sh` uses. The full `next build` against the unpatched
template would require those backing services, so the `make chatbot-demo`
target uses `next dev` (Turbopack) instead of `next start`.
