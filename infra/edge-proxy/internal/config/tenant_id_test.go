// SPDX-License-Identifier: Apache-2.0
// Tests for EDGE_PROXY_TENANT_ID, the envelope tenant claim that lets the default self-hosted
// config (a gateway running with auth disabled) actually record spend instead of having every
// batch refused with 422.
package config

import (
	"strings"
	"testing"
)

func TestTenantIdDefaultsToEmpty(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.TenantId != "" {
		t.Fatalf("TenantId = %q, want empty: an unconfigured tenant is unknown, never invented", cfg.TenantId)
	}
}

func TestTenantIdAcceptsUUID(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_TELEMETRY_URL": "http://gateway:8080/v1/batches",
		"EDGE_PROXY_INGEST_TOKEN":  "svc_token",
		"EDGE_PROXY_TENANT_ID":     " 7F1C3A2E-0000-4000-8000-000000000001 ",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Lowercased and trimmed so the claim matches the gateway's own rendering of the UUID.
	if cfg.TenantId != "7f1c3a2e-0000-4000-8000-000000000001" {
		t.Fatalf("TenantId = %q, want the trimmed lowercase UUID", cfg.TenantId)
	}
	if got := cfg.Warnings(); len(got) != 0 {
		t.Fatalf("unexpected warnings on a fully configured self-host: %v", got)
	}
}

// TestTenantIdRejectsNonUUID: the canonical TenantId is the tenant UUID. A tenant NAME here would
// parse fine and then join to nothing downstream, so it fails loudly at boot rather than quietly
// metering into a bucket nobody reads.
func TestTenantIdRejectsNonUUID(t *testing.T) {
	for _, bad := range []string{
		"local-dev",
		"7f1c3a2e00004000800000000000001",
		"7f1c3a2e-0000-4000-8000-00000000000g",
		"7f1c3a2e-0000-4000-8000",
	} {
		_, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_TENANT_ID": bad}))
		if err == nil {
			t.Fatalf("EDGE_PROXY_TENANT_ID %q was accepted, want a boot failure", bad)
		}
		if !strings.Contains(err.Error(), "EDGE_PROXY_TENANT_ID") {
			t.Fatalf("error for %q does not name the offending var: %v", bad, err)
		}
	}
}

// TestWarnsWhenTenantIdCannotBeUsed: the configured tenant only ever rides on a batch the ingest
// token authenticates. Without that token it is dead configuration, and an operator who set it
// believes their telemetry is attributed when none of it is even sent.
func TestWarnsWhenTenantIdCannotBeUsed(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_TELEMETRY_URL": "http://gateway:8080/v1/batches",
		"EDGE_PROXY_KEYS_URL":      "https://gw.example.com/v1/edge/keys",
		"EDGE_PROXY_SERVICE_TOKEN": "svc",
		"EDGE_PROXY_TENANT_ID":     "7f1c3a2e-0000-4000-8000-000000000001",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var found bool
	for _, w := range cfg.Warnings() {
		if strings.Contains(w, "EDGE_PROXY_TENANT_ID") {
			found = true
		}
	}
	if !found {
		t.Fatalf("want a warning that the configured tenant can never be sent, got %v", cfg.Warnings())
	}
}

// TestDefaultSelfHostShapeStillWarnsLoudly: the shape the integration run used (telemetry on, no
// credential, no key feed) must keep failing loudly rather than being masked by the new knob.
func TestDefaultSelfHostShapeStillWarnsLoudly(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_TELEMETRY_URL": "http://gateway:8080/v1/batches",
		"EDGE_PROXY_TENANT_ID":     "7f1c3a2e-0000-4000-8000-000000000001",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	warnings := strings.Join(cfg.Warnings(), " | ")
	if !strings.Contains(warnings, "NO telemetry") {
		t.Fatalf("want the loud no-credential warning, got %q", warnings)
	}
}
