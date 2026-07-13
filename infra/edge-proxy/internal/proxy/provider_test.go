// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// recordedGeminiResponse is a real-shape generateContent response body (content elided): the parts
// text is irrelevant to metadata — only usageMetadata and, via the path, the model matter.
const recordedGeminiResponse = `{
  "candidates": [
    {"content": {"parts": [{"text": "ok"}], "role": "model"}, "finishReason": "STOP"}
  ],
  "usageMetadata": {
    "promptTokenCount": 259,
    "candidatesTokenCount": 73,
    "totalTokenCount": 332
  },
  "modelVersion": "gemini-2.5-flash-002"
}`

// TestGeminiMetaFromRecordedResponse is the CTO-167 acceptance test: the proxy parses a recorded
// Gemini response into a correct set of TraceRecord scalars — model (from the request path) plus
// prompt/candidate token counts (from usageMetadata).
func TestGeminiMetaFromRecordedResponse(t *testing.T) {
	path := "/v1beta/models/gemini-2.5-flash:generateContent"
	meta := extractMeta(config.ProviderGemini, path, []byte(recordedGeminiResponse))

	if meta.Model != "gemini-2.5-flash" {
		t.Errorf("Model = %q, want gemini-2.5-flash", meta.Model)
	}
	if meta.PromptTokens != 259 {
		t.Errorf("PromptTokens = %d, want 259", meta.PromptTokens)
	}
	if meta.CompletionTokens != 73 {
		t.Errorf("CompletionTokens = %d, want 73", meta.CompletionTokens)
	}
}

func TestGeminiModelFromPath(t *testing.T) {
	cases := map[string]string{
		"/v1beta/models/gemini-2.5-flash:generateContent":       "gemini-2.5-flash",
		"/v1beta/models/gemini-1.5-pro:streamGenerateContent":   "gemini-1.5-pro",
		"/v1beta/models/gemini-2.5-flash-002:generateContent":   "gemini-2.5-flash-002",
		"/v1/chat/completions":                                  "", // not a gemini path
		"/v1beta/models/gemini-2.5-flash:generateContent?extra": "gemini-2.5-flash", // ':' precedes '?'
	}
	for path, want := range cases {
		if got := geminiModelFromPath(path); got != want {
			t.Errorf("geminiModelFromPath(%q) = %q, want %q", path, got, want)
		}
	}
}

// TestGeminiMetaFallsBackToModelVersion: if the path lacks a model (unexpected shape), the body's
// modelVersion is used so the record is still attributable.
func TestGeminiMetaFallsBackToModelVersion(t *testing.T) {
	meta := extractMeta(config.ProviderGemini, "/weird/path", []byte(recordedGeminiResponse))
	if meta.Model != "gemini-2.5-flash-002" {
		t.Errorf("Model = %q, want gemini-2.5-flash-002 (modelVersion fallback)", meta.Model)
	}
}

func TestOpenAIAndAnthropicMeta(t *testing.T) {
	openai := extractMeta(config.ProviderOpenAI, "/v1/chat/completions",
		[]byte(`{"model":"gpt-4o-2024-08-06","usage":{"prompt_tokens":12,"completion_tokens":34}}`))
	if openai.Model != "gpt-4o-2024-08-06" || openai.PromptTokens != 12 || openai.CompletionTokens != 34 {
		t.Errorf("openai meta = %+v", openai)
	}

	anthropic := extractMeta(config.ProviderAnthropic, "/v1/messages",
		[]byte(`{"model":"claude-sonnet-4-5","usage":{"input_tokens":100,"output_tokens":200}}`))
	if anthropic.Model != "claude-sonnet-4-5" || anthropic.PromptTokens != 100 || anthropic.CompletionTokens != 200 {
		t.Errorf("anthropic meta = %+v", anthropic)
	}
}

// newGeminiProxy builds a gemini-protocol proxy in front of the given upstream. Mirrors
// newTestProxy but sets EDGE_PROXY_PROVIDER=gemini so ModifyResponse metadata capture is active.
func newGeminiProxy(t *testing.T, upstream http.Handler) (*httptest.Server, *recordingSink) {
	t.Helper()
	origin := httptest.NewServer(upstream)
	t.Cleanup(origin.Close)

	cfg, err := config.FromEnv(func(k string) string {
		switch k {
		case "EDGE_PROXY_UPSTREAM":
			return origin.URL
		case "EDGE_PROXY_PROVIDER":
			return "gemini"
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

// TestGeminiProxyRecordsMetadataAndHidesKey drives an end-to-end Gemini request through the proxy:
//   - the recorded response is relayed byte-for-byte to the client,
//   - the TraceRecord carries the model + token counts + status parsed from it, and
//   - the ?key= credential is forwarded to the upstream but never lands in the TraceRecord.
func TestGeminiProxyRecordsMetadataAndHidesKey(t *testing.T) {
	const secretKey = "AIzaSy-super-secret-key"
	var gotKeyQuery, gotGoogHeader string
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotKeyQuery = r.URL.Query().Get("key")
		gotGoogHeader = r.Header.Get("x-goog-api-key")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, recordedGeminiResponse)
	})
	front, sink := newGeminiProxy(t, upstream)

	url := front.URL + "/v1beta/models/gemini-2.5-flash:generateContent?key=" + secretKey
	req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader(`{"contents":[]}`))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	// Response relayed unmodified.
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d", resp.StatusCode)
	}
	if string(body) != recordedGeminiResponse {
		t.Errorf("response body altered")
	}
	// The ?key= credential rode through to the upstream untouched.
	if gotKeyQuery != secretKey {
		t.Errorf("upstream ?key = %q, want it forwarded", gotKeyQuery)
	}
	_ = gotGoogHeader

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "gemini-2.5-flash" {
		t.Errorf("trace Model = %q", rec.Model)
	}
	if rec.PromptTokens != 259 || rec.CompletionTokens != 73 {
		t.Errorf("trace tokens = %d/%d, want 259/73", rec.PromptTokens, rec.CompletionTokens)
	}
	if rec.StatusCode != http.StatusOK {
		t.Errorf("trace status = %d", rec.StatusCode)
	}
	// The key must not leak into the recorded metadata: not in Path, not anywhere in the record.
	if strings.Contains(rec.Path, secretKey) {
		t.Errorf("trace Path leaked the key: %q", rec.Path)
	}
	if s := fmt.Sprintf("%+v", rec); strings.Contains(s, secretKey) || strings.Contains(s, "AIzaSy") {
		t.Errorf("trace record leaked the key: %s", s)
	}
}

// TestGeminiProxyStreamingModelWithoutUsage: a streamed response whose usage we can't buffer past
// the cap still yields the path-derived model (best-effort metadata), and never blocks streaming.
func TestGeminiProxyStreamingModelFromPath(t *testing.T) {
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		// No usageMetadata in this chunk — exercises the "model without tokens" path.
		_, _ = io.WriteString(w, `[{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}]`)
	})
	front, sink := newGeminiProxy(t, upstream)

	req, _ := http.NewRequest(http.MethodPost,
		front.URL+"/v1beta/models/gemini-1.5-pro:streamGenerateContent?key=k", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp.Body.Close()

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "gemini-1.5-pro" {
		t.Errorf("trace Model = %q, want gemini-1.5-pro", rec.Model)
	}
	if rec.PromptTokens != 0 || rec.CompletionTokens != 0 {
		t.Errorf("expected zero tokens when usage absent, got %d/%d", rec.PromptTokens, rec.CompletionTokens)
	}
}
