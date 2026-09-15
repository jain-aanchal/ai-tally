# Generated docs artifacts

These files are generated from the code and rendered by the developer docs at
https://ai-tally.com/docs (CTO-371). Do not edit them by hand. Each has a test
that fails when the committed copy is stale, so CI tells you when to regenerate.

| File | Generated from | Regenerate |
|---|---|---|
| `public-openapi.json` | the gateway app, filtered to `POST /v1/batches`, `POST /v1/otlp/traces`, `GET /v1/tenant/hmac-key` (`infra/gateway/src/gateway/public_openapi.py`) | `cd infra/gateway && uv run --extra dev python scripts/export_public_openapi.py` |
| `connect-snippets.json` | `web/lib/connectSnippets.ts`, with the placeholder key `YOUR_TALLY_KEY` and the hosted ingest host (`web/lib/connectSnippetsExport.ts`) | `cd web && UPDATE_DOCS_ARTIFACTS=1 npx vitest run lib/connectSnippetsExport.test.ts` |
| `sdk-reference.json` | the public functions in `sdk/python/src/tally` (`sdk/python/scripts/sdk_reference.py`) | `cd sdk/python && uv run --extra dev python scripts/sdk_reference.py` |

## How the docs site picks them up

`.github/workflows/docs-sync.yml` runs on merges to `main` that touch any of the
sources. It checks the committed files are current, uploads them as the
`docs-public-api` workflow artifact, and sends a `repository_dispatch`
(`ai-tally-docs-sync`, payload `{sha, run_id}`) to `jain-aanchal/ai-tally-website`.
That repo fetches the files at the SHA and opens a PR updating
`docs/src/generated/`.

The dispatch needs a `DOCS_SYNC_TOKEN` repository secret: a fine-grained token
with Contents write on `ai-tally-website`. Without it the dispatch step is
skipped with a notice and the workflow still passes.
