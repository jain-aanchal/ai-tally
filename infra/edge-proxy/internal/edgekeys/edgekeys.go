// SPDX-License-Identifier: Apache-2.0
// Package edgekeys is the proxy's in-memory, delta-synced view of the control plane's api_keys
// (Initiative 2 sec 6.2).
//
// To hold p99 < 3ms the proxy must map the real X-Tenant-Key (tally_sk_live_...) to a tenant UUID
// with no per-request gateway round-trip. It keeps a map of sha256(key) -> {tenant_uuid, scope}
// and computes the same sha256 transform the gateway uses (gateway/auth.py hash_key: SHA-256 hex of
// the UTF-8 token) to look the presented key up locally.
//
// The map is built from the gateway's read-only delta feed GET /v1/edge/keys?since={cursor}: each
// tick ships only creations and revocations since the last cursor, so steady state is cheap and a
// cold start (empty cursor) pages the full active set once. Revoked keys arrive with revoked_at set
// and are dropped, so revocation propagates within one refresh interval (the proxy-path revocation
// SLA).
//
// The hash map is sensitive: the hashes are not reversible, but a compromised proxy could use them
// for offline guess-validation, so it is held in memory only and never logged.
package edgekeys

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// HashKey computes the same key hash the gateway stores in api_keys.key_hash: the SHA-256 hex of
// the UTF-8 token (gateway/auth.py hash_key). Keeping this identical is what lets the proxy resolve
// a presented X-Tenant-Key against the control plane's stored hashes without ever seeing raw keys.
func HashKey(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// entry is the resolved identity for one key hash.
type entry struct {
	tenantID string
	scope    string
}

// change is one row of the delta feed. Only the SHA-256 hash is ever sent, never a raw key.
type change struct {
	KeyHash   string `json:"key_hash"`
	TenantID  string `json:"tenant_id"`
	Scope     string `json:"scope"`
	RevokedAt string `json:"revoked_at"`
}

// feedResponse is the /v1/edge/keys payload: the changes since the requested cursor plus the new
// watermark to poll from next.
type feedResponse struct {
	Changes []change `json:"changes"`
	Cursor  string   `json:"cursor"`
}

// Cache is the proxy's live key->tenant view. It is safe for concurrent use: Resolve runs on the
// request hot path from many goroutines while Refresh applies deltas.
type Cache struct {
	url      string
	token    string
	interval time.Duration
	client   *http.Client

	mu     sync.RWMutex
	m      map[string]entry
	cursor string
}

// Options configures a Cache.
type Options struct {
	// URL is the gateway edge-keys endpoint, e.g. https://gateway/v1/edge/keys.
	URL string
	// ServiceToken is the server-only bearer token sent as Authorization to the feed.
	ServiceToken string
	// Interval bounds how often Run polls the feed.
	Interval time.Duration
	// Client overrides the HTTP client (tests). Defaults to a short-timeout client.
	Client *http.Client
}

// New builds an empty Cache. Call Refresh (or Run) to populate it from the feed.
func New(opts Options) *Cache {
	client := opts.Client
	if client == nil {
		// The feed is off the request hot path, but a hung poll must not stall refresh forever.
		client = &http.Client{Timeout: 10 * time.Second}
	}
	return &Cache{
		url:      opts.URL,
		token:    opts.ServiceToken,
		interval: opts.Interval,
		client:   client,
		m:        make(map[string]entry),
	}
}

// Resolve returns the tenant UUID and scope for a key hash and whether it is present (and not
// revoked). A revoked key is absent, so Resolve returns ("", "", false) and the proxy fails closed.
// The scope lets the caller apply the gateway's ingest scope gate (CTO-33). This is the only method
// on the request hot path; it takes a read lock and does an O(1) map lookup.
func (c *Cache) Resolve(keyHash string) (tenantID string, scope string, ok bool) {
	c.mu.RLock()
	e, found := c.m[keyHash]
	c.mu.RUnlock()
	if !found {
		return "", "", false
	}
	return e.tenantID, e.scope, true
}

// writeScopes mirrors the gateway's WRITE_SCOPES (gateway/auth.py): the scopes permitted to write
// ingest data. Kept identical so the proxy and gateway agree on who may write (CTO-33).
var writeScopes = map[string]bool{"write": true, "admin": true}

// CanWrite reports whether a key scope may write ingest data. Metering a call through the proxy is
// ingest, so a read-only key authenticates but is not sufficient to be attributed.
func CanWrite(scope string) bool { return writeScopes[scope] }

// InitialSync performs the first feed sync before the proxy starts serving, retrying a bounded
// number of times on a transient error. Without a warm cache a boot-time feed blip would leave the
// map empty and, with RequireTenant set, the proxy fails closed on every request (403) for up to a
// full refresh interval, and the "keep the last good map" guarantee cannot help because there is no
// last-good map yet. attempts is clamped to at least 1; between tries it waits backoff (or returns
// early if ctx is cancelled). Returns nil on the first success, else the last error.
func (c *Cache) InitialSync(ctx context.Context, attempts int, backoff time.Duration) error {
	if attempts < 1 {
		attempts = 1
	}
	var err error
	for i := 0; i < attempts; i++ {
		if err = c.Refresh(ctx); err == nil {
			return nil
		}
		if i == attempts-1 {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}
	return err
}

// Run polls the feed every interval until ctx is cancelled. It refreshes once immediately so the
// cache is warm as soon as it starts. A failed poll is left to the next tick: the last good map is
// kept, so a transient gateway blip never empties the cache and never fails open.
func (c *Cache) Run(ctx context.Context) {
	// Warm start; a cold-start error is non-fatal (retried on the tick) so the proxy still boots.
	_ = c.Refresh(ctx)
	t := time.NewTicker(c.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = c.Refresh(ctx)
		}
	}
}

// Refresh polls the feed once from the persisted cursor and applies the returned delta. Creations
// (and scope changes) upsert; revocations delete. The cursor is advanced only on success.
func (c *Cache) Refresh(ctx context.Context) error {
	c.mu.RLock()
	since := c.cursor
	c.mu.RUnlock()

	u, err := url.Parse(c.url)
	if err != nil {
		return fmt.Errorf("edgekeys: bad url %q: %w", c.url, err)
	}
	q := u.Query()
	q.Set("since", since)
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return fmt.Errorf("edgekeys: build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)

	resp, err := c.client.Do(req)
	if err != nil {
		return fmt.Errorf("edgekeys: poll: %w", err)
	}
	defer func() {
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
	}()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("edgekeys: feed returned %d", resp.StatusCode)
	}
	var fr feedResponse
	if err := json.NewDecoder(resp.Body).Decode(&fr); err != nil {
		return fmt.Errorf("edgekeys: decode feed: %w", err)
	}

	c.mu.Lock()
	for _, ch := range fr.Changes {
		if ch.KeyHash == "" {
			continue
		}
		if ch.RevokedAt != "" {
			delete(c.m, ch.KeyHash)
			continue
		}
		c.m[ch.KeyHash] = entry{tenantID: ch.TenantID, scope: ch.Scope}
	}
	if fr.Cursor != "" {
		c.cursor = fr.Cursor
	}
	c.mu.Unlock()
	return nil
}

// Len reports the number of live keys in the cache (tests / diagnostics only).
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.m)
}
