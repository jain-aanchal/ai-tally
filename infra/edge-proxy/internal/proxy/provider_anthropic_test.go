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
		// Cache hit: see TestAnthropicCacheTokensAreNotFoldedIntoPromptTokens below.
		{"anthropic/message_cache_read.json", "claude-opus-5", ptr(21), ptr(44)},
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

// TestAnthropicCacheTokensAreNotFoldedIntoPromptTokens pins a real fidelity limit rather than
// asserting it away. Anthropic reports cache_read_input_tokens and cache_creation_input_tokens
// alongside input_tokens, and input_tokens EXCLUDES them, so PromptTokens on a cache hit is the
// uncached prompt only. That is honest (it is the field the provider labels input tokens) but it
// is not the whole billable input, and pricing a cached call from this record alone under-reports
// it. Recording the cache counts is a schema change beyond this proxy, so the test exists to make
// the gap visible instead of letting a future reader assume the number is complete.
func TestAnthropicCacheTokensAreNotFoldedIntoPromptTokens(t *testing.T) {
	meta := extractMeta(config.ProviderAnthropic, "/v1/messages",
		readFixture(t, "anthropic/message_cache_read.json"))
	if !tokensEq(meta.PromptTokens, 21) {
		t.Fatalf("PromptTokens = %s, want 21 (input_tokens only)", tokensStr(meta.PromptTokens))
	}
	// If this ever starts reporting 18944 (21 + 18923), the proxy learned about cache tokens and
	// this test should be replaced by one that asserts the new, richer record.
	if tokensEq(meta.PromptTokens, 18944) {
		t.Errorf("cache tokens are now folded into PromptTokens; update this test and the pricing path")
	}
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

// TestAnthropicProxyStreamingSSE drives the published SSE event sequence.
//
// Two things are asserted, and one gap is recorded honestly. The stream is relayed byte-for-byte,
// and the metadata stays UNKNOWN rather than being invented: anthropicMeta json.Unmarshals the
// whole body, an SSE stream is not one JSON document, so the decode fails and the record carries
// nils. That is the correct failure direction (a nil is a NULL downstream; a 0 would be a lie), but
// it does mean a streamed Anthropic call is currently unattributed to a model, even though the
// model and both token counts are right there in the message_start and message_delta events. The
// gemini path avoids this only because it can recover the model from the request URL, and the
// Anthropic path has no such fallback. Parsing SSE is a change to provider.go, which this
// test-only change deliberately does not make; see the issue.
func TestAnthropicProxyStreamingSSE(t *testing.T) {
	for _, name := range []string{
		"anthropic/stream_text.sse",
		"anthropic/stream_tool_use.sse",
		"anthropic/stream_midstream_error.sse",
	} {
		t.Run(name, func(t *testing.T) {
			body := readFixture(t, name)
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
			// The gap, pinned: unknown, and specifically not fabricated.
			if rec.PromptTokens != nil || rec.CompletionTokens != nil {
				t.Errorf("streamed usage is not parsed today; expected unknown counts, got %s/%s",
					tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens))
			}
			if rec.Model != "" {
				t.Errorf("Model = %q; if the SSE parser landed, replace this test with one that "+
					"asserts the model and the cumulative message_delta usage", rec.Model)
			}
		})
	}
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

// TestAnthropicProviderDefaultUpstream records today's behavior, which is a wart found while
// closing this issue: only gemini has a provider-specific default upstream, so
// EDGE_PROXY_PROVIDER=anthropic with no EDGE_PROXY_UPSTREAM resolves to api.openai.com and
// every request 404s against a protocol it was never meant for. Deployments always set the
// upstream explicitly, so nothing is broken in practice, but the default is wrong. The assertion
// is written so that adding a DefaultAnthropicUpstream fails here and the fix is a deliberate,
// reviewed change rather than a silent one.
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
	if got := cfg.Upstream.String(); got != config.DefaultUpstream {
		t.Errorf("default upstream for provider=anthropic = %q, want %q; if an Anthropic-specific "+
			"default was added, update this test to assert it", got, config.DefaultUpstream)
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
