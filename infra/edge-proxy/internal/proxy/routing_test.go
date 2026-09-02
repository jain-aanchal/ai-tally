// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// mustURL parses a URL or fails the test.
func mustURL(t *testing.T, raw string) *url.URL {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatalf("parse %q: %v", raw, err)
	}
	return u
}

// staticResolver is a fixed key-hash -> tenant map for proxy tests. Present keys resolve with a
// write scope so they pass the proxy's ingest scope gate; scope-specific behavior is exercised by
// scopeResolver.
type staticResolver map[string]string

func (s staticResolver) Resolve(keyHash string) (string, string, bool) {
	id, ok := s[keyHash]
	return id, "write", ok
}

// scopeResolver resolves a single key hash to a fixed tenant and scope, for exercising the proxy's
// read/write/admin ingest scope gate.
type scopeResolver struct {
	keyHash string
	tenant  string
	scope   string
}

func (s scopeResolver) Resolve(keyHash string) (string, string, bool) {
	if keyHash == s.keyHash {
		return s.tenant, s.scope, true
	}
	return "", "", false
}

// TestHostRoutingSelectsOrigin verifies host-based routing forwards each hostname to its own
// upstream, byte-for-byte, and tags the trace with that route's provider metadata.
func TestHostRoutingSelectsOrigin(t *testing.T) {
	// atomic: written in the upstream handler goroutines, read in the test goroutine (go test -race).
	var openaiHit, anthropicHit atomic.Bool
	openai := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		openaiHit.Store(true)
		_, _ = io.WriteString(w, `{"model":"gpt-5","usage":{"prompt_tokens":11,"completion_tokens":7}}`)
	}))
	defer openai.Close()
	anthropic := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		anthropicHit.Store(true)
		_, _ = io.WriteString(w, `{"model":"claude","usage":{"input_tokens":3,"output_tokens":9}}`)
	}))
	defer anthropic.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModeHost,
		Routes: []config.Route{
			{Match: "openai.proxy.test", Upstream: mustURL(t, openai.URL), Provider: config.ProviderOpenAI},
			{Match: "anthropic.proxy.test", Upstream: mustURL(t, anthropic.URL), Provider: config.ProviderAnthropic},
		},
	}
	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink)))
	defer front.Close()

	// Route to OpenAI by Host header.
	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", strings.NewReader(`{}`))
	req.Host = "openai.proxy.test"
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("openai request: %v", err)
	}
	_ = resp.Body.Close()
	if !openaiHit.Load() || anthropicHit.Load() {
		t.Fatalf("host routing wrong: openaiHit=%v anthropicHit=%v", openaiHit.Load(), anthropicHit.Load())
	}
	sink.waitFor(t, 1)
	if got := sink.last(); got.Model != "gpt-5" || got.PromptTokens != 11 || got.CompletionTokens != 7 {
		t.Errorf("openai route trace = %+v, want gpt-5 11/7", got)
	}

	// Route to Anthropic by Host header; provider metadata comes from the Anthropic extractor.
	openaiHit.Store(false)
	anthropicHit.Store(false)
	req2, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages", strings.NewReader(`{}`))
	req2.Host = "anthropic.proxy.test"
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatalf("anthropic request: %v", err)
	}
	_ = resp2.Body.Close()
	if openaiHit.Load() || !anthropicHit.Load() {
		t.Fatalf("host routing wrong on 2nd: openaiHit=%v anthropicHit=%v", openaiHit.Load(), anthropicHit.Load())
	}
	sink.waitFor(t, 2)
	if got := sink.last(); got.Model != "claude" || got.PromptTokens != 3 || got.CompletionTokens != 9 {
		t.Errorf("anthropic route trace = %+v, want claude 3/9", got)
	}
}

// TestHostRoutingNoMatchReturns404 confirms a configured route table that matches nothing fails
// rather than forwarding to a wrong origin.
func TestHostRoutingNoMatchReturns404(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("upstream must not be reached for an unmatched host")
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModeHost,
		Routes:       []config.Route{{Match: "openai.proxy.test", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderOpenAI}},
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	req, _ := http.NewRequest(http.MethodGet, front.URL+"/v1/models", nil)
	req.Host = "unknown.proxy.test"
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("status = %d, want 404", resp.StatusCode)
	}
}

// TestPathRoutingStripsPrefix verifies path-based routing selects the origin by leading prefix and
// strips that prefix before forwarding, so the upstream sees its own API path.
func TestPathRoutingStripsPrefix(t *testing.T) {
	var gotPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = io.WriteString(w, `{"model":"gpt-5","usage":{"prompt_tokens":1,"completion_tokens":2}}`)
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModePath,
		Routes:       []config.Route{{Match: "/openai", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderOpenAI}},
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	resp, err := http.Post(front.URL+"/openai/v1/chat/completions", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()
	if gotPath != "/v1/chat/completions" {
		t.Errorf("upstream path = %q, want /v1/chat/completions (prefix stripped)", gotPath)
	}
}

// TestPathRoutingPreservesEncodedSegment verifies that stripping the routing prefix keeps a
// percent-encoded path segment escaped byte-for-byte, so the upstream receives /v1/foo%2Fbar (not
// the re-encoded /v1/foo/bar). This is the byte-for-byte forwarding invariant on the path-mode hot
// path, matching host mode and single-origin.
func TestPathRoutingPreservesEncodedSegment(t *testing.T) {
	var gotEscaped, gotDecoded string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotEscaped = r.URL.EscapedPath()
		gotDecoded = r.URL.Path
		_, _ = io.WriteString(w, `{}`)
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModePath,
		Routes:       []config.Route{{Match: "/openai", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderOpenAI}},
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	// Build the request URL with the encoded segment preserved (http.NewRequest would otherwise not
	// re-encode, but set both Path and RawPath explicitly to be certain the escaped form travels).
	u := mustURL(t, front.URL)
	u.Path = "/openai/v1/foo/bar"
	u.RawPath = "/openai/v1/foo%2Fbar"
	resp, err := http.Post(u.String(), "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()

	if gotEscaped != "/v1/foo%2Fbar" {
		t.Errorf("upstream escaped path = %q, want /v1/foo%%2Fbar (encoded segment preserved)", gotEscaped)
	}
	if gotDecoded != "/v1/foo/bar" {
		t.Errorf("upstream decoded path = %q, want /v1/foo/bar", gotDecoded)
	}
}

// TestPathRoutingNoMatchReturns404WithPathMessage confirms the 404 body names the path-prefix match
// mode rather than always claiming "no route for host".
func TestPathRoutingNoMatchReturns404WithPathMessage(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("upstream must not be reached for an unmatched path")
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModePath,
		Routes:       []config.Route{{Match: "/openai", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderOpenAI}},
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	resp, err := http.Get(front.URL + "/unknown/v1/models")
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "no route for path") {
		t.Errorf("404 body = %q, want it to mention \"no route for path\"", string(body))
	}
}

// TestAnthropicCredentialPassthrough is the concrete sec 6.4 requirement: the Anthropic route
// forwards x-api-key and anthropic-version untouched (NOT Authorization), while X-Tenant-Key and
// the Tally control headers are stripped.
func TestAnthropicCredentialPassthrough(t *testing.T) {
	var gotAPIKey, gotVersion, gotAuth string
	var sawTenant, sawFeature bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAPIKey = r.Header.Get("x-api-key")
		gotVersion = r.Header.Get("anthropic-version")
		gotAuth = r.Header.Get("Authorization")
		_, sawTenant = r.Header["X-Tenant-Key"]
		_, sawFeature = r.Header["X-Tally-Feature-Tag"]
		_, _ = io.WriteString(w, `{"model":"claude","usage":{"input_tokens":1,"output_tokens":1}}`)
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader:     "X-Tenant-Key",
		FeatureTagHeader: "X-Tally-Feature-Tag",
		RouteMode:        config.RouteModeHost,
		Routes:           []config.Route{{Match: "anthropic.proxy.test", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderAnthropic}},
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/messages", strings.NewReader(`{}`))
	req.Host = "anthropic.proxy.test"
	req.Header.Set("x-api-key", "sk-ant-realkey")
	req.Header.Set("anthropic-version", "2023-06-01")
	req.Header.Set("X-Tenant-Key", "tally_sk_live_acme")
	req.Header.Set("X-Tally-Feature-Tag", "chat")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()

	if gotAPIKey != "sk-ant-realkey" {
		t.Errorf("x-api-key = %q, want sk-ant-realkey (forwarded untouched)", gotAPIKey)
	}
	if gotVersion != "2023-06-01" {
		t.Errorf("anthropic-version = %q, want 2023-06-01 (forwarded untouched)", gotVersion)
	}
	if gotAuth != "" {
		t.Errorf("Authorization = %q, want empty (Anthropic uses x-api-key, not Authorization)", gotAuth)
	}
	if sawTenant {
		t.Error("X-Tenant-Key must be stripped before upstream")
	}
	if sawFeature {
		t.Error("X-Tally-Feature-Tag must be stripped before upstream")
	}
}
