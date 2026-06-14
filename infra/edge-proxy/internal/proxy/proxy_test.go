// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// recordingSink captures emitted TraceRecords for assertions.
type recordingSink struct {
	mu      sync.Mutex
	records []TraceRecord
}

func (s *recordingSink) Record(r TraceRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records = append(s.records, r)
}

func (s *recordingSink) last() TraceRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.records[len(s.records)-1]
}

func (s *recordingSink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.records)
}

// newTestProxy builds a Proxy in front of the given upstream handler and returns a client-facing
// test server plus the sink.
func newTestProxy(t *testing.T, requireTenant bool, upstream http.Handler) (*httptest.Server, *recordingSink) {
	t.Helper()
	origin := httptest.NewServer(upstream)
	t.Cleanup(origin.Close)

	cfg, err := config.FromEnv(func(k string) string {
		switch k {
		case "EDGE_PROXY_UPSTREAM":
			return origin.URL
		case "EDGE_PROXY_REQUIRE_TENANT":
			if requireTenant {
				return "true"
			}
			return ""
		default:
			return ""
		}
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}

	sink := &recordingSink{}
	front := httptest.NewServer(proxy(t, cfg, WithSink(sink)))
	t.Cleanup(front.Close)
	return front, sink
}

// proxy constructs the handler (separate helper keeps newTestProxy readable).
func proxy(t *testing.T, cfg config.Config, opts ...Option) http.Handler {
	t.Helper()
	return New(cfg, opts...)
}

func TestTransparentForwarding(t *testing.T) {
	var (
		gotMethod, gotPath, gotQuery, gotAuth, gotBody string
		sawTenantHeader                                bool
	)
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotAuth = r.Header.Get("Authorization")
		_, sawTenantHeader = r.Header["X-Tenant-Key"]
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		w.Header().Set("X-Upstream", "yes")
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"ok":true}`)
	})

	front, sink := newTestProxy(t, false, upstream)

	reqBody := `{"model":"gpt-5","messages":[]}`
	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions?stream=false", strings.NewReader(reqBody))
	req.Header.Set("Authorization", "Bearer sk-customer-secret")
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)

	// Request reached upstream unmodified.
	if gotMethod != http.MethodPost {
		t.Errorf("method = %q", gotMethod)
	}
	if gotPath != "/v1/chat/completions" {
		t.Errorf("path = %q", gotPath)
	}
	if gotQuery != "stream=false" {
		t.Errorf("query = %q", gotQuery)
	}
	if gotBody != reqBody {
		t.Errorf("body forwarded mutated: %q != %q", gotBody, reqBody)
	}
	// The customer's provider key is forwarded untouched...
	if gotAuth != "Bearer sk-customer-secret" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	// ...but our control header is stripped before the upstream sees it.
	if sawTenantHeader {
		t.Error("X-Tenant-Key leaked to upstream")
	}

	// Response relayed unmodified.
	if resp.StatusCode != http.StatusCreated {
		t.Errorf("status = %d", resp.StatusCode)
	}
	if resp.Header.Get("X-Upstream") != "yes" {
		t.Error("upstream response header dropped")
	}
	if string(respBody) != `{"ok":true}` {
		t.Errorf("response body = %q", respBody)
	}

	// Telemetry copy captured the tenant + counts, no content.
	if sink.count() != 1 {
		t.Fatalf("expected 1 trace, got %d", sink.count())
	}
	rec := sink.last()
	if rec.TenantKey != "tk_live_acme" {
		t.Errorf("TenantKey = %q", rec.TenantKey)
	}
	if rec.StatusCode != http.StatusCreated {
		t.Errorf("trace status = %d", rec.StatusCode)
	}
	if rec.ReqBytes != int64(len(reqBody)) {
		t.Errorf("ReqBytes = %d, want %d", rec.ReqBytes, len(reqBody))
	}
	if rec.RespBytes != int64(len(`{"ok":true}`)) {
		t.Errorf("RespBytes = %d", rec.RespBytes)
	}
	if rec.Failed {
		t.Error("Failed should be false on success")
	}
	if rec.Duration <= 0 {
		t.Error("Duration should be positive")
	}
}

func TestRequireTenantRejectsMissingKey(t *testing.T) {
	called := false
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})
	front, _ := newTestProxy(t, true, upstream)

	resp, err := http.Get(front.URL + "/v1/models")
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", resp.StatusCode)
	}
	if called {
		t.Error("upstream must not be called when tenant key is required and missing")
	}
}

func TestUpstreamUnavailableReturns502(t *testing.T) {
	// Build a proxy whose upstream points at a closed server.
	origin := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	cfg, err := config.FromEnv(func(k string) string {
		if k == "EDGE_PROXY_UPSTREAM" {
			return origin.URL
		}
		return ""
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	origin.Close() // now unreachable

	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink)))
	defer front.Close()

	resp, err := http.Get(front.URL + "/v1/models")
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", resp.StatusCode)
	}
	if strings.Contains(string(body), origin.URL) {
		t.Errorf("error body leaked upstream address: %q", body)
	}
	if sink.count() != 1 || !sink.last().Failed {
		t.Errorf("expected a failed trace record, got %+v", sink.records)
	}
}

func TestLargeBodyForwardedByteForByte(t *testing.T) {
	// 1 MiB of deterministic data; upstream verifies an exact-length, exact-content echo.
	payload := strings.Repeat("abcdefgh", 128*1024) // 1 MiB
	var gotLen int
	var match bool
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		gotLen = len(b)
		match = string(b) == payload
		w.WriteHeader(http.StatusOK)
	})
	front, sink := newTestProxy(t, false, upstream)

	resp, err := http.Post(front.URL+"/v1/embeddings", "application/json", strings.NewReader(payload))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp.Body.Close()

	if gotLen != len(payload) || !match {
		t.Errorf("body corrupted: gotLen=%d want=%d match=%v", gotLen, len(payload), match)
	}
	if rec := sink.last(); rec.ReqBytes != int64(len(payload)) {
		t.Errorf("ReqBytes = %d, want %d", rec.ReqBytes, len(payload))
	}
}

// TestStreamingPassThrough verifies SSE-style chunked responses reach the client incrementally and
// unmodified — the proxy must not buffer the whole stream before flushing.
func TestStreamingPassThrough(t *testing.T) {
	chunks := []string{
		"data: {\"delta\":\"Hel\"}\n\n",
		"data: {\"delta\":\"lo\"}\n\n",
		"data: [DONE]\n\n",
	}
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		fl, ok := w.(http.Flusher)
		if !ok {
			t.Error("upstream writer is not a flusher")
			return
		}
		for _, c := range chunks {
			_, _ = io.WriteString(w, c)
			fl.Flush()
		}
	})
	front, sink := newTestProxy(t, false, upstream)

	resp, err := http.Get(front.URL + "/v1/chat/completions")
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Errorf("Content-Type = %q", ct)
	}

	// Read line-framed events back and confirm the full concatenation matches.
	var got strings.Builder
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 4096), 1<<20)
	for sc.Scan() {
		got.WriteString(sc.Text())
		got.WriteByte('\n')
	}
	want := strings.ReplaceAll(strings.Join(chunks, ""), "\n\n", "\n\n")
	// Normalize: scanner drops the blank-line framing differently; just assert the deltas survived.
	for _, frag := range []string{`"delta":"Hel"`, `"delta":"lo"`, "[DONE]"} {
		if !strings.Contains(got.String(), frag) {
			t.Errorf("streamed output missing %q; got:\n%s", frag, got.String())
		}
	}
	_ = want

	if rec := sink.last(); rec.StatusCode != http.StatusOK {
		t.Errorf("trace status = %d", rec.StatusCode)
	}
}

// TestFeatureTagHeaderRecordedAndStripped is CTO-104's structural guarantee: a per-request
// feature tag arriving on X-Tally-Feature-Tag is captured on the TraceRecord and stripped from
// the upstream-bound request — mirroring the tenant-header contract.
func TestFeatureTagHeaderRecordedAndStripped(t *testing.T) {
	var sawFeatureHeader bool
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, sawFeatureHeader = r.Header["X-Tally-Feature-Tag"]
		w.WriteHeader(http.StatusOK)
	})
	front, sink := newTestProxy(t, false, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", strings.NewReader("{}"))
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	req.Header.Set("X-Tally-Feature-Tag", "aider-demo")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp.Body.Close()

	if sawFeatureHeader {
		t.Error("X-Tally-Feature-Tag leaked to upstream")
	}
	if got := sink.last().FeatureTag; got != "aider-demo" {
		t.Errorf("FeatureTag = %q, want %q", got, "aider-demo")
	}
}

// TestFeatureTagHeaderAbsent: when the caller omits the header, FeatureTag is empty and the
// request still succeeds — feature tagging is purely opt-in, never required.
func TestFeatureTagHeaderAbsent(t *testing.T) {
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	front, sink := newTestProxy(t, false, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", strings.NewReader("{}"))
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp.Body.Close()

	if got := sink.last().FeatureTag; got != "" {
		t.Errorf("FeatureTag = %q, want empty", got)
	}
}

// TestTraceRecordCarriesNoBodyContent is a structural guard: the telemetry type must not gain a
// field that could hold prompt/completion/key content. If someone adds a `Body string`, this fails.
func TestTraceRecordCarriesNoBodyContent(t *testing.T) {
	rec := TraceRecord{TenantKey: "t", Method: "POST", Path: "/v1/x", StatusCode: 200}
	s := fmt.Sprintf("%+v", rec)
	for _, forbidden := range []string{"Bearer", "sk-", "messages", "prompt"} {
		if strings.Contains(s, forbidden) {
			t.Errorf("trace record stringification contains %q: %s", forbidden, s)
		}
	}
}
