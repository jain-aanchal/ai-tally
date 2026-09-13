// SPDX-License-Identifier: Apache-2.0
package edgekeys

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// HashKey must match gateway/auth.py hash_key exactly (SHA-256 hex of the UTF-8 token), or the
// proxy resolves nothing. Pin it against an independent computation.
func TestHashKeyMatchesGatewayTransform(t *testing.T) {
	sum := sha256.Sum256([]byte("tally_sk_live_abc"))
	want := hex.EncodeToString(sum[:])
	if got := HashKey("tally_sk_live_abc"); got != want {
		t.Fatalf("HashKey = %q, want %q", got, want)
	}
}

// fakeFeed is a stand-in for the gateway GET /v1/edge/keys delta endpoint. It serves a scripted
// sequence of responses keyed by the incoming `since` cursor and records auth headers seen.
type fakeFeed struct {
	mu        sync.Mutex
	responses map[string]feedResponse // since-cursor -> response
	lastAuth  string
	calls     int
}

func (f *fakeFeed) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.lastAuth = r.Header.Get("Authorization")
	f.calls++
	since := r.URL.Query().Get("since")
	resp, ok := f.responses[since]
	f.mu.Unlock()
	if !ok {
		// Unknown cursor: no changes, echo cursor back.
		resp = feedResponse{Cursor: since}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func hashOf(tok string) string { return HashKey(tok) }

func TestRefreshColdStartHitAndMiss(t *testing.T) {
	acme := hashOf("tally_sk_live_acme")
	feed := &fakeFeed{responses: map[string]feedResponse{
		"": {
			Changes: []change{{KeyHash: acme, TenantID: "uuid-acme", Scope: "write"}},
			Cursor:  "c1",
		},
	}}
	srv := httptest.NewServer(feed)
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "svc-tok", Interval: time.Minute})
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	if id, _, ok := c.Resolve(acme); !ok || id != "uuid-acme" {
		t.Errorf("hit: got (%q,%v), want (uuid-acme,true)", id, ok)
	}
	if _, _, ok := c.Resolve(hashOf("tally_sk_live_unknown")); ok {
		t.Error("miss: unknown key should not resolve")
	}
	if feed.lastAuth != "Bearer svc-tok" {
		t.Errorf("service token not sent: got %q", feed.lastAuth)
	}
}

func TestRefreshAppliesDeltaAndRevocation(t *testing.T) {
	acme := hashOf("tally_sk_live_acme")
	globex := hashOf("tally_sk_live_globex")
	feed := &fakeFeed{responses: map[string]feedResponse{
		"": {
			Changes: []change{{KeyHash: acme, TenantID: "uuid-acme", Scope: "write"}},
			Cursor:  "c1",
		},
		"c1": {
			// Delta since c1: a new key created, and acme revoked.
			Changes: []change{
				{KeyHash: globex, TenantID: "uuid-globex", Scope: "read"},
				{KeyHash: acme, TenantID: "uuid-acme", RevokedAt: "2026-09-01T00:00:00Z"},
			},
			Cursor: "c2",
		},
	}}
	srv := httptest.NewServer(feed)
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "svc-tok", Interval: time.Minute})
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("first refresh: %v", err)
	}
	if _, _, ok := c.Resolve(acme); !ok {
		t.Fatal("acme should resolve after first refresh")
	}

	// Second refresh polls from the persisted cursor c1 and applies the delta incrementally.
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("second refresh: %v", err)
	}
	if _, _, ok := c.Resolve(acme); ok {
		t.Error("revoked key acme should be dropped from the cache")
	}
	if id, _, ok := c.Resolve(globex); !ok || id != "uuid-globex" {
		t.Errorf("globex: got (%q,%v), want (uuid-globex,true)", id, ok)
	}
	if c.Len() != 1 {
		t.Errorf("cache len = %d, want 1 (acme dropped, globex kept)", c.Len())
	}
}

// A transient feed error must not empty the cache or fail open: the last good map survives.
func TestRefreshErrorKeepsLastGoodMap(t *testing.T) {
	acme := hashOf("tally_sk_live_acme")
	var fail bool
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		f := fail
		mu.Unlock()
		if f {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(feedResponse{
			Changes: []change{{KeyHash: acme, TenantID: "uuid-acme", Scope: "write"}},
			Cursor:  "c1",
		})
	}))
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: time.Minute})
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("first refresh: %v", err)
	}
	mu.Lock()
	fail = true
	mu.Unlock()
	if err := c.Refresh(context.Background()); err == nil {
		t.Error("expected error on failing feed")
	}
	if _, _, ok := c.Resolve(acme); !ok {
		t.Error("cache must keep the last good map after a failed refresh")
	}
}

// TestResolveReturnsScope confirms Resolve surfaces the key scope so the proxy can apply the ingest
// scope gate, and CanWrite matches the gateway's write/admin set.
func TestResolveReturnsScope(t *testing.T) {
	writeKey := hashOf("tally_sk_live_write")
	readKey := hashOf("tally_sk_live_read")
	feed := &fakeFeed{responses: map[string]feedResponse{
		"": {
			Changes: []change{
				{KeyHash: writeKey, TenantID: "uuid-w", Scope: "write"},
				{KeyHash: readKey, TenantID: "uuid-r", Scope: "read"},
			},
			Cursor: "c1",
		},
	}}
	srv := httptest.NewServer(feed)
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: time.Minute})
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if _, scope, ok := c.Resolve(writeKey); !ok || scope != "write" || !CanWrite(scope) {
		t.Errorf("write key: got scope=%q ok=%v canWrite=%v, want write/true/true", scope, ok, CanWrite(scope))
	}
	if _, scope, ok := c.Resolve(readKey); !ok || scope != "read" || CanWrite(scope) {
		t.Errorf("read key: got scope=%q ok=%v canWrite=%v, want read/true/false", scope, ok, CanWrite(scope))
	}
	if CanWrite("read") || !CanWrite("write") || !CanWrite("admin") || CanWrite("") {
		t.Error("CanWrite must permit only write/admin, matching gateway WRITE_SCOPES")
	}
}

// TestInitialSyncRetriesThenSucceeds confirms a transient boot-time feed error is retried within the
// bounded initial sync, so the cache is warm before serving and does not reject all traffic.
func TestInitialSyncRetriesThenSucceeds(t *testing.T) {
	acme := hashOf("tally_sk_live_acme")
	var mu sync.Mutex
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls++
		n := calls
		mu.Unlock()
		if n < 3 {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(feedResponse{
			Changes: []change{{KeyHash: acme, TenantID: "uuid-acme", Scope: "write"}},
			Cursor:  "c1",
		})
	}))
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: time.Minute})
	if err := c.InitialSync(context.Background(), 5, time.Millisecond); err != nil {
		t.Fatalf("InitialSync should have succeeded after transient failures: %v", err)
	}
	if _, _, ok := c.Resolve(acme); !ok {
		t.Error("cache must be warm after InitialSync")
	}
}

// TestInitialSyncReturnsErrorAfterExhaustion confirms InitialSync gives up (returning the error) when
// every bounded attempt fails, so the caller can decide whether to fail closed.
func TestInitialSyncReturnsErrorAfterExhaustion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: time.Minute})
	if err := c.InitialSync(context.Background(), 3, time.Millisecond); err == nil {
		t.Error("InitialSync should return an error when all attempts fail")
	}
}

func TestRunRefreshesUntilContextCancelled(t *testing.T) {
	acme := hashOf("tally_sk_live_acme")
	feed := &fakeFeed{responses: map[string]feedResponse{
		"": {Changes: []change{{KeyHash: acme, TenantID: "uuid-acme", Scope: "write"}}, Cursor: "c1"},
	}}
	srv := httptest.NewServer(feed)
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: 5 * time.Millisecond})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { c.Run(ctx); close(done) }()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, _, ok := c.Resolve(acme); ok {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, _, ok := c.Resolve(acme); !ok {
		t.Fatal("Run should have warmed the cache")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Run did not return after cancel")
	}
}

// TestProxySwitchFromFeed covers the per-organization hosted-proxy switch (gateway 0033). An ABSENT
// field is a gateway that predates the switch, so there is no admin choice to honor and the key is
// allowed; an explicit false is an admin turning the proxy off and must be honored; and a later
// change flipping it back on is applied in place, which is how a toggle reaches a running proxy.
func TestProxySwitchFromFeed(t *testing.T) {
	on, off := true, false
	legacy := hashOf("tally_sk_live_legacy")
	optedOut := hashOf("tally_sk_live_off")
	optedIn := hashOf("tally_sk_live_on")
	feed := &fakeFeed{responses: map[string]feedResponse{
		"": {
			Changes: []change{
				{KeyHash: legacy, TenantID: "uuid-l", Scope: "write"},
				{KeyHash: optedOut, TenantID: "uuid-o", Scope: "write", ProxyEnabled: &off},
				{KeyHash: optedIn, TenantID: "uuid-i", Scope: "write", ProxyEnabled: &on},
			},
			Cursor: "c1",
		},
		"c1": {
			Changes: []change{{KeyHash: optedOut, TenantID: "uuid-o", Scope: "write", ProxyEnabled: &on}},
			Cursor:  "c2",
		},
	}}
	srv := httptest.NewServer(feed)
	defer srv.Close()

	c := New(Options{URL: srv.URL, ServiceToken: "t", Interval: time.Minute})
	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if !c.ProxyEnabled(legacy) {
		t.Error("a change with no proxy_enabled field must allow the key (gateway without the switch)")
	}
	if c.ProxyEnabled(optedOut) {
		t.Error("proxy_enabled=false must be honored")
	}
	// Turned off is not revoked: the key is still valid, for the SDK path, and still resolves.
	if _, _, ok := c.Resolve(optedOut); !ok {
		t.Error("a key whose org turned the proxy off must still resolve")
	}
	if !c.ProxyEnabled(optedIn) {
		t.Error("proxy_enabled=true must allow the key")
	}
	if c.ProxyEnabled(hashOf("tally_sk_live_unknown")) {
		t.Error("an unknown key must never report the proxy as enabled")
	}

	if err := c.Refresh(context.Background()); err != nil {
		t.Fatalf("second refresh: %v", err)
	}
	if !c.ProxyEnabled(optedOut) {
		t.Error("a later change turning the proxy on must be applied to the cached key")
	}
}
