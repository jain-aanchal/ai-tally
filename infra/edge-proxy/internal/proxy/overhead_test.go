package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
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
	for i := 0; i < warmup; i++ {
		resp, err := client.Get(url)
		if err != nil {
			t.Fatalf("warmup request: %v", err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	out := make([]time.Duration, 0, n)
	for i := 0; i < n; i++ {
		start := time.Now()
		resp, err := client.Get(url)
		if err != nil {
			t.Fatalf("request: %v", err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		out = append(out, time.Since(start))
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// TestOverheadBudget enforces the CTO-39 acceptance criterion: p99 added latency < 3ms.
//
// Both paths hit the same loopback upstream, so subtracting the direct baseline isolates the
// proxy's own processing. This runs on every `go test`, so a regression that fattens the hot path
// (e.g. buffering a body, adding a sync allocation) trips CI rather than shipping silently.
func TestOverheadBudget(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping latency budget in -short mode")
	}

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

	const (
		warmup = 200
		n      = 3000
	)
	client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4}}

	direct := measure(t, client, origin.URL+"/v1/models", warmup, n)
	proxied := measure(t, client, front.URL+"/v1/models", warmup, n)

	// Overhead = proxied tail minus the direct typical service time.
	overheadP99 := percentile(proxied, 99) - percentile(direct, 50)
	if overheadP99 < 0 {
		overheadP99 = 0
	}

	t.Logf("direct  p50=%s p99=%s", percentile(direct, 50), percentile(direct, 99))
	t.Logf("proxied p50=%s p99=%s", percentile(proxied, 50), percentile(proxied, 99))
	t.Logf("added overhead p99=%s (budget 3ms)", overheadP99)

	const budget = 3 * time.Millisecond
	if overheadP99 >= budget {
		t.Errorf("p99 added overhead %s exceeds budget %s", overheadP99, budget)
	}
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
