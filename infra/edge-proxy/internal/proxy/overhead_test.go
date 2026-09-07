// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/edgekeys"
)

// tinyUpstream returns a fixed small JSON body, the shape of a cheap models/health call. We want
// the upstream's own service time to be near-constant so the measured delta is the proxy's added
// overhead, not upstream variance.
func tinyUpstream() http.Handler {
	body := []byte(`{"object":"list","data":[]}`)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
}

func percentile(sorted []time.Duration, p float64) time.Duration {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(p / 100 * float64(len(sorted)-1))
	return sorted[idx]
}

// measure issues n sequential GETs against url with a keep-alive client and returns sorted
// latencies. The first warmup requests are discarded so we never charge a cold TLS/connection
// setup to the steady-state measurement — exactly how the proxy runs in production (hot pool).
func measure(t testing.TB, client *http.Client, url string, warmup, n int) []time.Duration {
	t.Helper()
	return measureWith(t, client, url, nil, warmup, n)
}

// measureWith is measure plus per-request headers, so the hosted path can be measured carrying the
// X-Tenant-Key that triggers the sha256 + cache lookup it pays for in production.
func measureWith(
	t testing.TB, client *http.Client, url string, header http.Header, warmup, n int,
) []time.Duration {
	t.Helper()
	do := func() {
		req, err := http.NewRequest(http.MethodGet, url, nil)
		if err != nil {
			t.Fatalf("build request: %v", err)
		}
		for k, vs := range header {
			for _, v := range vs {
				req.Header.Add(k, v)
			}
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("request: %v", err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	for i := 0; i < warmup; i++ {
		do()
	}
	out := make([]time.Duration, 0, n)
	for i := 0; i < n; i++ {
		start := time.Now()
		do()
		out = append(out, time.Since(start))
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// checkBudget reports the measured overhead and fails when it crosses the CTO-39 budget.
func checkBudget(t *testing.T, label string, direct, proxied []time.Duration) {
	t.Helper()
	// Overhead = proxied tail minus the direct typical service time.
	overheadP99 := percentile(proxied, 99) - percentile(direct, 50)
	if overheadP99 < 0 {
		overheadP99 = 0
	}

	t.Logf("%s: direct  p50=%s p99=%s", label, percentile(direct, 50), percentile(direct, 99))
	t.Logf("%s: proxied p50=%s p99=%s", label, percentile(proxied, 50), percentile(proxied, 99))
	t.Logf("%s: added overhead p99=%s (budget 3ms)", label, overheadP99)

	const budget = 3 * time.Millisecond
	if overheadP99 >= budget {
		t.Errorf("%s: p99 added overhead %s exceeds budget %s", label, overheadP99, budget)
	}
}

// TestOverheadBudget enforces the CTO-39 acceptance criterion: p99 added latency < 3ms.
//
// Both paths hit the same loopback upstream, so subtracting the direct baseline isolates the
// proxy's own processing. This runs on every `go test`, so a regression that fattens the hot path
// (e.g. buffering a body, adding a sync allocation) trips CI rather than shipping silently.
//
// Two configurations are measured, because they do measurably different work per request:
//
//   - single-origin: the CTO-39 core, one upstream, no routing and no key resolution.
//   - hosted: the Initiative 2 shape the cloud actually runs (sec 6.1/6.2): a route table the
//     router scans per request, a sha256 of the presented key plus a cache lookup, and a provider
//     protocol whose response scanner tees the body aside. Budgeting only the single-origin path
//     would leave the deployment customers touch unmeasured.
func TestOverheadBudget(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping latency budget in -short mode")
	}

	const (
		warmup = 200
		n      = 3000
	)

	t.Run("single-origin", func(t *testing.T) {
		origin := httptest.NewServer(tinyUpstream())
		defer origin.Close()

		cfg, err := config.FromEnv(func(k string) string {
			if k == "EDGE_PROXY_UPSTREAM" {
				return origin.URL
			}
			return ""
		})
		if err != nil {
			t.Fatalf("config: %v", err)
		}
		front := httptest.NewServer(New(cfg)) // NopSink: zero telemetry overhead on the hot path
		defer front.Close()

		client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4}}
		direct := measure(t, client, origin.URL+"/v1/models", warmup, n)
		proxied := measure(t, client, front.URL+"/v1/models", warmup, n)
		checkBudget(t, "single-origin", direct, proxied)
	})

	t.Run("hosted", func(t *testing.T) {
		origin := httptest.NewServer(tinyUpstream())
		defer origin.Close()

		// The loopback host the test client will send, placed last in the table so the router pays a
		// full scan past the decoy routes on every request, as it does behind real hostnames.
		const tenantKey = "tally_sk_live_overhead"
		cfg := config.Config{
			TenantHeader:        "X-Tenant-Key",
			FeatureTagHeader:    "X-Tally-Feature-Tag",
			AccountIdHashHeader: "X-Tally-Account-Id-Hash",
			RequireTenant:       true,
			RouteMode:           config.RouteModeHost,
			Routes: []config.Route{
				{Match: "openai.proxy.test", Upstream: mustURL(t, origin.URL), Provider: config.ProviderOpenAI},
				{Match: "anthropic.proxy.test", Upstream: mustURL(t, origin.URL), Provider: config.ProviderAnthropic},
				{Match: "gemini.proxy.test", Upstream: mustURL(t, origin.URL), Provider: config.ProviderGemini},
				{Match: "127.0.0.1", Upstream: mustURL(t, origin.URL), Provider: config.ProviderOpenAI},
			},
		}
		resolver := staticResolver{edgekeys.HashKey(tenantKey): "uuid-overhead"}
		front := httptest.NewServer(New(cfg, WithKeyResolver(resolver)))
		defer front.Close()

		header := http.Header{}
		header.Set("X-Tenant-Key", tenantKey)
		header.Set("X-Tally-Feature-Tag", "overhead-probe")

		client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4}}
		direct := measure(t, client, origin.URL+"/v1/models", warmup, n)
		proxied := measureWith(t, client, front.URL+"/v1/models", header, warmup, n)
		checkBudget(t, "hosted", direct, proxied)
	})
}

// BenchmarkProxyOverhead reports ns/op for a single proxied round-trip against a loopback upstream.
// Run: go test -bench=ProxyOverhead -benchmem ./internal/proxy/
func BenchmarkProxyOverhead(b *testing.B) {
	origin := httptest.NewServer(tinyUpstream())
	defer origin.Close()

	cfg, err := config.FromEnv(func(k string) string {
		if k == "EDGE_PROXY_UPSTREAM" {
			return origin.URL
		}
		return ""
	})
	if err != nil {
		b.Fatalf("config: %v", err)
	}
	front := httptest.NewServer(New(cfg))
	defer front.Close()

	client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4}}
	url := front.URL + "/v1/models"

	// Warm the connection pool.
	for i := 0; i < 50; i++ {
		resp, err := client.Get(url)
		if err != nil {
			b.Fatalf("warmup: %v", err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		resp, err := client.Get(url)
		if err != nil {
			b.Fatalf("request: %v", err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
}
