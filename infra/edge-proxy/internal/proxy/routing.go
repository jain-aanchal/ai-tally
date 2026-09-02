// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// Initiative 2 sec 6.1: per-request routing.
//
// A single hosted deployment forwards to the right provider origin per request. Routing is a
// pre-forward origin selection: it picks {upstream, provider} from the request's host (host mode)
// or leading path prefix (path mode), and nothing else. Bodies, headers, and credentials are still
// forwarded byte-for-byte, so the p99 budget and every CTO-39 invariant hold. When no routes are
// configured the router falls back to the single-origin cfg.Upstream/cfg.Provider, keeping
// self-host and the existing tests byte-identical.

// resolvedRoute is the concrete forwarding target chosen for one request.
type resolvedRoute struct {
	upstream *url.URL
	provider config.Provider
	// stripPrefix, in path mode, is the leading path segment to remove before forwarding (e.g.
	// "/openai"). Empty in host mode and single-origin mode: the path is forwarded unchanged.
	stripPrefix string
}

// router selects a resolvedRoute per request.
type router struct {
	mode     config.RouteMode
	routes   []config.Route
	fallback resolvedRoute // single-origin target when routes is empty
}

// newRouter compiles the config route table. When cfg.Routes is empty the router serves only the
// single-origin fallback, matching every request.
func newRouter(cfg config.Config) *router {
	r := &router{
		mode:   cfg.RouteMode,
		routes: cfg.Routes,
		fallback: resolvedRoute{
			upstream: cfg.Upstream,
			provider: cfg.Provider,
		},
	}
	// Path mode matches by longest prefix, so order routes longest-match-first once at startup and
	// keep the hot path a simple linear scan with no per-request sorting.
	if r.mode == config.RouteModePath {
		sorted := make([]config.Route, len(r.routes))
		copy(sorted, r.routes)
		sort.SliceStable(sorted, func(i, j int) bool {
			return len(sorted[i].Match) > len(sorted[j].Match)
		})
		r.routes = sorted
	}
	return r
}

// resolve picks the forwarding target for r. ok is false only when routes are configured but none
// matches, which the caller turns into a clean error rather than forwarding to a wrong origin.
func (rt *router) resolve(r *http.Request) (resolvedRoute, bool) {
	if len(rt.routes) == 0 {
		return rt.fallback, true
	}
	switch rt.mode {
	case config.RouteModePath:
		path := r.URL.Path
		for _, route := range rt.routes {
			if path == route.Match || strings.HasPrefix(path, route.Match+"/") {
				return resolvedRoute{
					upstream:    route.Upstream,
					provider:    route.Provider,
					stripPrefix: route.Match,
				}, true
			}
		}
	default: // host mode
		host := hostname(r.Host)
		for _, route := range rt.routes {
			if strings.EqualFold(host, hostname(route.Match)) {
				return resolvedRoute{upstream: route.Upstream, provider: route.Provider}, true
			}
		}
	}
	return resolvedRoute{}, false
}

// hostname strips an optional :port from a Host header value so host-mode matching compares only
// the hostname.
func hostname(h string) string {
	if i := strings.LastIndexByte(h, ':'); i >= 0 {
		// Guard against an IPv6 literal without a port ("[::1]"): only treat the tail as a port when
		// there is no closing bracket after the colon.
		if !strings.Contains(h[i:], "]") {
			return h[:i]
		}
	}
	return h
}
