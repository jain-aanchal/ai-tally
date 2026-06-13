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

Every added file/line is marked with the comment `// ai-tally:` (or `# ai-tally:`
where the file isn't TS/JS). To audit: `grep -rn "ai-tally:" examples/vercel-chatbot/app`.

### `app/lib/tally.ts` (new file)

A small helper module — POSTs spans and CDP events to the ai-tally gateway,
classifies prompts into feature tags (`chatbot.support` / `chatbot.brainstorm`
/ `chatbot.code`), and pins the model to `gpt-5-mini` on the outbound batch
so the gateway's seed price catalog can compute cost authoritatively. The real
provider/model travel on long-tail attributes (`chatbot.real_provider`,
`chatbot.real_model`). See **gateway-side workaround** below.

### `app/app/api/demo-chat/route.ts` (new file)

A no-auth, no-DB chat endpoint used by the traffic driver. Accepts a
`{sessionId, prompt, provider, turnIndex, dryRun?}` body, calls OpenAI or
Anthropic over plain `fetch`, then fire-and-forgets a span (and, on turn 5,
a `session_engaged` event) into the gateway. After the 5th message the route
also emits the engagement signal so the attribution dashboard can show
$/engaged-session alongside $/conversion.

### `app/app/api/demo-event/route.ts` (new file)

Thin convenience route for CDP events: thumbs-up (`positive_feedback`) and
post-session `conversion`. The driver posts here; a real UI thumbs-up button
would do the same.

### Why separate routes vs. patching upstream `(chat)/api/chat/route.ts`

The upstream route is ~400 lines and is wired hard to NextAuth, Drizzle,
Vercel Blob, BotID, and a resumable-stream context — none of which is
relevant to a 50-session demo driven from a script. Stripping all of that to
make the upstream route runnable in standalone mode would require touching
≈20 files and a custom DB shim; the value of the demo is the **gateway-side
shape** (spans + events), not the chatbot UX itself. So instead of patching
the upstream route, we add a dedicated `/api/demo-chat` route that emits the
exact same `gen_ai.*` and `chatbot.*` attributes the upstream route would,
and the driver hits that one. The upstream route is left untouched so
refreshing the vendor copy is a clean re-clone.

If/when someone wants the UI to also flow into the gateway, the patch is two
lines: `import { postSpan } from "@/lib/tally"` and `void postSpan({...})`
after `streamText` finalizes in `app/(chat)/api/chat/route.ts`.

### Auth / DB

We do not modify `app/(auth)/auth.ts` or the middleware. The demo driver
never enters the auth flow because it hits `/api/demo-chat` directly. A
human visiting `:3001` will be redirected to `/login` as upstream intends —
the demo is the cost-and-attribution pipeline, not the chat UI.

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
handful of paid services. We do **not** depend on those for the demo —
`run.sh` boots the chatbot with `next dev --turbo`, the driver hits the
ai-tally-added `/api/demo-chat` route directly, and the gateway-side
infrastructure does the actual work.

`next build` is therefore not part of the demo's acceptance check. The added
files (`lib/tally.ts`, `api/demo-chat`, `api/demo-event`) are TypeScript
that the Next.js compiler picks up automatically — `tsc --noEmit` against the
chatbot's own `tsconfig.json` is a faster sanity check.
