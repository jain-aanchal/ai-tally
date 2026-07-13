// SPDX-License-Identifier: Apache-2.0
// Package config loads the edge-proxy runtime configuration from the environment.
//
// The proxy is deliberately stateless: every knob comes from an env var so the same binary
// scales horizontally behind a load balancer with no per-instance state to coordinate. Nothing
// here is a secret — the customer's provider key rides on each request's Authorization header and
// is never read into config (see package proxy for the in-memory-only guarantee).
package config

import (
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
)

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
	// DefaultUpstreamTimeout is generous because LLM completions are slow and may stream for
	// minutes; the proxy must not be the thing that cuts a long generation short.
	DefaultUpstreamTimeout = 10 * time.Minute
	// DefaultBrokerTTL bounds minted-token reuse in broker mode.
	DefaultBrokerTTL = 5 * time.Minute
)

// Env is a minimal indirection over os.Getenv so tests can supply a fixed environment.
type Env func(key string) string

// FromEnv resolves and validates a Config from the given lookup function.
func FromEnv(lookup Env) (Config, error) {
	cfg := Config{
		ListenAddr:       firstNonEmpty(lookup("EDGE_PROXY_LISTEN"), DefaultListenAddr),
		TenantHeader:     firstNonEmpty(lookup("EDGE_PROXY_TENANT_HEADER"), DefaultTenantHeader),
		FeatureTagHeader: firstNonEmpty(lookup("EDGE_PROXY_FEATURE_TAG_HEADER"), DefaultFeatureTagHeader),
		UpstreamTimeout:  DefaultUpstreamTimeout,
		Mode:             ModePassthrough,
		BrokerTTL:        DefaultBrokerTTL,
		TelemetryURL:     lookup("EDGE_PROXY_TELEMETRY_URL"),
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

	return cfg, nil
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
