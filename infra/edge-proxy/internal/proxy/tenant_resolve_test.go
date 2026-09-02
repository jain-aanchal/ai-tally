// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/edgekeys"
)

// resolverProxy builds a proxy with a key resolver in front of a recording upstream.
func resolverProxy(t *testing.T, requireTenant bool, resolver TenantResolver, upstream http.Handler) (*httptest.Server, *recordingSink) {
	t.Helper()
	origin := httptest.NewServer(upstream)
	t.Cleanup(origin.Close)
	cfg := config.Config{
		TenantHeader:  "X-Tenant-Key",
		RequireTenant: requireTenant,
		Upstream:      mustURL(t, origin.URL),
	}
	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink), WithKeyResolver(resolver)))
	t.Cleanup(front.Close)
	return front, sink
}

// echoUpstream returns 200 with a fixed body and records nothing.
func echoUpstream() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"ok":true}`)
	})
}

// TestKeyResolveHitStampsTenantId: a known key resolves to its tenant UUID, which lands on the
// TraceRecord as the canonical tag (sec 6.3), never the raw key string.
func TestKeyResolveHitStampsTenantId(t *testing.T) {
	resolver := staticResolver{edgekeys.HashKey("tally_sk_live_acme"): "uuid-acme"}
	front, sink := resolverProxy(t, true, resolver, echoUpstream())

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tally_sk_live_acme")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	sink.waitFor(t, 1)
	got := sink.last()
	if got.TenantId != "uuid-acme" {
		t.Errorf("TenantId = %q, want uuid-acme", got.TenantId)
	}
	if got.TenantKey != "tally_sk_live_acme" {
		t.Errorf("TenantKey = %q, want the presented key", got.TenantKey)
	}
}

// TestKeyResolveMissFailsClosed: a key absent from the cache (unknown OR revoked, which the cache
// represents identically by dropping it) is rejected with 403 when RequireTenant is set, never
// forwarded unauthenticated.
func TestKeyResolveMissFailsClosed(t *testing.T) {
	var forwarded bool
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		forwarded = true
		_, _ = io.WriteString(w, `{"ok":true}`)
	})
	// Cache holds only acme; the revoked/unknown key is simply not present.
	resolver := staticResolver{edgekeys.HashKey("tally_sk_live_acme"): "uuid-acme"}
	front, _ := resolverProxy(t, true, resolver, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tally_sk_live_revoked")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want 403 for unknown/revoked key", resp.StatusCode)
	}
	if forwarded {
		t.Error("unknown key must not be forwarded upstream (fail closed)")
	}
}

// TestKeyResolveMissOpenWhenNotRequired: with RequireTenant false, an unresolved key is not
// rejected; it forwards and simply carries no TenantId (honest blank, not a fabricated tag).
func TestKeyResolveMissOpenWhenNotRequired(t *testing.T) {
	resolver := staticResolver{}
	front, sink := resolverProxy(t, false, resolver, echoUpstream())

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tally_sk_live_unknown")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200 (open when tenant not required)", resp.StatusCode)
	}
	sink.waitFor(t, 1)
	if got := sink.last(); got.TenantId != "" {
		t.Errorf("TenantId = %q, want empty for an unresolved key", got.TenantId)
	}
}

// TestKeyResolveReadScopeFailsClosed: a known key whose scope cannot write ingest data (read) is
// rejected with 403 when RequireTenant is set and never forwarded, matching the gateway's ingest
// scope gate (CTO-33).
func TestKeyResolveReadScopeFailsClosed(t *testing.T) {
	var forwarded bool
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		forwarded = true
		_, _ = io.WriteString(w, `{"ok":true}`)
	})
	resolver := scopeResolver{keyHash: edgekeys.HashKey("tally_sk_live_readonly"), tenant: "uuid-acme", scope: "read"}
	front, _ := resolverProxy(t, true, resolver, upstream)

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tally_sk_live_readonly")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want 403 for a read-only key", resp.StatusCode)
	}
	if forwarded {
		t.Error("a read-only key must not be forwarded upstream (fail closed on scope)")
	}
}

// TestKeyResolveReadScopeOpenCarriesNoTenantId: with RequireTenant false, a read-only key still
// forwards but is not attributed (no TenantId), an honest blank rather than a tag for a key that may
// not write.
func TestKeyResolveReadScopeOpenCarriesNoTenantId(t *testing.T) {
	resolver := scopeResolver{keyHash: edgekeys.HashKey("tally_sk_live_readonly"), tenant: "uuid-acme", scope: "read"}
	front, sink := resolverProxy(t, false, resolver, echoUpstream())

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tally_sk_live_readonly")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200 (open when tenant not required)", resp.StatusCode)
	}
	sink.waitFor(t, 1)
	if got := sink.last(); got.TenantId != "" {
		t.Errorf("TenantId = %q, want empty for a read-only key", got.TenantId)
	}
}

// TestByteForByteUnderRouting: routing and key resolution never touch the body. A 1 MiB payload is
// echoed back exactly, proving the streaming/no-mutation invariants hold on the routed path.
func TestByteForByteUnderRouting(t *testing.T) {
	payload := bytes.Repeat([]byte("A"), 1<<20)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		if !bytes.Equal(b, payload) {
			t.Errorf("upstream received %d bytes, want %d exact", len(b), len(payload))
		}
		_, _ = w.Write(b)
	}))
	defer upstream.Close()

	cfg := config.Config{
		TenantHeader: "X-Tenant-Key",
		RouteMode:    config.RouteModeHost,
		Routes:       []config.Route{{Match: "openai.proxy.test", Upstream: mustURL(t, upstream.URL), Provider: config.ProviderOpenAI}},
	}
	resolver := staticResolver{edgekeys.HashKey("tally_sk_live_acme"): "uuid-acme"}
	front := httptest.NewServer(New(cfg, WithKeyResolver(resolver)))
	defer front.Close()

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", bytes.NewReader(payload))
	req.Host = "openai.proxy.test"
	req.Header.Set("X-Tenant-Key", "tally_sk_live_acme")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	echoed, _ := io.ReadAll(resp.Body)
	if !bytes.Equal(echoed, payload) {
		t.Errorf("echoed %d bytes, want %d exact byte-for-byte", len(echoed), len(payload))
	}
}
