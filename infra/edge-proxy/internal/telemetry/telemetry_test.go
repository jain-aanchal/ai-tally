// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

func sampleRecord() proxy.TraceRecord {
	return proxy.TraceRecord{
		TenantKey:  "tk_live_acme",
		Method:     "POST",
		Path:       "/v1/chat/completions",
		StatusCode: 200,
		ReqBytes:   512,
		RespBytes:  4096,
		Duration:   1500 * time.Millisecond,
		StartedAt:  time.Unix(1_700_000_000, 123),
		Failed:     false,
	}
}

// TestParitySelfHostMatchesCloud is the CTO-43 acceptance test: a self-hosted proxy must emit
// telemetry identical to the cloud proxy. We encode the same record under both deployment labels
// and assert every field matches except `deployment`.
func TestParitySelfHostMatchesCloud(t *testing.T) {
	rec := sampleRecord()

	cloud, err := Encode(DeploymentCloud, rec)
	if err != nil {
		t.Fatalf("encode cloud: %v", err)
	}
	self, err := Encode(DeploymentSelfHost, rec)
	if err != nil {
		t.Fatalf("encode self-host: %v", err)
	}

	var cloudMap, selfMap map[string]any
	if err := json.Unmarshal(cloud, &cloudMap); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(self, &selfMap); err != nil {
		t.Fatal(err)
	}

	if cloudMap["deployment"] != "cloud" || selfMap["deployment"] != "self-host" {
		t.Fatalf("deployment labels wrong: %v / %v", cloudMap["deployment"], selfMap["deployment"])
	}

	delete(cloudMap, "deployment")
	delete(selfMap, "deployment")
	if len(cloudMap) != len(selfMap) {
		t.Fatalf("field count differs: cloud %d, self-host %d", len(cloudMap), len(selfMap))
	}
	for k, v := range cloudMap {
		if selfMap[k] != v {
			t.Fatalf("field %q differs: cloud %v, self-host %v", k, v, selfMap[k])
		}
	}
}

// TestEncodeCarriesNoBodyContent guards the metadata-only invariant: the wire format must not grow
// a field that could carry a prompt, completion, or provider key.
func TestEncodeCarriesNoBodyContent(t *testing.T) {
	body, err := Encode(DeploymentCloud, sampleRecord())
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatal(err)
	}
	allowed := map[string]bool{
		"deployment": true, "tenant_key": true, "method": true, "path": true,
		"status_code": true, "req_bytes": true, "resp_bytes": true,
		"duration_ns": true, "started_at_ns": true, "failed": true,
	}
	for k := range m {
		if !allowed[k] {
			t.Fatalf("unexpected field %q in telemetry wire format", k)
		}
	}
}

func TestHTTPSinkPostsRecord(t *testing.T) {
	var (
		mu       sync.Mutex
		received [][]byte
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("content-type = %q", r.Header.Get("Content-Type"))
		}
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		received = append(received, b)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	sink := NewHTTPSink(Options{URL: srv.URL, Deployment: DeploymentSelfHost})
	sink.Record(sampleRecord())
	sink.Close() // flushes and waits

	mu.Lock()
	defer mu.Unlock()
	if len(received) != 1 {
		t.Fatalf("collector got %d records, want 1", len(received))
	}
	var m map[string]any
	if err := json.Unmarshal(received[0], &m); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if m["deployment"] != "self-host" || m["tenant_key"] != "tk_live_acme" {
		t.Fatalf("unexpected payload: %v", m)
	}
}

func TestHTTPSinkDropsWhenFull(t *testing.T) {
	// A blocking collector + buffer of 1 forces overflow; Record must never block.
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		<-release
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	sink := NewHTTPSink(Options{URL: srv.URL, Deployment: DeploymentCloud, Buffer: 1})
	// First record gets pulled by the worker (which then blocks on the collector); subsequent
	// records fill the buffer (1) then overflow and drop.
	for i := 0; i < 50; i++ {
		sink.Record(sampleRecord())
	}
	if got := sink.Dropped(); got == 0 {
		t.Fatal("expected some records to be dropped under a stalled collector")
	}
	close(release)
	sink.Close()
}
