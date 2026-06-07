// SPDX-License-Identifier: Apache-2.0
// Package keybroker implements the optional key-broker mode for the self-hostable edge proxy
// (CTO-43).
//
// In the default cloud topology the customer's application holds the provider key and sends it on
// each request's Authorization header; the proxy forwards it untouched and never reads it (the
// in-memory-only guarantee from CTO-42). Regulated customers who run the proxy in their own VPC can
// instead enable *broker mode*: the provider key material stays in the customer's KMS, the
// application sends only an ai-tally X-Tenant-Key, and the proxy mints a short-lived bearer token
// from the broker and injects it on the way to the upstream. The provider key therefore never
// leaves the customer's network and never reaches their application code.
//
// A Broker mints credentials. Real deployments back it with the customer's KMS/STS; StaticBroker is
// a file-backed implementation (the "KMS export" a customer drops into their VPC) that mints
// short-lived, cached tokens with the same expiry semantics, so the proxy code path is identical
// regardless of the backing store.
package keybroker

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"time"
)

// Credential is a short-lived provider authorization minted for one tenant. Authorization is the
// full header value to send upstream (e.g. "Bearer sk-..."); ExpiresAt bounds its validity so a
// leaked token is useless after the TTL.
type Credential struct {
	Authorization string
	ExpiresAt     time.Time
}

// expired reports whether the credential is at or past its expiry as of now.
func (c Credential) expired(now time.Time) bool {
	return !c.ExpiresAt.After(now)
}

// Broker mints a provider Credential for an ai-tally tenant key. Implementations must be safe for
// concurrent use: the proxy calls Mint inline on the request hot path from many goroutines.
type Broker interface {
	// Mint returns a currently-valid Credential for the tenant, or an error if the tenant is
	// unknown or the backing store is unreachable. The proxy treats an error as "do not forward".
	Mint(ctx context.Context, tenantKey string) (Credential, error)
}

// ErrUnknownTenant is returned when no key material exists for the requested tenant.
type ErrUnknownTenant struct{ Tenant string }

func (e ErrUnknownTenant) Error() string {
	return fmt.Sprintf("keybroker: no key material for tenant %q", e.Tenant)
}

// StaticBroker mints tokens from an in-memory map of tenant -> provider key, loaded from a JSON
// file that represents the customer's KMS export. Minted credentials are cached per tenant and
// re-minted once they pass the configured TTL, modelling KMS-backed short-lived tokens without a
// network round-trip on every request.
//
// The raw key material is held only in memory (never logged, never written back to disk by this
// process) — consistent with the proxy's in-memory-only key guarantee.
type StaticBroker struct {
	ttl  time.Duration
	now  func() time.Time
	keys map[string]string // tenant key -> provider Authorization header value

	mu    sync.Mutex
	cache map[string]Credential
}

// keyFile is the on-disk shape of the KMS export.
//
//	{"tenants": {"tk_live_acme": "Bearer sk-...", "tk_live_globex": "Bearer sk-..."}}
type keyFile struct {
	Tenants map[string]string `json:"tenants"`
}

// NewStaticBroker builds a broker from an already-parsed tenant->Authorization map. ttl bounds how
// long a minted credential is reused before being re-minted (<=0 means mint fresh every call).
func NewStaticBroker(keys map[string]string, ttl time.Duration) *StaticBroker {
	cp := make(map[string]string, len(keys))
	for k, v := range keys {
		cp[k] = v
	}
	return &StaticBroker{
		ttl:   ttl,
		now:   time.Now,
		keys:  cp,
		cache: make(map[string]Credential),
	}
}

// LoadStaticBroker reads a JSON KMS-export file and builds a StaticBroker from it.
func LoadStaticBroker(path string, ttl time.Duration) (*StaticBroker, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("keybroker: read %q: %w", path, err)
	}
	var kf keyFile
	if err := json.Unmarshal(raw, &kf); err != nil {
		return nil, fmt.Errorf("keybroker: parse %q: %w", path, err)
	}
	if len(kf.Tenants) == 0 {
		return nil, fmt.Errorf("keybroker: %q has no tenants", path)
	}
	return NewStaticBroker(kf.Tenants, ttl), nil
}

// Mint implements Broker. It returns a cached credential while still valid, otherwise mints a new
// short-lived token from the stored key material.
func (b *StaticBroker) Mint(_ context.Context, tenantKey string) (Credential, error) {
	auth, ok := b.keys[tenantKey]
	if !ok {
		return Credential{}, ErrUnknownTenant{Tenant: tenantKey}
	}

	now := b.now()

	b.mu.Lock()
	defer b.mu.Unlock()
	if cred, ok := b.cache[tenantKey]; ok && !cred.expired(now) {
		return cred, nil
	}

	exp := now.Add(b.ttl)
	if b.ttl <= 0 {
		// No caching window: still hand back a credential, but mark it already-expired so it is
		// never reused from cache.
		exp = now
	}
	cred := Credential{Authorization: auth, ExpiresAt: exp}
	b.cache[tenantKey] = cred
	return cred, nil
}

// withClock swaps the time source (tests only).
func (b *StaticBroker) withClock(now func() time.Time) *StaticBroker {
	b.now = now
	return b
}
