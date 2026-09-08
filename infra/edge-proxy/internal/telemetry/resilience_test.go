// SPDX-License-Identifier: Apache-2.0
// Tests for the two telemetry holes an end-to-end integration run surfaced: an envelope that
// claimed no tenant (so a gateway with auth disabled refused every batch), and a sink that threw a
// record away on the first sign of gateway backpressure.
package telemetry

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// --- Bug 1: the default self-hosted config shipped an empty tenant_id ---------------------------

// tenantOf pulls the envelope's tenant claim out of an encoded batch.
func tenantOf(t *testing.T, body []byte) string {
	t.Helper()
	var batch map[string]any
	if err := json.Unmarshal(body, &batch); err != nil {
		t.Fatalf("bad batch json: %v", err)
	}
	v, ok := batch["tenant_id"].(string)
	if !ok {
		t.Fatalf("tenant_id missing or not a string: %v", batch["tenant_id"])
	}
	return v
}

// captureCollector records the last body it received and answers 200.
func captureCollector(body *[]byte, mu *sync.Mutex) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		*body = b
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
}

// TestEnvelopeClaimsConfiguredTenantWhenUnresolved is the fix for the default self-host path: with
// no edge-key feed the record resolves no tenant, and a gateway running with auth disabled needs an
// explicit one in the envelope or it refuses the batch with 422.
func TestEnvelopeClaimsConfiguredTenantWhenUnresolved(t *testing.T) {
	const operatorTenant = "9d4b2c11-0000-4000-8000-0000000000aa"
	var (
		mu   sync.Mutex
		body []byte
	)
	srv := captureCollector(&body, &mu)
	defer srv.Close()

	rec := sampleRecord()
	rec.TenantKey = "" // no edge-key feed: nothing resolved
	rec.TenantId = ""
	sink := NewHTTPSink(Options{
		URL:         srv.URL,
		Deployment:  DeploymentSelfHost,
		IngestToken: "svc_token",
		TenantId:    operatorTenant,
	})
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if got := tenantOf(t, body); got != operatorTenant {
		t.Fatalf("tenant_id = %q, want the configured tenant %q", got, operatorTenant)
	}
}

// TestResolvedTenantWinsOverConfiguredOne: the configured tenant is a fallback for unresolved
// records only. A record the edge-key cache resolved keeps its own tenant, or a hosted
// multi-tenant proxy would file one org's spend under the operator's.
func TestResolvedTenantWinsOverConfiguredOne(t *testing.T) {
	var (
		mu   sync.Mutex
		body []byte
	)
	srv := captureCollector(&body, &mu)
	defer srv.Close()

	rec := sampleRecord() // carries a resolved TenantId
	sink := NewHTTPSink(Options{
		URL:        srv.URL,
		Deployment: DeploymentCloud,
		TenantId:   "9d4b2c11-0000-4000-8000-0000000000aa",
	})
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if got := tenantOf(t, body); got != rec.TenantId {
		t.Fatalf("tenant_id = %q, want the record's resolved tenant %q", got, rec.TenantId)
	}
}

// TestNoTenantIsNeverFabricated: with nothing configured and nothing resolved the envelope claims
// no tenant. A placeholder would be worse than none, since a wrong tenant silently files spend into
// the wrong account instead of failing visibly.
func TestNoTenantIsNeverFabricated(t *testing.T) {
	var (
		mu   sync.Mutex
		body []byte
	)
	srv := captureCollector(&body, &mu)
	defer srv.Close()

	rec := sampleRecord()
	rec.TenantKey = ""
	rec.TenantId = ""
	sink := NewHTTPSink(Options{URL: srv.URL, Deployment: DeploymentSelfHost, IngestToken: "svc_token"})
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if got := tenantOf(t, body); got != "" {
		t.Fatalf("tenant_id = %q, want an empty claim rather than an invented one", got)
	}
}

// TestEmptyTenant422IsEscalatedLoudly is the failure the integration run actually hit. The
// gateway's 422 is the only place this misconfiguration is knowable, so the sink names the fix
// rather than logging a bare status code, says it once, and counts the loss.
func TestEmptyTenant422IsEscalatedLoudly(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"tenant_id required when auth is disabled"}`))
	}))
	defer srv.Close()

	var (
		mu    sync.Mutex
		lines []string
	)
	rec := sampleRecord()
	rec.TenantKey = ""
	rec.TenantId = ""
	sink := NewHTTPSink(Options{
		URL:         srv.URL,
		Deployment:  DeploymentSelfHost,
		IngestToken: "svc_token",
		Logf: func(format string, args ...any) {
			mu.Lock()
			lines = append(lines, fmt.Sprintf(format, args...))
			mu.Unlock()
		},
	})
	sink.Record(rec)
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	hints := 0
	for _, l := range lines {
		if strings.Contains(l, "EDGE_PROXY_TENANT_ID") {
			hints++
		}
	}
	if hints != 1 {
		t.Fatalf("want exactly one actionable EDGE_PROXY_TENANT_ID hint, got %d in %v", hints, lines)
	}
	if got := sink.Rejected(); got != 2 {
		t.Fatalf("Rejected() = %d, want 2 (the refusal must be counted, not swallowed)", got)
	}
}

// TestNoTenantHintWhenTenantWasClaimed: a 422 on a batch that DID claim a tenant is a different
// problem, and pointing at EDGE_PROXY_TENANT_ID would send the operator down a dead end.
func TestNoTenantHintWhenTenantWasClaimed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
	}))
	defer srv.Close()

	var (
		mu    sync.Mutex
		lines []string
	)
	sink := NewHTTPSink(Options{
		URL:        srv.URL,
		Deployment: DeploymentCloud,
		Logf: func(format string, args ...any) {
			mu.Lock()
			lines = append(lines, fmt.Sprintf(format, args...))
			mu.Unlock()
		},
	})
	sink.Record(sampleRecord()) // resolved tenant, so the claim is non-empty
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	for _, l := range lines {
		if strings.Contains(l, "EDGE_PROXY_TENANT_ID") {
			t.Fatalf("hinted at an empty tenant on a batch that claimed one: %v", lines)
		}
	}
}

// --- Bug 2: telemetry was dropped on the first sign of gateway backpressure ---------------------

// fastRetry keeps the bounded-retry shape (attempt cap, growth, jitter) while making tests cheap.
func fastRetry() *RetryPolicy {
	return &RetryPolicy{
		MaxAttempts: 3,
		Base:        5 * time.Millisecond,
		Max:         20 * time.Millisecond,
		Jitter:      0.25,
	}
}

// TestRetriesRetryableStatusesThenShedsAndCounts: a 429 and a 5xx are transient, so the same bytes
// are resent up to the cap. Past the cap the record is shed and counted, never held forever and
// never silently forgotten.
func TestRetriesRetryableStatusesThenShedsAndCounts(t *testing.T) {
	for name, status := range map[string]int{
		"429 backpressure": http.StatusTooManyRequests,
		"503 server fault": http.StatusServiceUnavailable,
	} {
		t.Run(name, func(t *testing.T) {
			var (
				mu     sync.Mutex
				bodies [][]byte
			)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				b, _ := io.ReadAll(r.Body)
				mu.Lock()
				bodies = append(bodies, b)
				mu.Unlock()
				w.WriteHeader(status)
			}))
			defer srv.Close()

			policy := fastRetry()
			sink := NewHTTPSink(Options{
				URL:         srv.URL,
				Deployment:  DeploymentCloud,
				IngestToken: "svc_token",
				Retry:       policy,
				Logf:        func(string, ...any) {},
			})
			start := time.Now()
			sink.Record(sampleRecord())
			sink.Close()
			elapsed := time.Since(start)

			mu.Lock()
			defer mu.Unlock()
			if len(bodies) != policy.MaxAttempts {
				t.Fatalf("collector saw %d attempts, want the %d-attempt cap",
					len(bodies), policy.MaxAttempts)
			}
			// Resent byte for byte: batch_id stays stable, so the gateway's (tenant_id, batch_id)
			// idempotency key turns a retry into a replay rather than a duplicate span.
			for i, b := range bodies[1:] {
				if string(b) != string(bodies[0]) {
					t.Fatalf("attempt %d resent a different body, breaking batch_id idempotency", i+2)
				}
			}
			// Backoff, not a hot loop: two waits of at least Base each separate three attempts,
			// discounted by the jitter floor.
			minWait := time.Duration(float64(policy.Base)*(1-policy.Jitter)) * 2
			if elapsed < minWait {
				t.Fatalf("retried in %s, want at least %s of backoff", elapsed, minWait)
			}
			if got := sink.Undelivered(); got != 1 {
				t.Fatalf("Undelivered() = %d, want 1 shed record after the budget was spent", got)
			}
			if got := sink.Dropped(); got != 0 {
				t.Fatalf("Dropped() = %d, want 0 (the buffer was never full)", got)
			}
		})
	}
}

// TestRetryStopsOnFirstSuccess: the budget exists for transient failures only, so a batch the
// gateway accepts on the second attempt is not sent a third time and is not counted as loss.
func TestRetryStopsOnFirstSuccess(t *testing.T) {
	var (
		mu   sync.Mutex
		hits int
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		hits++
		n := hits
		mu.Unlock()
		if n == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"accepted","accepted_spans":1}`))
	}))
	defer srv.Close()

	sink := NewHTTPSink(Options{
		URL:         srv.URL,
		Deployment:  DeploymentCloud,
		IngestToken: "svc_token",
		Retry:       fastRetry(),
		Logf:        func(string, ...any) {},
	})
	sink.Record(sampleRecord())
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if hits != 2 {
		t.Fatalf("collector saw %d attempts, want 2 (retry once, then stop on success)", hits)
	}
	if got := sink.Undelivered(); got != 0 {
		t.Fatalf("Undelivered() = %d, want 0 after a delivered batch", got)
	}
	if got := sink.Rejected(); got != 0 {
		t.Fatalf("Rejected() = %d, want 0 after a fully accepted batch", got)
	}
}

// TestNonRetryable4xxIsNotRetried: a validation refusal, a bad credential or a wrong scope is
// deterministic. Resending identical bytes cannot change the answer, so it stays a single attempt.
func TestNonRetryable4xxIsNotRetried(t *testing.T) {
	for name, status := range map[string]int{
		"422 validation":     http.StatusUnprocessableEntity,
		"401 bad credential": http.StatusUnauthorized,
		"403 wrong scope":    http.StatusForbidden,
		"400 bad protocol":   http.StatusBadRequest,
	} {
		t.Run(name, func(t *testing.T) {
			var (
				mu   sync.Mutex
				hits int
			)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				mu.Lock()
				hits++
				mu.Unlock()
				w.WriteHeader(status)
			}))
			defer srv.Close()

			sink := NewHTTPSink(Options{
				URL:         srv.URL,
				Deployment:  DeploymentCloud,
				IngestToken: "svc_token",
				Retry:       fastRetry(),
				Logf:        func(string, ...any) {},
			})
			sink.Record(sampleRecord())
			sink.Close()

			mu.Lock()
			defer mu.Unlock()
			if hits != 1 {
				t.Fatalf("collector saw %d attempts for %d, want exactly 1", hits, status)
			}
			if got := sink.Undelivered(); got != 0 {
				t.Fatalf("Undelivered() = %d, want 0: a refused batch is rejected, not undelivered", got)
			}
		})
	}
}

// TestRetriesTransportFailure: a connection that never reached the gateway is the most retryable
// failure there is, and before this change it was logged and forgotten without even a counter.
func TestRetriesTransportFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // nothing is listening now

	sink := NewHTTPSink(Options{
		URL:         url,
		Deployment:  DeploymentCloud,
		IngestToken: "svc_token",
		Retry:       fastRetry(),
		Logf:        func(string, ...any) {},
	})
	sink.Record(sampleRecord())
	sink.Close()

	if got := sink.Undelivered(); got != 1 {
		t.Fatalf("Undelivered() = %d, want 1 after an unreachable collector", got)
	}
}

// TestRetryAfterIsHonoredButCapped: the gateway asks for a pause on a 429. We honor it, but a
// server asking for minutes must not stall the worker, so it is clamped to the policy's Max.
func TestRetryAfterIsHonoredButCapped(t *testing.T) {
	p := RetryPolicy{MaxAttempts: 2, Base: time.Millisecond, Max: 50 * time.Millisecond, Jitter: 0}
	if got := p.delay(1, 300*time.Second); got != p.Max {
		t.Fatalf("delay with a 5 minute Retry-After = %s, want the %s cap", got, p.Max)
	}
	if got := p.delay(1, 20*time.Millisecond); got != 20*time.Millisecond {
		t.Fatalf("delay = %s, want the server's 20ms", got)
	}
	if got := parseRetryAfter("Wed, 21 Oct 2015 07:28:00 GMT"); got != 0 {
		t.Fatalf("parseRetryAfter(http-date) = %s, want 0 so our own backoff applies", got)
	}
	if got := parseRetryAfter("2"); got != 2*time.Second {
		t.Fatalf("parseRetryAfter(%q) = %s, want 2s", "2", got)
	}
}

// TestBackoffGrowsAndIsCapped: exponential growth bounded by Max, which is what keeps a persistent
// outage from turning the worker into an unbounded sleep.
func TestBackoffGrowsAndIsCapped(t *testing.T) {
	p := RetryPolicy{MaxAttempts: 10, Base: 10 * time.Millisecond, Max: 40 * time.Millisecond, Jitter: 0}
	want := []time.Duration{10, 20, 40, 40, 40}
	for i, w := range want {
		if got := p.delay(i+1, 0); got != w*time.Millisecond {
			t.Fatalf("delay(%d) = %s, want %s", i+1, got, w*time.Millisecond)
		}
	}
}

// TestCloseIsNotHeldOpenByRetries: shutdown must not wait out the retry budget of a buffer full of
// records aimed at a dead gateway. The remaining attempts are abandoned, and the records are shed
// and counted rather than reported as shipped.
func TestCloseIsNotHeldOpenByRetries(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	slow := &RetryPolicy{MaxAttempts: 5, Base: 5 * time.Second, Max: 10 * time.Second, Jitter: 0}
	sink := NewHTTPSink(Options{
		URL:         srv.URL,
		Deployment:  DeploymentCloud,
		IngestToken: "svc_token",
		Retry:       slow,
		Logf:        func(string, ...any) {},
	})
	sink.Record(sampleRecord())
	start := time.Now()
	sink.Close()
	if elapsed := time.Since(start); elapsed > 4*time.Second {
		t.Fatalf("Close took %s, want it to abandon the backoff rather than sleep it out", elapsed)
	}
	if got := sink.Undelivered(); got != 1 {
		t.Fatalf("Undelivered() = %d, want the abandoned record counted as loss", got)
	}
}
