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
