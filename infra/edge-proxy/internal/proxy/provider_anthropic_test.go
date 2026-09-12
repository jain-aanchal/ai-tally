// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-318: the Anthropic protocol through the proxy.
//
// The CTO-244/245 end-to-end verification drove only the openai and gemini protocols, so
// EDGE_PROXY_PROVIDER=anthropic had never been exercised past the one unit assertion in
// TestOpenAIAndAnthropicMeta. This file drives it end to end and covers the payload variations a
// real Messages API deployment actually produces.
//
// PROVENANCE OF THE FIXTURES, WHICH IS THE POINT. Every file under testdata/anthropic/ is
// transcribed from Anthropic's PUBLISHED Messages API documentation (the response shape on
// /en/api/messages, the SSE event sequence on /en/build-with-claude/streaming, and the error
// envelope on /en/api/errors), not derived from anthropicMeta below. A fixture written by reading
// our own parser can only ever confirm that the parser agrees with itself; one written from the
// provider's published schema can actually disagree with it. Ids, token counts and text are
// illustrative, the field names and nesting are the documented ones.
//
// WHAT THIS STILL DOES NOT PROVE. No request in this file reaches api.anthropic.com. The fixtures
// match the published schema; a real endpoint can still emit a field the docs do not describe, and
// only real traffic with a real key settles that. See the issue for what remains open.

// readFixture loads a recorded provider payload from testdata/.
func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", filepath.FromSlash(name)))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	return b
}

// newAnthropicProxy builds an anthropic-protocol proxy in front of the given upstream. Mirrors
// newGeminiProxy, with EDGE_PROXY_PROVIDER=anthropic so ModifyResponse metadata capture is active.
func newAnthropicProxy(t *testing.T, upstream http.Handler) (*httptest.Server, *recordingSink) {
	t.Helper()
	origin := httptest.NewServer(upstream)
	t.Cleanup(origin.Close)

	cfg, err := config.FromEnv(func(k string) string {
		switch k {
		case "EDGE_PROXY_UPSTREAM":
			return origin.URL
		case "EDGE_PROXY_PROVIDER":
			return "anthropic"
		default:
			return ""
		}
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink)))
	t.Cleanup(front.Close)
	return front, sink
}

// TestAnthropicMetaFromPublishedResponses parses each documented non-streaming response shape.
// The nil cases are the honesty invariant: a payload that carries no usage must leave the counts
// unknown, never 0, because a 0 reads downstream as a real call that cost nothing.
func TestAnthropicMetaFromPublishedResponses(t *testing.T) {
	cases := []struct {
		fixture    string
		wantModel  string
		wantPrompt *int64
		wantComp   *int64
	}{
		// The ordinary completion: both counts present.
		{"anthropic/message_end_turn.json", "claude-opus-5", ptr(10), ptr(15)},
		// stop_reason "tool_use": usage is reported exactly as on a text turn, so a tool-calling
		// turn must meter identically. Nothing about the tool_use content block is retained.
		{"anthropic/message_tool_use.json", "claude-opus-5", ptr(472), ptr(89)},
		// A truncated generation is still fully billed input plus what it managed to emit.
		{"anthropic/message_max_tokens.json", "claude-sonnet-5", ptr(1204), ptr(8)},
		// A refusal (stop_reason "refusal", empty content) bills the input and produces no output.
		// The 0 here is the provider's own reported figure, not a proxy-invented one.
		{"anthropic/message_refusal.json", "claude-opus-5", ptr(331), ptr(0)},
		// Cache hit: input_tokens (21) EXCLUDES the 18923 tokens served from cache, so the billable
		// prompt is the sum. See TestAnthropicCacheTokensFoldIntoPromptTokens below.
		{"anthropic/message_cache_read.json", "claude-opus-5", ptr(18944), ptr(44)},
		// The error envelope carries neither model nor usage. Everything stays unknown.
		{"anthropic/error_overloaded.json", "", nil, nil},
	}
	for _, tc := range cases {
		t.Run(tc.fixture, func(t *testing.T) {
			meta := extractMeta(config.ProviderAnthropic, "/v1/messages", readFixture(t, tc.fixture))
			if meta.Model != tc.wantModel {
				t.Errorf("Model = %q, want %q", meta.Model, tc.wantModel)
			}
			assertTokens(t, "PromptTokens", meta.PromptTokens, tc.wantPrompt)
			assertTokens(t, "CompletionTokens", meta.CompletionTokens, tc.wantComp)
		})
	}
}

// TestAnthropicCacheTokensFoldIntoPromptTokens covers CTO-349's second defect.
//
// Anthropic reports cache_creation_input_tokens and cache_read_input_tokens alongside
// input_tokens, and input_tokens EXCLUDES both. Reporting input_tokens alone therefore under-counts
// a cached prompt by everything the cache served: this fixture's real prompt is 18944 tokens and
// the record used to claim 21. The cache-read share rides along separately, because it is billed at
// a lower rate than fresh input and the price catalog can express exactly that split.
//
// What is still approximate is recorded rather than hidden: cache CREATION tokens are inside the
// total but have no separate field, so they price at the standard input rate instead of the write
// premium. See docs/anthropic-cache-tokens.md.
func TestAnthropicCacheTokensFoldIntoPromptTokens(t *testing.T) {
	meta := extractMeta(config.ProviderAnthropic, "/v1/messages",
		readFixture(t, "anthropic/message_cache_read.json"))
	// 21 fresh + 0 cache writes + 18923 cache reads.
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(18944))
	assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, ptr(18923))
}

// TestAnthropicCacheTokenAbsenceVsZero is the honesty half of the fold, and the distinction the
// pointer counts exist for. A response that omits the cache fields reports the cache share as
// UNKNOWN; one that reports them as 0 reports 0. Collapsing the two would make the proxy assert
// "prompt caching saved this tenant nothing" on every payload that simply did not mention it.
func TestAnthropicCacheTokenAbsenceVsZero(t *testing.T) {
	// message_tool_use.json carries usage with no cache fields at all.
	absent := extractMeta(config.ProviderAnthropic, "/v1/messages",
		readFixture(t, "anthropic/message_tool_use.json"))
	assertTokens(t, "PromptTokens", absent.PromptTokens, ptr(472))
	assertTokens(t, "CachedInputTokens", absent.CachedInputTokens, nil)

	// message_end_turn.json reports both cache buckets as an explicit 0, which is the provider's
	// own figure and is preserved as 0.
	reported := extractMeta(config.ProviderAnthropic, "/v1/messages",
		readFixture(t, "anthropic/message_end_turn.json"))
	assertTokens(t, "PromptTokens", reported.PromptTokens, ptr(10))
	assertTokens(t, "CachedInputTokens", reported.CachedInputTokens, ptr(0))
}

// TestAnthropicProxyRecordsMetadataAndHidesKey is the end-to-end acceptance: a POST /v1/messages
// through an anthropic-protocol proxy relays the body byte-for-byte, records the model and token
// counts, forwards the credential upstream, and keeps that credential out of the TraceRecord.
func TestAnthropicProxyRecordsMetadataAndHidesKey(t *testing.T) {
	const secretKey = "sk-ant-api03-super-secret-key"
	body := readFixture(t, "anthropic/message_end_turn.json")

	var gotAPIKey, gotVersion, gotPath string
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAPIKey = r.Header.Get("x-api-key")
		gotVersion = r.Header.Get("anthropic-version")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	})
	front, sink := newAnthropicProxy(t, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages",
		strings.NewReader(`{"model":"claude-opus-5","max_tokens":1024,"messages":[]}`))
	req.Header.Set("Content-Type", "application/json")
	// Anthropic authenticates on x-api-key, not Authorization: a different header from the openai
	// and gemini paths, and the reason this needs its own end-to-end test.
	req.Header.Set("x-api-key", secretKey)
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	got, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d", resp.StatusCode)
	}
	if string(got) != string(body) {
		t.Errorf("response body altered in transit")
	}
	if gotAPIKey != secretKey {
		t.Errorf("upstream x-api-key = %q, want it forwarded untouched", gotAPIKey)
	}
	if gotVersion != "2023-06-01" {
		t.Errorf("upstream anthropic-version = %q, want it forwarded", gotVersion)
	}
	if gotPath != "/v1/messages" {
		t.Errorf("upstream path = %q", gotPath)
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "claude-opus-5" {
		t.Errorf("trace Model = %q, want claude-opus-5", rec.Model)
	}
	if !tokensEq(rec.PromptTokens, 10) || !tokensEq(rec.CompletionTokens, 15) {
		t.Errorf("trace tokens = %s/%s, want 10/15",
			tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens))
	}
	if rec.StatusCode != http.StatusOK {
		t.Errorf("trace status = %d", rec.StatusCode)
	}
	if s := fmt.Sprintf("%+v", rec); strings.Contains(s, secretKey) || strings.Contains(s, "sk-ant") {
		t.Errorf("trace record leaked the key: %s", s)
	}
}

// TestAnthropicProxyToolUseTurn: a tool-calling turn meters like any other turn, and no part of the
// tool call (its name, its id, its arguments) reaches the record. Tool arguments are user content.
func TestAnthropicProxyToolUseTurn(t *testing.T) {
	body := readFixture(t, "anthropic/message_tool_use.json")
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
	front, sink := newAnthropicProxy(t, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages", strings.NewReader(`{}`))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp.Body.Close()

	sink.waitFor(t, 1)
	rec := sink.last()
	if !tokensEq(rec.PromptTokens, 472) || !tokensEq(rec.CompletionTokens, 89) {
		t.Errorf("trace tokens = %s/%s, want 472/89",
			tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens))
	}
	s := fmt.Sprintf("%+v", rec)
	for _, leak := range []string{"get_weather", "San Francisco", "toolu_"} {
		if strings.Contains(s, leak) {
			t.Errorf("trace record leaked tool content %q: %s", leak, s)
		}
	}
}

// TestAnthropicProxyStreamingSSE drives the published SSE event sequence end to end (CTO-349).
//
// Anthropic splits the usage across events: message_start carries the model and the input tokens,
// message_delta the final cumulative output count, so a parser that reads either one alone gets
// half the answer. The stream must still reach the client byte-for-byte and unbuffered, which is
// asserted here too: the fold happens on the bytes as they pass, never by holding them.
//
// The mid-stream error case is the interesting one for honesty. That stream ends after
// message_start with an error event and no message_delta, so the input count and the 1 output token
// message_start reported are real and are kept, while the final output count never existed and
// stays at what was actually reported rather than being completed with a guess.
func TestAnthropicProxyStreamingSSE(t *testing.T) {
	cases := []struct {
		fixture    string
		wantModel  string
		wantPrompt *int64
		wantComp   *int64
	}{
		// message_start input 25, message_delta cumulative output 15.
		{"anthropic/stream_text.sse", "claude-opus-5", ptr(25), ptr(15)},
		// A tool-use stream meters like any other: input 472, final output 89. The partial_json
		// deltas carrying the tool arguments are content and are never retained.
		{"anthropic/stream_tool_use.sse", "claude-opus-5", ptr(472), ptr(89)},
		// Cut short by an error event: what message_start reported stands, nothing is invented.
		{"anthropic/stream_midstream_error.sse", "claude-opus-5", ptr(25), ptr(1)},
	}
	for _, tc := range cases {
		t.Run(tc.fixture, func(t *testing.T) {
			body := readFixture(t, tc.fixture)
			upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				w.WriteHeader(http.StatusOK)
				// Flush per event so this is a real stream, not one buffered write.
				for _, chunk := range strings.SplitAfter(string(body), "\n\n") {
					_, _ = io.WriteString(w, chunk)
					if f, ok := w.(http.Flusher); ok {
						f.Flush()
					}
				}
			})
			front, sink := newAnthropicProxy(t, upstream)

			req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages",
				strings.NewReader(`{"stream":true}`))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("request: %v", err)
			}
			got, _ := io.ReadAll(resp.Body)
			resp.Body.Close()

			if string(got) != string(body) {
				t.Errorf("SSE stream altered in transit")
			}

			sink.waitFor(t, 1)
			rec := sink.last()
			if rec.StatusCode != http.StatusOK {
				t.Errorf("trace status = %d, want 200 (a mid-stream error is still a 200 on the wire)",
					rec.StatusCode)
			}
			if rec.Model != tc.wantModel {
				t.Errorf("Model = %q, want %q", rec.Model, tc.wantModel)
			}
			assertTokens(t, "PromptTokens", rec.PromptTokens, tc.wantPrompt)
			assertTokens(t, "CompletionTokens", rec.CompletionTokens, tc.wantComp)
			// None of these streams mentions the prompt cache, so the cache share is unknown.
			assertTokens(t, "CachedInputTokens", rec.CachedInputTokens, nil)
			// The fold reads counts only; no fragment of the streamed content may survive on the
			// record (the tool-use stream carries a location argument, the text stream "Hello").
			for _, leak := range []string{"San Francisco", "Hello", "get_weather"} {
				if strings.Contains(fmt.Sprintf("%+v", rec), leak) {
					t.Errorf("trace record leaked streamed content %q", leak)
				}
			}
		})
	}
}

// TestAnthropicStreamClientDisconnect covers the stream that ends early because the CLIENT walked
// away mid-event, not because the provider finished. The proxy reports what the provider managed to
// send before the cut and nothing more: a truncated final event is not parsed at all (a half-read
// JSON object could decode to a plausible wrong number), so the counts from complete earlier events
// stand and the rest is whatever was last reported.
func TestAnthropicStreamClientDisconnect(t *testing.T) {
	full := string(readFixture(t, "anthropic/stream_text.sse"))
	// Cut in the middle of the message_delta event that carries the final output count.
	cut := strings.Index(full, `"stop_reason": "end_turn"`)
	if cut < 0 {
		t.Fatal("fixture no longer contains the message_delta stop_reason")
	}
	truncated := full[:cut]

	var meta responseMeta
	mc := newMetaCapture(io.NopCloser(strings.NewReader(truncated)),
		config.ProviderAnthropic, "/v1/messages", true, &meta)
	if _, err := io.Copy(io.Discard, mc); err != nil {
		t.Fatalf("copy: %v", err)
	}
	if err := mc.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	if meta.Model != "claude-opus-5" {
		t.Errorf("Model = %q, want claude-opus-5", meta.Model)
	}
	// message_start completed, so its counts are real; the final output count never arrived.
	assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(25))
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(1))
}

// TestAnthropicProxyErrorResponse: a 529 error envelope is recorded with its real status and no
// invented usage. A failed call that reports 0 tokens would show up on the dashboard as a real,
// free call rather than as a failure.
func TestAnthropicProxyErrorResponse(t *testing.T) {
	body := readFixture(t, "anthropic/error_overloaded.json")
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(529)
		_, _ = w.Write(body)
	})
	front, sink := newAnthropicProxy(t, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages", strings.NewReader(`{}`))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	if resp.StatusCode != 529 {
		t.Errorf("status = %d, want 529 relayed unchanged", resp.StatusCode)
	}
	if string(got) != string(body) {
		t.Errorf("error body altered in transit")
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.StatusCode != 529 {
		t.Errorf("trace status = %d, want 529", rec.StatusCode)
	}
	if rec.Model != "" {
		t.Errorf("Model = %q, want empty: the error envelope names no model", rec.Model)
	}
	if rec.PromptTokens != nil || rec.CompletionTokens != nil {
		t.Errorf("expected unknown tokens on an error, got %s/%s",
			tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens))
	}
}

// TestAnthropicProviderDefaultUpstream covers CTO-349's third defect: EDGE_PROXY_PROVIDER=anthropic
// with no EDGE_PROXY_UPSTREAM used to resolve to api.openai.com, quietly pointing a customer's
// Anthropic traffic at the wrong vendor. Each provider now names its own origin.
func TestAnthropicProviderDefaultUpstream(t *testing.T) {
	cfg, err := config.FromEnv(func(k string) string {
		if k == "EDGE_PROXY_PROVIDER" {
			return "anthropic"
		}
		return ""
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	if got := cfg.Upstream.String(); got != config.DefaultAnthropicUpstream {
		t.Errorf("default upstream for provider=anthropic = %q, want %q", got,
			config.DefaultAnthropicUpstream)
	}
}

// TestProviderDefaultUpstreamIsExplicit checks the whole mapping, because the bug was not really
// "anthropic is missing" but "anything that is not gemini silently becomes OpenAI". An explicit
// EDGE_PROXY_UPSTREAM still wins everywhere.
func TestProviderDefaultUpstreamIsExplicit(t *testing.T) {
	cases := []struct{ provider, want string }{
		{"", config.DefaultUpstream},
		{"openai", config.DefaultUpstream},
		{"anthropic", config.DefaultAnthropicUpstream},
		{"gemini", config.DefaultGeminiUpstream},
	}
	for _, tc := range cases {
		t.Run("default/"+tc.provider, func(t *testing.T) {
			cfg, err := config.FromEnv(func(k string) string {
				if k == "EDGE_PROXY_PROVIDER" {
					return tc.provider
				}
				return ""
			})
			if err != nil {
				t.Fatalf("config: %v", err)
			}
			if got := cfg.Upstream.String(); got != tc.want {
				t.Errorf("provider %q default upstream = %q, want %q", tc.provider, got, tc.want)
			}
		})
		t.Run("explicit/"+tc.provider, func(t *testing.T) {
			cfg, err := config.FromEnv(func(k string) string {
				switch k {
				case "EDGE_PROXY_PROVIDER":
					return tc.provider
				case "EDGE_PROXY_UPSTREAM":
					return "https://llm.internal.example"
				}
				return ""
			})
			if err != nil {
				t.Fatalf("config: %v", err)
			}
			if got := cfg.Upstream.String(); got != "https://llm.internal.example" {
				t.Errorf("explicit upstream for provider %q = %q", tc.provider, got)
			}
		})
	}
}

// ptr is a test helper for the nullable token counts; see responseMeta on why they are pointers.
func ptr(v int64) *int64 { return &v }

// assertTokens compares a nullable count against a nullable expectation, so "unknown" is a value
// the table can express rather than something a test has to spell out case by case.
func assertTokens(t *testing.T, field string, got, want *int64) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s = %s, want unknown", field, tokensStr(got))
	case want != nil && !tokensEq(got, *want):
		t.Errorf("%s = %s, want %d", field, tokensStr(got), *want)
	}
}
