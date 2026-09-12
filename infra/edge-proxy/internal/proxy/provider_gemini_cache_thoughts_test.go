// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// TestGeminiCachedContentIsASubsetOfThePrompt covers CTO-375.
//
// Gemini's promptTokenCount already contains the context-cache share: the API reference says it "is
// still the total effective prompt size meaning this includes the number of tokens in the cached
// content". So this is the OpenAI shape, not the Anthropic one, and the fix is to record the share
// without touching the total.
//
// Before the fix CachedInputTokens was always nil on Gemini, so compute_cost_micro_usd billed all
// 100200 tokens at the standard input rate instead of 200 standard + 100000 cached. Gemini's cached
// rate is a fraction of input, so the dashboard over-charged a cached call several times over: the
// opposite direction from the Anthropic bug in CTO-349, and just as confidently wrong.
func TestGeminiCachedContentIsASubsetOfThePrompt(t *testing.T) {
	meta := extractMeta(config.ProviderGemini, "/v1beta/models/gemini-2.5-pro:generateContent",
		readFixture(t, "gemini/response_cached_context.json"))
	// The total is NOT 100200+100000. Adding the cached share would be the Anthropic mistake.
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(100200))
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, ptr(100000))
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(31))
}

// TestGeminiCachedAbsenceVsZero is the honesty half, matching the Anthropic case in
// provider_anthropic_test.go. A response that never mentions the cache reports the share as UNKNOWN;
// one reporting 0 reports 0. Collapsing them would assert "context caching saved this tenant
// nothing" on every payload that simply did not mention it.
func TestGeminiCachedAbsenceVsZero(t *testing.T) {
	// response_prompt_blocked.json carries usageMetadata with no cache field at all.
	absent := extractMeta(config.ProviderGemini, "/v1beta/models/gemini-2.5-flash:generateContent",
		readFixture(t, "gemini/response_prompt_blocked.json"))
	assertTokens(t, "CachedInputTokens", absent.CachedInputTokens, nil)

	// response_max_tokens.json reports an explicit 0, which is a measurement.
	zero := extractMeta(config.ProviderGemini, "/v1beta/models/gemini-2.5-flash:generateContent",
		readFixture(t, "gemini/response_max_tokens.json"))
	assertTokens(t, "CachedInputTokens", zero.CachedInputTokens, ptr(0))
}

// TestGeminiThinkingTokensFoldIntoOutput covers CTO-376 on the streamed path, which is where
// reasoning-heavy calls actually arrive.
//
// The fixture's trailing chunk reports 8000 thought tokens against 150 visible ones. Before the fix
// the proxy recorded 150, so a call whose real output cost was 8150 tokens priced at under 2% of it.
func TestGeminiThinkingTokensFoldIntoOutput(t *testing.T) {
	meta := extractMeta(config.ProviderGemini, "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
		readFixture(t, "gemini/stream_thinking.sse"))
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(310))
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(8150))
}

// TestGeminiCompletionGuardsAgainstDoubleCounting is the reason geminiCompletion is not a two-term
// sum, and the case a docs-only reading of this API would have missed.
//
// The reference defines totalTokenCount as prompt+thoughts+candidates, which makes candidates and
// thoughts disjoint, and the captured response_max_tokens.json fixture confirms it arithmetically
// (1204+96+8 = 1308). But the convention is not uniform in the field: Vertex AI excludes thinking
// tokens from candidatesTokenCount while the Generative Language API has been observed including
// them. On a payload where they are already folded in, a blind sum would invent tokens and
// over-bill a reasoning-heavy call by the whole thinking share.
//
// total-prompt is thoughts+candidates under either convention, so it is the authority whenever the
// provider supplied a total.
func TestGeminiCompletionGuardsAgainstDoubleCounting(t *testing.T) {
	// candidates(8150) already contains thoughts(8000): total-prompt says the output is 8150, not
	// the 16150 a blind sum would report.
	already := geminiCompletion(ptr(8150), ptr(8000), ptr(8460), ptr(310))
	assertTokens(t, "CompletionTokens", already, ptr(8150))

	// The disjoint convention: the sum agrees with total-prompt, so the sum stands.
	disjoint := geminiCompletion(ptr(150), ptr(8000), ptr(8460), ptr(310))
	assertTokens(t, "CompletionTokens", disjoint, ptr(8150))
}

// TestGeminiCompletionWithoutATotal pins the fallback. Some models omit totalTokenCount, and some
// omit thoughtsTokenCount while still charging for thinking; neither absence may produce a zero.
func TestGeminiCompletionWithoutATotal(t *testing.T) {
	// No total to cross-check against: the sum is the best available answer.
	assertTokens(t, "CompletionTokens", geminiCompletion(ptr(150), ptr(8000), nil, ptr(310)), ptr(8150))
	// No thinking reported at all: unchanged from the pre-fix behaviour.
	assertTokens(t, "CompletionTokens", geminiCompletion(ptr(150), nil, ptr(460), ptr(310)), ptr(150))
	// Nothing reported: unknown, never 0. A blocked prompt was billed for input and produced no
	// output, and "0 output tokens" would claim the model answered for free.
	assertTokens(t, "CompletionTokens", geminiCompletion(nil, nil, ptr(42), ptr(42)), nil)
	// Thinking reported with no candidates yet (an early streamed chunk, or a call cut off during
	// thinking): the thought tokens were still billed.
	assertTokens(t, "CompletionTokens", geminiCompletion(nil, ptr(2048), nil, nil), ptr(2048))
}

// TestGeminiStreamedCacheShareSurvivesEveryChunk: usageMetadata repeats on every chunk, so the
// cached share is reported many times over and the fold must not lose or double it.
func TestGeminiStreamedCacheShareSurvivesEveryChunk(t *testing.T) {
	meta := scanSSE(config.ProviderGemini, readFixture(t, "gemini/stream_thinking.sse"))
	// The fixture reports no cache field, so the share stays unknown rather than becoming 0.
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, nil)
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(8150))
}
