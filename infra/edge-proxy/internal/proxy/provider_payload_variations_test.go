// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-318: OpenAI and Gemini payload variations, from the providers' PUBLISHED schemas.
//
// The CTO-244/245 verification drove a fake upstream whose OpenAI and Gemini bodies were hand-built
// by reading provider.go. That is the actual weakness the issue records: a fixture derived from our
// own parser can only confirm the parser agrees with itself. The fixtures under testdata/openai/
// and testdata/gemini/ are instead transcribed from OpenAI's published OpenAPI specification
// (openai/openai-openapi, the CreateChatCompletionResponse / chunk examples and the CompletionUsage
// schema) and Google's published GenerateContentResponse reference. They cover the variations that
// realistically differ from the happy path: truncation, refusals, tool calls, a response with no
// usage block at all, a safety-blocked prompt, streaming, and each provider's error envelope.
//
// This narrows the gap. It does not close it: no request here touches a real endpoint, and only
// real traffic against a real key can prove a provider does not emit something its own docs omit.

func TestOpenAIMetaFromPublishedResponses(t *testing.T) {
	cases := []struct {
		fixture    string
		wantModel  string
		wantPrompt *int64
		wantComp   *int64
	}{
		// The documented happy path. The nested *_tokens_details objects the parser ignores must
		// not disturb the two counts it does read.
		{"openai/completion_stop.json", "gpt-4o-2024-08-06", ptr(19), ptr(10)},
		// finish_reason "length": a truncated answer is still billed for what it produced.
		{"openai/completion_length.json", "gpt-4o-2024-08-06", ptr(1204), ptr(8)},
		// A refusal arrives as a normal 200 with message.refusal set. It costs real tokens and must
		// meter like any other turn.
		{"openai/completion_refusal.json", "gpt-4o-2024-08-06", ptr(331), ptr(12)},
		// finish_reason "tool_calls" with NO usage object: the model is known, the counts are not.
		// Unknown must stay unknown; a 0 would price this real call at nothing.
		{"openai/completion_tool_calls_no_usage.json", "gpt-4o-2024-08-06", nil, nil},
		// The error envelope has no top-level model and no usage.
		{"openai/error_rate_limit.json", "", nil, nil},
	}
	for _, tc := range cases {
		t.Run(tc.fixture, func(t *testing.T) {
			meta := extractMeta(config.ProviderOpenAI, "/v1/chat/completions", readFixture(t, tc.fixture))
			if meta.Model != tc.wantModel {
				t.Errorf("Model = %q, want %q", meta.Model, tc.wantModel)
			}
			assertTokens(t, "PromptTokens", meta.PromptTokens, tc.wantPrompt)
			assertTokens(t, "CompletionTokens", meta.CompletionTokens, tc.wantComp)
		})
	}
}

// TestOpenAIStreamedUsage covers CTO-349 on the OpenAI protocol. With
// stream_options.include_usage the final chunk (the one whose choices array is empty) carries the
// whole usage block, and every chunk carries the model, so a streamed call now meters exactly like
// a buffered one.
func TestOpenAIStreamedUsage(t *testing.T) {
	meta := extractMeta(config.ProviderOpenAI, "/v1/chat/completions",
		readFixture(t, "openai/stream_with_usage.sse"))
	if meta.Model != "gpt-4o-2024-08-06" {
		t.Errorf("Model = %q, want gpt-4o-2024-08-06", meta.Model)
	}
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(19))
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(10))
	// The fixture's usage block has no prompt_tokens_details, so the cached share is unknown.
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, nil)
}

// TestOpenAIStreamedWithoutUsageOptIn is the case the proxy cannot fix and must not paper over.
//
// OpenAI emits the usage chunk only when the request set stream_options.include_usage. Without it
// the counts are on no chunk at all, so they stay unknown: the proxy does not estimate them from
// the streamed text, and deliberately does not inject the opt-in into the customer's request body,
// which it forwards byte-for-byte. The model is still recovered from the chunks, so the span is
// attributable to a model and reads as an unpriced call rather than a free one. The fix for those
// spans is for the caller to set stream_options.include_usage.
func TestOpenAIStreamedWithoutUsageOptIn(t *testing.T) {
	meta := extractMeta(config.ProviderOpenAI, "/v1/chat/completions",
		readFixture(t, "openai/stream_no_usage.sse"))
	if meta.Model != "gpt-4o-2024-08-06" {
		t.Errorf("Model = %q, want gpt-4o-2024-08-06", meta.Model)
	}
	assertTokens(t, "PromptTokens", meta.PromptTokens, nil)
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, nil)
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, nil)
}

// TestOpenAIStreamedCachedPromptTokens reads prompt_tokens_details.cached_tokens off the streamed
// usage block. OpenAI's prompt_tokens INCLUDES the cached share (unlike Anthropic's input_tokens),
// so the cached count is reported as the subset it is and nothing is added to the prompt total.
func TestOpenAIStreamedCachedPromptTokens(t *testing.T) {
	meta := extractMeta(config.ProviderOpenAI, "/v1/chat/completions",
		readFixture(t, "openai/stream_cached_prompt.sse"))
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(2048))
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(12))
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, ptr(1920))
}

func TestGeminiMetaFromPublishedResponses(t *testing.T) {
	cases := []struct {
		name       string
		fixture    string
		path       string
		wantModel  string
		wantPrompt *int64
		wantComp   *int64
	}{
		{
			// finishReason MAX_TOKENS, plus the thinking-model thoughtsTokenCount the parser does
			// not read. candidatesTokenCount excludes thoughts, so 8 is the visible output only and
			// the 96 thinking tokens are billed but unrecorded. Pinned here so the omission is
			// visible rather than assumed away.
			name: "max_tokens_with_thoughts", fixture: "gemini/response_max_tokens.json",
			path:      "/v1beta/models/gemini-2.5-flash:generateContent",
			wantModel: "gemini-2.5-flash", wantPrompt: ptr(1204), wantComp: ptr(8),
		},
		{
			// A safety-blocked prompt returns no candidates and a usageMetadata carrying only
			// promptTokenCount. The input was billed; there is no output count to report, and
			// reporting 0 would claim the model answered for free.
			name: "prompt_blocked", fixture: "gemini/response_prompt_blocked.json",
			path:      "/v1beta/models/gemini-2.5-flash:generateContent",
			wantModel: "gemini-2.5-flash", wantPrompt: ptr(42), wantComp: nil,
		},
		{
			// An error body names no model, but the path still does, so the failed call stays
			// attributable to a model with both counts unknown.
			name: "error_envelope", fixture: "gemini/error_resource_exhausted.json",
			path:      "/v1beta/models/gemini-1.5-pro:generateContent",
			wantModel: "gemini-1.5-pro", wantPrompt: nil, wantComp: nil,
		},
		{
			// alt=sse streaming (CTO-349): usageMetadata repeats on every chunk with the running
			// totals, so the counts come from the last chunk that reported each one. The early
			// chunks carry promptTokenCount with no candidatesTokenCount yet, and that absence must
			// not overwrite the final count nor read as an output of 0.
			name: "streaming_sse", fixture: "gemini/stream_generate_content.sse",
			path:      "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
			wantModel: "gemini-2.5-flash", wantPrompt: ptr(25), wantComp: ptr(15),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			meta := extractMeta(config.ProviderGemini, tc.path, readFixture(t, tc.fixture))
			if meta.Model != tc.wantModel {
				t.Errorf("Model = %q, want %q", meta.Model, tc.wantModel)
			}
			assertTokens(t, "PromptTokens", meta.PromptTokens, tc.wantPrompt)
			assertTokens(t, "CompletionTokens", meta.CompletionTokens, tc.wantComp)
		})
	}
}
