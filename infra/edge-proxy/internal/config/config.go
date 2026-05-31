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

// Config is the fully-resolved, validated proxy configuration.
type Config struct {
	// ListenAddr is the address the proxy binds (e.g. ":8088").
	ListenAddr string
	// Upstream is the provider origin requests are forwarded to (e.g. https://api.openai.com).
	Upstream *url.URL
	// TenantHeader names the control header carrying the ai-tally tenant key (default X-Tenant-Key).
	// It is stripped before the request leaves for the upstream provider.
	TenantHeader string
	// RequireTenant rejects requests missing TenantHeader with 400 when true.
	RequireTenant bool
	// UpstreamTimeout bounds a single forwarded request end-to-end (0 = no timeout, for streaming).
	UpstreamTimeout time.Duration
}

// Defaults applied when the corresponding env var is unset.
const (
	DefaultListenAddr   = ":8088"
	DefaultUpstream     = "https://api.openai.com"
	DefaultTenantHeader = "X-Tenant-Key"
	// DefaultUpstreamTimeout is generous because LLM completions are slow and may stream for
	// minutes; the proxy must not be the thing that cuts a long generation short.
	DefaultUpstreamTimeout = 10 * time.Minute
)

// Env is a minimal indirection over os.Getenv so tests can supply a fixed environment.
type Env func(key string) string

// FromEnv resolves and validates a Config from the given lookup function.
func FromEnv(lookup Env) (Config, error) {
	cfg := Config{
		ListenAddr:      firstNonEmpty(lookup("EDGE_PROXY_LISTEN"), DefaultListenAddr),
		TenantHeader:    firstNonEmpty(lookup("EDGE_PROXY_TENANT_HEADER"), DefaultTenantHeader),
		UpstreamTimeout: DefaultUpstreamTimeout,
	}

	rawUpstream := firstNonEmpty(lookup("EDGE_PROXY_UPSTREAM"), DefaultUpstream)
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
