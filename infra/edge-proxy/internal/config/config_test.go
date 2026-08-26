// SPDX-License-Identifier: Apache-2.0
package config

import (
	"testing"
	"time"
)

// envMap builds an Env lookup from a map.
func envMap(m map[string]string) Env {
	return func(k string) string { return m[k] }
}

func TestDefaults(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.ListenAddr != DefaultListenAddr {
		t.Errorf("ListenAddr = %q, want %q", cfg.ListenAddr, DefaultListenAddr)
	}
	if cfg.TenantHeader != DefaultTenantHeader {
		t.Errorf("TenantHeader = %q, want %q", cfg.TenantHeader, DefaultTenantHeader)
	}
	if cfg.Upstream.String() != DefaultUpstream {
		t.Errorf("Upstream = %q, want %q", cfg.Upstream, DefaultUpstream)
	}
	if cfg.RequireTenant {
		t.Error("RequireTenant should default to false")
	}
	if cfg.UpstreamTimeout != DefaultUpstreamTimeout {
		t.Errorf("UpstreamTimeout = %s, want %s", cfg.UpstreamTimeout, DefaultUpstreamTimeout)
	}
}

func TestOverrides(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_LISTEN":           ":9000",
		"EDGE_PROXY_UPSTREAM":         "https://api.example.com/base/",
		"EDGE_PROXY_TENANT_HEADER":    "X-Org",
		"EDGE_PROXY_REQUIRE_TENANT":   "true",
		"EDGE_PROXY_UPSTREAM_TIMEOUT": "90s",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.ListenAddr != ":9000" {
		t.Errorf("ListenAddr = %q", cfg.ListenAddr)
	}
	if cfg.TenantHeader != "X-Org" {
		t.Errorf("TenantHeader = %q", cfg.TenantHeader)
	}
	if !cfg.RequireTenant {
		t.Error("RequireTenant should be true")
	}
	if cfg.UpstreamTimeout != 90*time.Second {
		t.Errorf("UpstreamTimeout = %s", cfg.UpstreamTimeout)
	}
	// Trailing slash must be trimmed so path joining doesn't double up.
	if got := cfg.Upstream.String(); got != "https://api.example.com/base" {
		t.Errorf("Upstream = %q, want trailing slash trimmed", got)
	}
}

func TestInvalidUpstream(t *testing.T) {
	cases := map[string]string{
		"empty scheme": "api.openai.com",
		"ftp scheme":   "ftp://api.openai.com",
		"no host":      "https://",
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_UPSTREAM": raw})); err == nil {
				t.Errorf("expected error for upstream %q", raw)
			}
		})
	}
}

func TestInvalidScalars(t *testing.T) {
	if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_REQUIRE_TENANT": "maybe"})); err == nil {
		t.Error("expected error for non-bool REQUIRE_TENANT")
	}
	if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_UPSTREAM_TIMEOUT": "soon"})); err == nil {
		t.Error("expected error for bad duration")
	}
	if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_UPSTREAM_TIMEOUT": "-5s"})); err == nil {
		t.Error("expected error for negative duration")
	}
}

func TestDefaultModeIsPassthrough(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Mode != ModePassthrough {
		t.Errorf("Mode = %q, want passthrough", cfg.Mode)
	}
	if cfg.SelfHosted {
		t.Error("SelfHosted should default to false")
	}
	if cfg.BrokerTTL != DefaultBrokerTTL {
		t.Errorf("BrokerTTL = %s, want %s", cfg.BrokerTTL, DefaultBrokerTTL)
	}
}

func TestBrokerModeRequiresBrokerFile(t *testing.T) {
	_, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_MODE": "broker"}))
	if err == nil {
		t.Fatal("expected error: broker mode without EDGE_PROXY_BROKER_FILE")
	}
}

func TestBrokerModeAccepted(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_MODE":          "broker",
		"EDGE_PROXY_BROKER_FILE":   "/etc/ai-tally/keys.json",
		"EDGE_PROXY_BROKER_TTL":    "90s",
		"EDGE_PROXY_SELF_HOSTED":   "true",
		"EDGE_PROXY_TELEMETRY_URL": "https://ingest.ai-tally.com/v1/edge",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Mode != ModeBroker {
		t.Errorf("Mode = %q, want broker", cfg.Mode)
	}
	if cfg.BrokerFile != "/etc/ai-tally/keys.json" {
		t.Errorf("BrokerFile = %q", cfg.BrokerFile)
	}
	if cfg.BrokerTTL != 90*time.Second {
		t.Errorf("BrokerTTL = %s, want 90s", cfg.BrokerTTL)
	}
	if !cfg.SelfHosted {
		t.Error("SelfHosted should be true")
	}
	if cfg.TelemetryURL != "https://ingest.ai-tally.com/v1/edge" {
		t.Errorf("TelemetryURL = %q", cfg.TelemetryURL)
	}
}

func TestFeatureTagHeaderDefault(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.FeatureTagHeader != DefaultFeatureTagHeader {
		t.Errorf("FeatureTagHeader = %q, want %q", cfg.FeatureTagHeader, DefaultFeatureTagHeader)
	}
}

func TestFeatureTagHeaderOverride(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_FEATURE_TAG_HEADER": "X-Feature"}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.FeatureTagHeader != "X-Feature" {
		t.Errorf("FeatureTagHeader = %q", cfg.FeatureTagHeader)
	}
}

func TestInvalidMode(t *testing.T) {
	if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_MODE": "sideways"})); err == nil {
		t.Fatal("expected error for invalid mode")
	}
}

// TestProviderSelection covers CTO-167: the provider flag validates its value and switches the
// default upstream to Google's origin for gemini while leaving an explicit upstream untouched.
func TestProviderSelection(t *testing.T) {
	// Default: no provider, pure pass-through, OpenAI default upstream.
	if cfg, _ := FromEnv(envMap(nil)); cfg.Provider != "" {
		t.Errorf("default Provider = %q, want empty", cfg.Provider)
	}

	// gemini with no explicit upstream defaults to Google's Generative Language origin.
	cfg, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_PROVIDER": "gemini"}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Provider != ProviderGemini {
		t.Errorf("Provider = %q, want gemini", cfg.Provider)
	}
	if cfg.Upstream.String() != DefaultGeminiUpstream {
		t.Errorf("Upstream = %q, want %q", cfg.Upstream, DefaultGeminiUpstream)
	}

	// An explicit upstream still wins for gemini.
	cfg, err = FromEnv(envMap(map[string]string{
		"EDGE_PROXY_PROVIDER": "gemini",
		"EDGE_PROXY_UPSTREAM": "https://gemini.internal.example",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Upstream.String() != "https://gemini.internal.example" {
		t.Errorf("Upstream = %q, want explicit override", cfg.Upstream)
	}

	// An unknown provider is rejected.
	if _, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_PROVIDER": "bard"})); err == nil {
		t.Error("expected error for unknown provider")
	}
}

// TestAccountIdHashHeaderDefault: the account control header (CTO-182) defaults to the X-Tally-*
// name documented in the README, matching the existing feature-tag convention.
func TestAccountIdHashHeaderDefault(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.AccountIdHashHeader != DefaultAccountIdHashHeader {
		t.Errorf("AccountIdHashHeader = %q, want %q", cfg.AccountIdHashHeader, DefaultAccountIdHashHeader)
	}
	if DefaultAccountIdHashHeader != "X-Tally-Account-Id-Hash" {
		t.Errorf("default header name changed to %q; update the README", DefaultAccountIdHashHeader)
	}
}

func TestAccountIdHashHeaderOverride(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{"EDGE_PROXY_ACCOUNT_ID_HASH_HEADER": "X-Acct"}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.AccountIdHashHeader != "X-Acct" {
		t.Errorf("AccountIdHashHeader = %q", cfg.AccountIdHashHeader)
	}
}
