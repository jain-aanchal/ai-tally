# make aider-demo

A one-command live demo of ai-tally: [Aider](https://aider.chat) edits a small
Python fixture, talking to OpenAI (or Anthropic) **through the ai-tally edge
proxy** with a per-request `X-Tally-Feature-Tag: aider-demo` header. When all
three tasks finish, the dashboard auto-opens, pre-filtered to that tag.

Goal: a stranger who just finished `make demo` can run `make aider-demo` and
see real agent traces in under five minutes.

## Prereqs

1. The local stack is up:
   ```
   cd infra && make up && make seed
   ```
2. `aider-chat` on `$PATH`:
   ```
   pip install aider-chat
   ```
3. A provider API key in the environment:
   ```
   export OPENAI_API_KEY=sk-...
   # or, for the cross-provider variant:
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

## Run it

```
cd infra && make aider-demo
# cross-provider variant:
cd infra && PROVIDER=anthropic make aider-demo
```

## What you'll see

```
▶ starting edge-proxy on :7070 → https://api.openai.com
▶ task 1/3: Make the failing tests in test_string_utils.py pass…
   [12s · 4 turns · $0.018]
▶ task 2/3: Refactor string_utils.py to share a single private…
   [18s · 5 turns · $0.024]
▶ task 3/3: Add a new function most_common_word(s: str) -> str | None…
   [15s · 4 turns · $0.021]

─── make aider-demo: done ────────────────────────────────────
  3 tasks · 13 turns · $0.0630 total

  Workflow 1 (agents):  http://localhost:3000/agents?tag=aider-demo
    drill into tail run: http://localhost:3000/agents?tag=aider-demo&run=…
  Workflow 2 (compare): http://localhost:3000/compare?tag=aider-demo
  Workflow 3 (cost):    http://localhost:3000/cost?tag=aider-demo

Opening Workflow 1…
```

Workflow 1 lands you on the agent runs table, filtered to the `aider-demo`
tag. Click the tail run to see the per-step span tree.

## How it works

```
aider --message-file 01-fix-test.txt …
   │
   ▼  (OPENAI_API_BASE=http://localhost:7070/v1, header: X-Tally-Feature-Tag)
edge-proxy (Go)  ──▶  api.openai.com
   │
   ▼ (TraceRecord — metadata only, no body)
   (CTO-40/41: bridge to otel_spans — not wired yet)

run.sh also POSTs a feature-tagged batch directly to the gateway between tasks
(parsed cost + turn count from Aider's stdout) so the dashboard has rows the
deep links can filter on. When the proxy→ClickHouse bridge lands this
side-channel goes away.
```

## Reset

The fixture in `target-repo/` is restored between tasks by `reset.sh`, which
just does `git checkout HEAD -- target-repo && git clean -fd`. After the demo
itself you can also run:

```
cd infra && make aider-demo-stop   # kills the proxy via PID file
```

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `ERROR: ai-tally gateway not reachable` | `cd infra && make up && make seed` first |
| `ERROR: OPENAI_API_KEY not set` | `export OPENAI_API_KEY=…` or pass `PROVIDER=anthropic` |
| `ERROR: aider not on PATH` | `pip install aider-chat` |
| Edge-proxy didn't bind | Check `/tmp/ai-tally-aider-edge-proxy.log` |
| Dashboard shows "Synthetic preview" | The gateway batch failed — check `make logs` |

## What good looks like

- Three tasks complete in under three minutes total
- `Workflow 1` shows three agent runs tagged `aider-demo`, each with multi-step
  span tree
- `Workflow 3` shows `aider-demo` as a single-feature column in the cost
  breakdown

## Screenshots

Screenshots of the three workflows go in `screenshots/`. They aren't
committed unless they've been generated against a real run (we don't ship
mock-looking pictures of a live demo).
