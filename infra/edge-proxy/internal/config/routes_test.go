// SPDX-License-Identifier: Apache-2.0
package config

import "testing"

func TestRoutesUnsetKeepsSingleOrigin(t *testing.T) {
	cfg, err := FromEnv(envMap(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(cfg.Routes) != 0 {
		t.Errorf("Routes should be empty by default, got %d", len(cfg.Routes))
	}
	if cfg.RouteMode != RouteModeHost {
		t.Errorf("RouteMode default = %q, want host", cfg.RouteMode)
	}
}

func TestRoutesCommaListHostMode(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_ROUTES": "openai.proxy.ai-tally.com=https://api.openai.com:openai," +
			"anthropic.proxy.ai-tally.com=https://api.anthropic.com:anthropic",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(cfg.Routes) != 2 {
		t.Fatalf("want 2 routes, got %d", len(cfg.Routes))
	}
	byMatch := map[string]Route{}
	for _, r := range cfg.Routes {
		byMatch[r.Match] = r
	}
	oa := byMatch["openai.proxy.ai-tally.com"]
	if oa.Upstream == nil || oa.Upstream.String() != "https://api.openai.com" {
		t.Errorf("openai upstream = %v, want https://api.openai.com", oa.Upstream)
	}
	if oa.Provider != ProviderOpenAI {
		t.Errorf("openai provider = %q, want openai", oa.Provider)
	}
	an := byMatch["anthropic.proxy.ai-tally.com"]
	if an.Provider != ProviderAnthropic {
		t.Errorf("anthropic provider = %q, want anthropic", an.Provider)
	}
}

// A port in the upstream must not be mistaken for a provider name (last-colon parsing).
func TestRoutesUpstreamWithPortNotMisreadAsProvider(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_ROUTES": "self.internal=https://api.internal:8443",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(cfg.Routes) != 1 {
		t.Fatalf("want 1 route, got %d", len(cfg.Routes))
	}
	r := cfg.Routes[0]
	if r.Upstream.String() != "https://api.internal:8443" {
		t.Errorf("upstream = %q, want https://api.internal:8443", r.Upstream)
	}
	if r.Provider != "" {
		t.Errorf("provider = %q, want empty (pass-through)", r.Provider)
	}
}

func TestRoutesPathModeNormalizesPrefix(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_ROUTE_MODE": "path",
		"EDGE_PROXY_ROUTES":     "openai=https://api.openai.com:openai",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.RouteMode != RouteModePath {
		t.Fatalf("RouteMode = %q, want path", cfg.RouteMode)
	}
	if cfg.Routes[0].Match != "/openai" {
		t.Errorf("path match = %q, want /openai (leading slash, no trailing)", cfg.Routes[0].Match)
	}
}

func TestRoutesJSONForm(t *testing.T) {
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_ROUTES": `{"anthropic.proxy.ai-tally.com":{"upstream":"https://api.anthropic.com","provider":"anthropic"}}`,
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(cfg.Routes) != 1 || cfg.Routes[0].Provider != ProviderAnthropic {
		t.Fatalf("want 1 anthropic route, got %+v", cfg.Routes)
	}
}

func TestRoutesInvalid(t *testing.T) {
	cases := map[string]map[string]string{
		"no equals":         {"EDGE_PROXY_ROUTES": "openai.proxy.ai-tally.com"},
		"bad scheme":        {"EDGE_PROXY_ROUTES": "h=ftp://api.openai.com:openai"},
		"empty match":       {"EDGE_PROXY_ROUTES": "=https://api.openai.com:openai"},
		"bad route mode":    {"EDGE_PROXY_ROUTE_MODE": "sideways", "EDGE_PROXY_ROUTES": "a=https://x.com"},
		"bad json provider": {"EDGE_PROXY_ROUTES": `{"h":{"upstream":"https://x.com","provider":"bogus"}}`},
	}
	for name, env := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := FromEnv(envMap(env)); err == nil {
				t.Errorf("expected error for %s", name)
			}
		})
	}
}

func TestKeysURLRequiresServiceToken(t *testing.T) {
	if _, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_KEYS_URL": "https://gateway/v1/edge/keys",
	})); err == nil {
		t.Error("EDGE_PROXY_KEYS_URL without a service token should error")
	}
	cfg, err := FromEnv(envMap(map[string]string{
		"EDGE_PROXY_KEYS_URL":              "https://gateway/v1/edge/keys",
		"EDGE_PROXY_SERVICE_TOKEN":         "svc-token",
		"EDGE_PROXY_KEYS_REFRESH_INTERVAL": "30s",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.KeysRefreshInterval != 30_000_000_000 {
		t.Errorf("refresh interval = %s, want 30s", cfg.KeysRefreshInterval)
	}
}
