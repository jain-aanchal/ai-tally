// SPDX-License-Identifier: Apache-2.0
// Package config loads the edge-proxy runtime configuration from the environment.
//
// The proxy is deliberately stateless: every knob comes from an env var so the same binary
// scales horizontally behind a load balancer with no per-instance state to coordinate. Nothing
// here is a secret — the customer's provider key rides on each request's Authorization header and
// is never read into config (see package proxy for the in-memory-only guarantee).
package config

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// RouteMode selects how EDGE_PROXY_ROUTES entries are matched against an inbound request when the
// hosted multi-provider deployment serves several providers behind one binary (Initiative 2 sec 6.1).
type RouteMode string

const (
	// RouteModeHost matches on the request's hostname (the hosted default). Distinct hostnames map
	// to providers, e.g. openai.proxy.ai-tally.com -> https://api.openai.com. Routing is a
	// hostname-to-origin lookup with no body inspection, so the hot path stays byte-identical.
	RouteModeHost RouteMode = "host"
	// RouteModePath matches on a leading path prefix (fallback for customers who cannot set distinct
	// hostnames), e.g. /openai/... . The matched prefix is stripped before forwarding.
	RouteModePath RouteMode = "path"
)

// Route pins one match key (a hostname in host mode, or a leading path segment in path mode) to an
// upstream origin and the provider protocol used to read response metadata for that origin. It is a
// pre-forward origin selection only: bodies, headers, and credentials are still forwarded
// byte-for-byte (Initiative 2 sec 6.1 / 6.4).
type Route struct {
	// Match is the hostname (host mode) or leading path prefix (path mode) this route serves.
	Match string
	// Upstream is the provider origin requests on this route are forwarded to.
	Upstream *url.URL
	// Provider selects the extractMeta branch for this route's responses (may be empty for pure
	// pass-through with no metadata extraction).
	Provider Provider
}

// Mode selects how the customer's provider key reaches the upstream.
type Mode string

const (
	// ModePassthrough is the default cloud behavior: the customer's app sends the provider key on
	// each request's Authorization header and the proxy forwards it untouched, never reading it.
	ModePassthrough Mode = "passthrough"
	// ModeBroker is the self-host / regulated-customer behavior (CTO-43): the provider key stays in
	// the customer's KMS, the app sends only an ai-tally tenant key, and the proxy mints a
	// short-lived token from the broker and injects it on the way upstream.
	ModeBroker Mode = "broker"
)

// Provider names the upstream API protocol so the proxy knows how to read scalar metadata (model,
// token usage) out of the relayed response (CTO-167). It is orthogonal to Mode: it changes what the
// proxy *understands* about the traffic, never how the request is forwarded — every provider is
// still a byte-for-byte pass-through.
//
// The empty Provider ("") is the CTO-39 default: pure pass-through with zero response inspection, so
// the hot path stays byte-identical and pays no parsing overhead. Set a provider only when you want
// per-request model/usage metadata on the TraceRecord.
type Provider string

const (
	// ProviderOpenAI reads the OpenAI chat/completions response shape: top-level "model" and
	// usage.prompt_tokens / usage.completion_tokens.
	ProviderOpenAI Provider = "openai"
	// ProviderAnthropic reads the Anthropic Messages response shape: top-level "model" and
	// usage.input_tokens / usage.output_tokens.
	ProviderAnthropic Provider = "anthropic"
	// ProviderGemini reads Google's Generative Language API: the model comes from the request path
	// (/v1beta/models/{model}:generateContent), token usage from usageMetadata.promptTokenCount /
	// usageMetadata.candidatesTokenCount, and auth is a ?key= query param or x-goog-api-key header
	// rather than Authorization: Bearer.
	ProviderGemini Provider = "gemini"
)

// Config is the fully-resolved, validated proxy configuration.
type Config struct {
	// ListenAddr is the address the proxy binds (e.g. ":8088").
	ListenAddr string
	// Upstream is the provider origin requests are forwarded to (e.g. https://api.openai.com).
	Upstream *url.URL
	// TenantHeader names the control header carrying the ai-tally tenant key (default X-Tenant-Key).
	// It is stripped before the request leaves for the upstream provider.
	TenantHeader string
	// FeatureTagHeader names an optional control header carrying a per-request feature/agent tag
	// (default X-Tally-Feature-Tag). Like TenantHeader, it is stripped before the request leaves for
	// the upstream provider; its value is recorded on the TraceRecord so downstream telemetry can
	// segment traffic by feature (CTO-104). Unlike the tenant key, the feature tag is purely
	// informational — missing/empty is fine and never rejected.
	FeatureTagHeader string
	// AccountIdHashHeader names an optional control header carrying the HMAC-SHA256 hex of the
	// caller's own paying customer / account id (default X-Tally-Account-Id-Hash). It exists so
	// non-Python callers get the same cost-per-customer dimension the Python SDK's with_account()
	// provides (CTO-182). Like TenantHeader and FeatureTagHeader it is stripped before the request
	// leaves for the upstream provider, and like the feature tag it is purely informational:
	// missing/empty is fine and never rejected, landing in the unattributed bucket downstream.
	//
	// The value must ALREADY be hashed by the caller. The proxy never sees, and must never be sent,
	// a raw account id: the whole point of the hash is that a customer identifier cannot be
	// reversed or joined across tenants. The proxy does not hash on the caller's behalf because it
	// does not hold the tenant's HMAC key.
	AccountIdHashHeader string
	// RequireTenant rejects requests missing TenantHeader with 400 when true.
	RequireTenant bool
	// UpstreamTimeout bounds a single forwarded request end-to-end (0 = no timeout, for streaming).
	UpstreamTimeout time.Duration

	// --- BYO-deployment / key-broker (CTO-43) ---

	// Mode selects passthrough (default) or broker key handling.
	Mode Mode
	// Provider selects the upstream protocol for response-metadata extraction (CTO-167). Empty means
	// pure pass-through with no response inspection (the CTO-39 default). When set (openai, anthropic,
	// gemini) the proxy reads scalar model/usage metadata off each response into the TraceRecord.
	Provider Provider
	// BrokerFile is the path to the JSON KMS-export consumed in broker mode (required when Mode is
	// broker). The file maps ai-tally tenant key -> provider Authorization header value.
	BrokerFile string
	// BrokerTTL bounds how long a minted credential is reused before re-minting.
	BrokerTTL time.Duration
	// SelfHosted marks this as a customer-VPC deployment; it labels emitted telemetry so the cloud
	// can distinguish self-hosted ingest. Defaults to false (cloud).
	SelfHosted bool
	// TelemetryURL, if set, is the collector endpoint the proxy POSTs metadata-only TraceRecords
	// to. Empty disables telemetry shipping (NopSink), as in the CTO-39 core.
	TelemetryURL string

	// --- Hosted multi-provider routing (Initiative 2 sec 6.1) ---

	// Routes is the per-request route table for the hosted deployment. Empty means single-origin
	// mode: forward everything to Upstream with Provider, preserving self-host and CTO-39 behavior.
	Routes []Route
	// RouteMode selects host- or path-based matching for Routes. Defaults to host.
	RouteMode RouteMode

	// --- Fast key-to-tenant resolution (Initiative 2 sec 6.2) ---

	// KeysURL is the gateway's read-only edge-keys delta endpoint (GET /v1/edge/keys?since=cursor)
	// the proxy polls to build its in-memory sha256(key)->tenant-UUID map. Empty disables edge key
	// resolution, so self-host and the CTO-39 core are unaffected.
	KeysURL string
	// ServiceToken authenticates the proxy to KeysURL (server-only bearer token, never a human
	// session). Sent as Authorization: Bearer <token>.
	ServiceToken string
	// KeysRefreshInterval bounds how often the proxy polls KeysURL for changes; it is also the
	// revocation-propagation SLA on the proxy path.
	KeysRefreshInterval time.Duration
}

// Defaults applied when the corresponding env var is unset.
const (
	DefaultListenAddr = ":8088"
	DefaultUpstream   = "https://api.openai.com"
	// DefaultGeminiUpstream is the origin used when Provider is gemini and EDGE_PROXY_UPSTREAM is
	// unset — Google's Generative Language API (CTO-167).
	DefaultGeminiUpstream   = "https://generativelanguage.googleapis.com"
	DefaultTenantHeader     = "X-Tenant-Key"
	DefaultFeatureTagHeader = "X-Tally-Feature-Tag"
	// DefaultAccountIdHashHeader carries the pre-hashed account id (CTO-182). Named to match the
	// existing X-Tally-* control-header convention and the gen_ai.account_id_hash wire attribute.
	DefaultAccountIdHashHeader = "X-Tally-Account-Id-Hash"
	// DefaultUpstreamTimeout is generous because LLM completions are slow and may stream for
	// minutes; the proxy must not be the thing that cuts a long generation short.
	DefaultUpstreamTimeout = 10 * time.Minute
	// DefaultBrokerTTL bounds minted-token reuse in broker mode.
	DefaultBrokerTTL = 5 * time.Minute
	// DefaultKeysRefreshInterval is how often the proxy polls the edge-keys delta feed. The spec
	// suggests 30 to 60s; 45s keeps the revocation window bounded without hammering the gateway.
	DefaultKeysRefreshInterval = 45 * time.Second
)

// Env is a minimal indirection over os.Getenv so tests can supply a fixed environment.
type Env func(key string) string

// FromEnv resolves and validates a Config from the given lookup function.
func FromEnv(lookup Env) (Config, error) {
	cfg := Config{
		ListenAddr:       firstNonEmpty(lookup("EDGE_PROXY_LISTEN"), DefaultListenAddr),
		TenantHeader:     firstNonEmpty(lookup("EDGE_PROXY_TENANT_HEADER"), DefaultTenantHeader),
		FeatureTagHeader: firstNonEmpty(lookup("EDGE_PROXY_FEATURE_TAG_HEADER"), DefaultFeatureTagHeader),
		AccountIdHashHeader: firstNonEmpty(
			lookup("EDGE_PROXY_ACCOUNT_ID_HASH_HEADER"), DefaultAccountIdHashHeader),
		UpstreamTimeout: DefaultUpstreamTimeout,
		Mode:            ModePassthrough,
		BrokerTTL:       DefaultBrokerTTL,
		TelemetryURL:    lookup("EDGE_PROXY_TELEMETRY_URL"),
	}

	// Provider is orthogonal to upstream selection but changes the default upstream: a gemini proxy
	// with no explicit EDGE_PROXY_UPSTREAM defaults to Google's origin rather than OpenAI's.
	if v := lookup("EDGE_PROXY_PROVIDER"); v != "" {
		switch Provider(v) {
		case ProviderOpenAI, ProviderAnthropic, ProviderGemini:
			cfg.Provider = Provider(v)
		default:
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_PROVIDER %q (want openai|anthropic|gemini)", v)
		}
	}

	defaultUpstream := DefaultUpstream
	if cfg.Provider == ProviderGemini {
		defaultUpstream = DefaultGeminiUpstream
	}
	rawUpstream := firstNonEmpty(lookup("EDGE_PROXY_UPSTREAM"), defaultUpstream)
	u, err := url.Parse(rawUpstream)
	if err != nil {
		return Config{}, fmt.Errorf("invalid EDGE_PROXY_UPSTREAM %q: %w", rawUpstream, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return Config{}, fmt.Errorf("EDGE_PROXY_UPSTREAM must be http(s), got %q", rawUpstream)
	}
	if u.Host == "" {
		return Config{}, fmt.Errorf("EDGE_PROXY_UPSTREAM %q has no host", rawUpstream)
	}
	// Forwarding joins onto the upstream path, so a trailing slash would double up ("//v1").
	u.Path = strings.TrimRight(u.Path, "/")
	cfg.Upstream = u

	if v := lookup("EDGE_PROXY_REQUIRE_TENANT"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_REQUIRE_TENANT %q: %w", v, err)
		}
		cfg.RequireTenant = b
	}

	if v := lookup("EDGE_PROXY_UPSTREAM_TIMEOUT"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_UPSTREAM_TIMEOUT %q: %w", v, err)
		}
		if d < 0 {
			return Config{}, fmt.Errorf("EDGE_PROXY_UPSTREAM_TIMEOUT must be >= 0, got %s", d)
		}
		cfg.UpstreamTimeout = d
	}

	if v := lookup("EDGE_PROXY_SELF_HOSTED"); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_SELF_HOSTED %q: %w", v, err)
		}
		cfg.SelfHosted = b
	}

	if v := lookup("EDGE_PROXY_MODE"); v != "" {
		switch Mode(v) {
		case ModePassthrough, ModeBroker:
			cfg.Mode = Mode(v)
		default:
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_MODE %q (want passthrough|broker)", v)
		}
	}

	if v := lookup("EDGE_PROXY_BROKER_TTL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_BROKER_TTL %q: %w", v, err)
		}
		if d < 0 {
			return Config{}, fmt.Errorf("EDGE_PROXY_BROKER_TTL must be >= 0, got %s", d)
		}
		cfg.BrokerTTL = d
	}

	cfg.BrokerFile = lookup("EDGE_PROXY_BROKER_FILE")
	if cfg.Mode == ModeBroker && cfg.BrokerFile == "" {
		return Config{}, fmt.Errorf("EDGE_PROXY_MODE=broker requires EDGE_PROXY_BROKER_FILE")
	}

	// Hosted multi-provider routing (sec 6.1). RouteMode is parsed regardless so an operator can set
	// it explicitly; it only takes effect once EDGE_PROXY_ROUTES is non-empty.
	cfg.RouteMode = RouteModeHost
	if v := lookup("EDGE_PROXY_ROUTE_MODE"); v != "" {
		switch RouteMode(v) {
		case RouteModeHost, RouteModePath:
			cfg.RouteMode = RouteMode(v)
		default:
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_ROUTE_MODE %q (want host|path)", v)
		}
	}
	if v := lookup("EDGE_PROXY_ROUTES"); strings.TrimSpace(v) != "" {
		routes, err := parseRoutes(v, cfg.RouteMode)
		if err != nil {
			return Config{}, err
		}
		cfg.Routes = routes
	}

	// Fast key-to-tenant resolution (sec 6.2). Empty KeysURL keeps the CTO-39 core: no edge key
	// cache, no per-request resolution.
	cfg.KeysURL = lookup("EDGE_PROXY_KEYS_URL")
	cfg.ServiceToken = lookup("EDGE_PROXY_SERVICE_TOKEN")
	cfg.KeysRefreshInterval = DefaultKeysRefreshInterval
	if v := lookup("EDGE_PROXY_KEYS_REFRESH_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid EDGE_PROXY_KEYS_REFRESH_INTERVAL %q: %w", v, err)
		}
		if d <= 0 {
			return Config{}, fmt.Errorf("EDGE_PROXY_KEYS_REFRESH_INTERVAL must be > 0, got %s", d)
		}
		cfg.KeysRefreshInterval = d
	}
	if cfg.KeysURL != "" && cfg.ServiceToken == "" {
		return Config{}, fmt.Errorf("EDGE_PROXY_KEYS_URL requires EDGE_PROXY_SERVICE_TOKEN")
	}

	return cfg, nil
}

// parseRoutes builds the route table from EDGE_PROXY_ROUTES. Two wire forms are accepted:
//
//   - JSON object: {"openai.proxy.ai-tally.com": {"upstream": "https://api.openai.com",
//     "provider": "openai"}}
//   - comma list:  openai.proxy.ai-tally.com=https://api.openai.com:openai,anthropic...=...:anthropic
//
// In the comma form the provider is the token after the LAST colon when it names a known provider;
// otherwise the whole value is treated as the upstream with an empty (pure pass-through) provider,
// so an origin carrying a port (https://host:8443) is not misread as a provider.
func parseRoutes(raw string, mode RouteMode) ([]Route, error) {
	raw = strings.TrimSpace(raw)
	if strings.HasPrefix(raw, "{") {
		return parseRoutesJSON(raw, mode)
	}
	var routes []Route
	for _, entry := range strings.Split(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		eq := strings.IndexByte(entry, '=')
		if eq < 0 {
			return nil, fmt.Errorf("invalid EDGE_PROXY_ROUTES entry %q (want match=upstream[:provider])", entry)
		}
		match := strings.TrimSpace(entry[:eq])
		value := strings.TrimSpace(entry[eq+1:])
		rawUpstream, prov := value, Provider("")
		if colon := strings.LastIndexByte(value, ':'); colon >= 0 {
			if p, ok := parseProvider(value[colon+1:]); ok {
				rawUpstream, prov = value[:colon], p
			}
		}
		route, err := buildRoute(match, rawUpstream, prov, mode)
		if err != nil {
			return nil, err
		}
		routes = append(routes, route)
	}
	if len(routes) == 0 {
		return nil, fmt.Errorf("EDGE_PROXY_ROUTES is set but parsed to no routes")
	}
	return routes, nil
}

func parseRoutesJSON(raw string, mode RouteMode) ([]Route, error) {
	var m map[string]struct {
		Upstream string `json:"upstream"`
		Provider string `json:"provider"`
	}
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		return nil, fmt.Errorf("invalid EDGE_PROXY_ROUTES JSON: %w", err)
	}
	var routes []Route
	for match, spec := range m {
		prov := Provider("")
		if spec.Provider != "" {
			p, ok := parseProvider(spec.Provider)
			if !ok {
				return nil, fmt.Errorf("invalid provider %q for route %q", spec.Provider, match)
			}
			prov = p
		}
		route, err := buildRoute(match, spec.Upstream, prov, mode)
		if err != nil {
			return nil, err
		}
		routes = append(routes, route)
	}
	if len(routes) == 0 {
		return nil, fmt.Errorf("EDGE_PROXY_ROUTES is set but parsed to no routes")
	}
	return routes, nil
}

func parseProvider(s string) (Provider, bool) {
	switch Provider(s) {
	case ProviderOpenAI, ProviderAnthropic, ProviderGemini:
		return Provider(s), true
	default:
		return "", false
	}
}

func buildRoute(match, rawUpstream string, prov Provider, mode RouteMode) (Route, error) {
	if match == "" {
		return Route{}, fmt.Errorf("EDGE_PROXY_ROUTES entry has an empty match key")
	}
	// In path mode the match is a leading path prefix; normalize to a single leading slash and no
	// trailing slash so prefix stripping is unambiguous.
	if mode == RouteModePath {
		match = "/" + strings.Trim(match, "/")
	}
	u, err := url.Parse(strings.TrimSpace(rawUpstream))
	if err != nil {
		return Route{}, fmt.Errorf("invalid upstream %q for route %q: %w", rawUpstream, match, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return Route{}, fmt.Errorf("upstream for route %q must be http(s), got %q", match, rawUpstream)
	}
	if u.Host == "" {
		return Route{}, fmt.Errorf("upstream %q for route %q has no host", rawUpstream, match)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return Route{Match: match, Upstream: u, Provider: prov}, nil
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
