// SPDX-License-Identifier: Apache-2.0
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// coverage names the code path a call exists to exercise, so the report can say plainly when a run
// did not actually reach it (a cache miss, a model that chose not to think). A green table over a path
// that never ran would be the fixture problem again, one level up.
type coverage int

const (
	coverPlain coverage = iota
	coverCacheRead
	coverThoughts
)

// callSpec is one request in the fixed matrix.
type callSpec struct {
	// Name is unique within a provider and becomes the captured fixture's file name.
	Name     string
	Provider config.Provider
	Stream   bool
	Model    string
	// Path is the upstream path (and query), without the proxy's /<provider> routing prefix.
	Path string
	Body []byte
	// MaxOutput is the most output tokens the call can bill: max output plus any thinking budget.
	MaxOutput int64
	// CacheWrite marks a call whose prompt may bill at Anthropic's cache-write premium.
	CacheWrite bool
	Cover      coverage
}

func (s callSpec) mode() string {
	if s.Stream {
		return "stream"
	}
	return "json"
}

// tinyPrompt keeps the plain calls to a handful of tokens each way. The point is the usage block, not
// the answer.
const tinyPrompt = "Reply with the single word: ok"

// thinkingPrompt is small but needs a step of reasoning, so a thinking model has a reason to spend
// thought tokens instead of answering from the surface.
const thinkingPrompt = "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. " +
	"How much does the ball cost? Answer in one short sentence."

// prefixBytes sizes the cacheable prefix. Prompt caching only engages above a per-model minimum:
// 1024 tokens for OpenAI and Gemini 2.5 Flash implicit caching, and 4096 for Claude Haiku 4.5, below
// which Anthropic silently writes nothing. Numeric rows tokenize at roughly 3 bytes a token, so 28 KB
// is about 9000 tokens: clear of every minimum with margin, and still cents under the byte-length
// bound the spend cap uses.
const prefixBytes = 28_000

// cacheablePrefix is deterministic, because a prefix that differs by one byte never reads from cache.
// It is synthetic filler, never captured: fixtures keep usage metadata only.
func cacheablePrefix() string {
	var b strings.Builder
	b.WriteString("You are a terse assistant. The reference table below is fixed test material for a " +
		"prompt-cache check. Do not discuss it.\n")
	for i := 1; b.Len() < prefixBytes; i++ {
		fmt.Fprintf(&b, "Row %d: bin %d holds %d units of part %d, reorder at %d, audited in week %d.\n",
			i, i%97, i*7%1000, i*13%5000, i*3%400, i%52+1)
	}
	return b.String()
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err) // static request shapes; a failure here is a programming error
	}
	return b
}

// buildMatrix returns the fixed call matrix for the enabled providers, in execution order. Cache
// pairs are adjacent so the second call lands well inside the provider's cache TTL.
func buildMatrix(models map[config.Provider]string, enabled map[config.Provider]bool) []callSpec {
	var out []callSpec
	prefix := cacheablePrefix()

	if enabled[config.ProviderOpenAI] {
		m := models[config.ProviderOpenAI]
		plain := map[string]any{
			"model":                 m,
			"messages":              []any{map[string]any{"role": "user", "content": tinyPrompt}},
			"max_completion_tokens": 64,
		}
		stream := map[string]any{
			"model":                 m,
			"messages":              []any{map[string]any{"role": "user", "content": tinyPrompt}},
			"max_completion_tokens": 64,
			"stream":                true,
			// Without this opt-in OpenAI puts usage on no chunk at all, and the proxy correctly records
			// nil. The harness sets it because it is measuring the parser, not the opt-in.
			"stream_options": map[string]any{"include_usage": true},
		}
		cached := map[string]any{
			"model": m,
			"messages": []any{
				map[string]any{"role": "system", "content": prefix},
				map[string]any{"role": "user", "content": tinyPrompt},
			},
			"max_completion_tokens": 64,
		}
		const path = "/v1/chat/completions"
		out = append(out,
			callSpec{Name: "plain", Provider: config.ProviderOpenAI, Model: m, Path: path, Body: mustJSON(plain), MaxOutput: 64},
			callSpec{Name: "stream", Provider: config.ProviderOpenAI, Stream: true, Model: m, Path: path, Body: mustJSON(stream), MaxOutput: 64},
			callSpec{Name: "cache_warm", Provider: config.ProviderOpenAI, Model: m, Path: path, Body: mustJSON(cached), MaxOutput: 64},
			callSpec{Name: "cache_read", Provider: config.ProviderOpenAI, Model: m, Path: path, Body: mustJSON(cached), MaxOutput: 64, Cover: coverCacheRead},
		)
	}

	if enabled[config.ProviderAnthropic] {
		m := models[config.ProviderAnthropic]
		msgs := []any{map[string]any{"role": "user", "content": tinyPrompt}}
		plain := map[string]any{"model": m, "max_tokens": 64, "messages": msgs}
		stream := map[string]any{"model": m, "max_tokens": 64, "messages": msgs, "stream": true}
		system := []any{map[string]any{
			"type": "text", "text": prefix, "cache_control": map[string]any{"type": "ephemeral"},
		}}
		cacheWrite := map[string]any{"model": m, "max_tokens": 64, "system": system, "messages": msgs}
		// The read half streams on purpose: on a stream the cache buckets arrive on message_start and
		// the output count on message_delta, which is the fold CTO-349 changed.
		cacheRead := map[string]any{"model": m, "max_tokens": 64, "system": system, "messages": msgs, "stream": true}
		const path = "/v1/messages"
		out = append(out,
			callSpec{Name: "plain", Provider: config.ProviderAnthropic, Model: m, Path: path, Body: mustJSON(plain), MaxOutput: 64},
			callSpec{Name: "stream", Provider: config.ProviderAnthropic, Stream: true, Model: m, Path: path, Body: mustJSON(stream), MaxOutput: 64},
			callSpec{Name: "cache_write", Provider: config.ProviderAnthropic, Model: m, Path: path, Body: mustJSON(cacheWrite), MaxOutput: 64, CacheWrite: true},
			callSpec{Name: "cache_read_stream", Provider: config.ProviderAnthropic, Stream: true, Model: m, Path: path, Body: mustJSON(cacheRead), MaxOutput: 64, CacheWrite: true, Cover: coverCacheRead},
		)
	}

	if enabled[config.ProviderGemini] {
		m := models[config.ProviderGemini]
		gen := func(maxOut, budget int) map[string]any {
			return map[string]any{
				"maxOutputTokens": maxOut,
				"thinkingConfig":  map[string]any{"thinkingBudget": budget},
			}
		}
		user := func(text string) []any {
			return []any{map[string]any{"role": "user", "parts": []any{map[string]any{"text": text}}}}
		}
		// Thinking budget 0 on the plain calls keeps them plain; 2.5 Flash thinks by default otherwise.
		plain := map[string]any{"contents": user(tinyPrompt), "generationConfig": gen(64, 0)}
		thinking := map[string]any{"contents": user(thinkingPrompt), "generationConfig": gen(1024, 512)}
		cached := map[string]any{"contents": user(prefix + "\n" + tinyPrompt), "generationConfig": gen(64, 0)}
		gen64 := "/v1beta/models/" + m + ":generateContent"
		stream := "/v1beta/models/" + m + ":streamGenerateContent?alt=sse"
		out = append(out,
			callSpec{Name: "plain", Provider: config.ProviderGemini, Model: m, Path: gen64, Body: mustJSON(plain), MaxOutput: 64},
			callSpec{Name: "stream", Provider: config.ProviderGemini, Stream: true, Model: m, Path: stream, Body: mustJSON(plain), MaxOutput: 64},
			// The two thinking calls replace the hand-written stream_thinking.sse once captured.
			callSpec{Name: "thinking", Provider: config.ProviderGemini, Model: m, Path: gen64, Body: mustJSON(thinking), MaxOutput: 1024 + 512, Cover: coverThoughts},
			callSpec{Name: "thinking_stream", Provider: config.ProviderGemini, Stream: true, Model: m, Path: stream, Body: mustJSON(thinking), MaxOutput: 1024 + 512, Cover: coverThoughts},
			// Implicit caching is best-effort on Google's side, so a miss is reported as a coverage gap,
			// not a failure. A hit replaces the hand-written response_cached_context.json.
			callSpec{Name: "cache_warm", Provider: config.ProviderGemini, Model: m, Path: gen64, Body: mustJSON(cached), MaxOutput: 64},
			callSpec{Name: "cache_read", Provider: config.ProviderGemini, Model: m, Path: gen64, Body: mustJSON(cached), MaxOutput: 64, Cover: coverCacheRead},
		)
	}
	return out
}

// setAuth attaches the provider credential. Gemini's key goes in x-goog-api-key rather than ?key= so
// the key never appears in a URL, an error string, or a captured request path.
func setAuth(req *http.Request, p config.Provider, key string) {
	req.Header.Set("Content-Type", "application/json")
	switch p {
	case config.ProviderOpenAI:
		req.Header.Set("Authorization", "Bearer "+key)
	case config.ProviderAnthropic:
		req.Header.Set("x-api-key", key)
		req.Header.Set("anthropic-version", "2023-06-01")
	case config.ProviderGemini:
		req.Header.Set("x-goog-api-key", key)
	}
}

// requestIDHeader is the response header each provider puts its request id in. Gemini has none, so
// its body responseId stands in.
func requestIDHeader(p config.Provider) string {
	switch p {
	case config.ProviderOpenAI:
		return "x-request-id"
	case config.ProviderAnthropic:
		return "request-id"
	}
	return ""
}
