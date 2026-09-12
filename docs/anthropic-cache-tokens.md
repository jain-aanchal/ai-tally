# Anthropic prompt-cache tokens: what we record, and what we still cannot price (CTO-349)

Written while fixing the edge proxy's streamed-usage parsing. It records a decision and a known
limit, so the next person reading `anthropicPrompt` does not have to reconstruct either.

## The three buckets

Anthropic's Messages API reports a prompt in three separately billed pieces:

| Field | What it is | Billed at |
|---|---|---|
| `usage.input_tokens` | fresh prompt tokens, neither written to nor read from cache | standard input rate |
| `usage.cache_creation_input_tokens` | tokens written into the prompt cache | a premium over the input rate |
| `usage.cache_read_input_tokens` | tokens served from the cache | a fraction of the input rate |

The trap is that `input_tokens` **excludes** the other two. It is not a total. A request that reads
18923 tokens from cache and sends 21 fresh ones reports `input_tokens: 21`, and the proxy used to
record 21 as the whole prompt: a call with an 18944-token prompt showing up as a rounding error.

## What the proxy records now

- `TraceRecord.PromptTokens` is the sum of all three buckets, which is the honest token count for
  the call.
- `TraceRecord.CachedInputTokens` carries the `cache_read_input_tokens` share and ships as
  `gen_ai.usage.cached_input_tokens`. The gateway and the price catalog already read that attribute
  as a **subset** of the input tokens: they bill `(input - cached)` at the standard rate and
  `cached` at the cached rate (`sdk/python/src/tally/pricing.py`). So the split prices correctly
  without any schema change.
- Both stay `nil` when the provider did not report them. A response that never mentions the cache
  reports the cache share as unknown; one that reports `0` reports `0`. Those are different facts
  and the pointer types keep them apart.

## What we still cannot represent

**Cache creation tokens have nowhere to go.** There is no `gen_ai.usage.cache_creation_input_tokens`
attribute, no `PriceType.CACHE_WRITE` in the catalog, and no column in `otel_spans`. Two options
were on the table:

1. Leave them out of `PromptTokens`. The call is then under-counted by 100% of the cache write,
   and a cache-warming request (which is mostly cache writes) reads as nearly free.
2. Include them in `PromptTokens`. The token count is then right, and the write share prices at the
   standard input rate instead of the write premium, so the call is under-priced by the premium on
   that slice only.

We do (2), because it is wrong by a rate multiplier on one slice rather than wrong by the whole
slice, and because the token count itself, which is what the record claims to be, is then accurate.

This is an approximation, not an unknown, so it is not a violation of the honest-under-uncertainty
invariant: no number is fabricated and no blank is filled in with a guess. It is a fidelity limit,
and it is written down here rather than left for someone to find in a variance report.

**Closing it properly** needs, in this order: a `gen_ai.usage.cache_creation_input_tokens` span
attribute in `tally.schema`, a nullable column on `otel_spans`, a `CACHE_WRITE` price type with the
per-model rates, and then a one-line change in `anthropicPrompt` to stop folding the bucket in. Until
all four exist, adding a field to `TraceRecord` alone would only move the loss to a value nothing
reads.

## Other providers

- **OpenAI** reports `usage.prompt_tokens_details.cached_tokens`, and its `prompt_tokens` already
  **includes** the cached share. The proxy reads it straight into `CachedInputTokens` and adds
  nothing to the total. OpenAI does not bill a separate cache-write rate, so there is no equivalent
  gap.
- **Gemini** reports `usageMetadata.cachedContentTokenCount` for context caching. The proxy does not
  read it yet; a Gemini call using an explicit cached context therefore prices its cached tokens at
  the standard input rate. It is the same shape of fix as the OpenAI one and is not covered here
  because CTO-349 had no Gemini cache fixture to verify the semantics against.
