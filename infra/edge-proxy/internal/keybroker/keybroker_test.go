package keybroker

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestMintReturnsStoredCredential(t *testing.T) {
	b := NewStaticBroker(map[string]string{"tk_live_acme": "Bearer sk-acme"}, time.Minute)

	cred, err := b.Mint(context.Background(), "tk_live_acme")
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}
	if cred.Authorization != "Bearer sk-acme" {
		t.Fatalf("Authorization = %q, want %q", cred.Authorization, "Bearer sk-acme")
	}
	if !cred.ExpiresAt.After(time.Now()) {
		t.Fatalf("ExpiresAt %v should be in the future", cred.ExpiresAt)
	}
}

func TestMintUnknownTenant(t *testing.T) {
	b := NewStaticBroker(map[string]string{"tk_live_acme": "Bearer sk-acme"}, time.Minute)

	_, err := b.Mint(context.Background(), "tk_live_nope")
	var unknown ErrUnknownTenant
	if !errors.As(err, &unknown) {
		t.Fatalf("err = %v, want ErrUnknownTenant", err)
	}
	if unknown.Tenant != "tk_live_nope" {
		t.Fatalf("ErrUnknownTenant.Tenant = %q", unknown.Tenant)
	}
}

func TestMintCachesUntilTTL(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	b := NewStaticBroker(map[string]string{"tk": "Bearer v1"}, 30*time.Second).
		withClock(func() time.Time { return now })

	first, err := b.Mint(context.Background(), "tk")
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}

	// Rotate the underlying key material; a cached credential must still be returned until TTL.
	b.keys["tk"] = "Bearer v2"

	now = now.Add(10 * time.Second) // still inside the 30s window
	cached, _ := b.Mint(context.Background(), "tk")
	if cached.Authorization != first.Authorization {
		t.Fatalf("expected cached %q, got %q", first.Authorization, cached.Authorization)
	}

	now = now.Add(25 * time.Second) // now past expiry (35s elapsed)
	reminted, _ := b.Mint(context.Background(), "tk")
	if reminted.Authorization != "Bearer v2" {
		t.Fatalf("expected re-mint %q, got %q", "Bearer v2", reminted.Authorization)
	}
}

func TestMintZeroTTLNeverCaches(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	b := NewStaticBroker(map[string]string{"tk": "Bearer v1"}, 0).
		withClock(func() time.Time { return now })

	if _, err := b.Mint(context.Background(), "tk"); err != nil {
		t.Fatalf("Mint: %v", err)
	}
	b.keys["tk"] = "Bearer v2"
	got, _ := b.Mint(context.Background(), "tk") // same instant, but zero TTL => not cached
	if got.Authorization != "Bearer v2" {
		t.Fatalf("zero-TTL should re-read; got %q", got.Authorization)
	}
}

func TestLoadStaticBrokerFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "keys.json")
	body, _ := json.Marshal(keyFile{Tenants: map[string]string{"tk_live_acme": "Bearer sk-acme"}})
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}

	b, err := LoadStaticBroker(path, time.Minute)
	if err != nil {
		t.Fatalf("LoadStaticBroker: %v", err)
	}
	cred, err := b.Mint(context.Background(), "tk_live_acme")
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}
	if cred.Authorization != "Bearer sk-acme" {
		t.Fatalf("Authorization = %q", cred.Authorization)
	}
}

func TestLoadStaticBrokerErrors(t *testing.T) {
	if _, err := LoadStaticBroker("/no/such/file.json", time.Minute); err == nil {
		t.Fatal("expected error for missing file")
	}

	dir := t.TempDir()
	empty := filepath.Join(dir, "empty.json")
	_ = os.WriteFile(empty, []byte(`{"tenants":{}}`), 0o600)
	if _, err := LoadStaticBroker(empty, time.Minute); err == nil {
		t.Fatal("expected error for empty tenants")
	}
}

func TestMintConcurrent(t *testing.T) {
	b := NewStaticBroker(map[string]string{"tk": "Bearer v1"}, time.Minute)
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := b.Mint(context.Background(), "tk"); err != nil {
				t.Errorf("Mint: %v", err)
			}
		}()
	}
	wg.Wait()
}
