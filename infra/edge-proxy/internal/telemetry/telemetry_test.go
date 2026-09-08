// SPDX-License-Identifier: Apache-2.0
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

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

func i64(v int64) *int64 { return &v }

func sampleRecord() proxy.TraceRecord {
	return proxy.TraceRecord{
		TenantKey:        "tk_live_acme",
		TenantId:         "7f1c3a2e-0000-4000-8000-000000000001",
		FeatureTag:       "support-bot",
		AccountIdHash:    "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
		Method:           "POST",
		Path:             "/v1/chat/completions",
		Provider:         "openai",
		Model:            "gpt-4o-2024-08-06",
		PromptTokens:     i64(12),
		CompletionTokens: i64(34),
		StatusCode:       200,
		ReqBytes:         512,
		RespBytes:        4096,
		Duration:         1500 * time.Millisecond,
		StartedAt:        time.Unix(1_700_000_000, 123),
		Failed:           false,
	}
}

// fixedIds pins the per-batch identifiers and send timestamp so two encodings of the same record
// are comparable byte for byte.
func fixedIds() ids {
	n := 0
	return ids{
		newHex: func(nBytes int) string {
			n++
			return fmt.Sprintf("%0*d", nBytes*2, n)
		},
		nowNs: func() int64 { return 1_700_000_001_000_000_000 },
	}
}

// decodeSpan pulls the single span out of an encoded batch.
func decodeSpan(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var batch map[string]any
	if err := json.Unmarshal(body, &batch); err != nil {
		t.Fatalf("bad batch json: %v", err)
	}
	spans, ok := batch["resource_spans"].([]any)
	if !ok || len(spans) != 1 {
		t.Fatalf("want exactly one resource span, got %v", batch["resource_spans"])
	}
	span, ok := spans[0].(map[string]any)
	if !ok {
		t.Fatalf("span is not an object: %v", spans[0])
	}
	return span
}

// TestParitySelfHostMatchesCloud is the CTO-43 acceptance test: a self-hosted proxy must emit
// telemetry identical to the cloud proxy. We encode the same record under both deployment labels
// and assert every field matches except the deployment label on the span.
func TestParitySelfHostMatchesCloud(t *testing.T) {
	rec := sampleRecord()

	cloud, err := encode(DeploymentCloud, rec, fixedIds())
	if err != nil {
		t.Fatalf("encode cloud: %v", err)
	}
	self, err := encode(DeploymentSelfHost, rec, fixedIds())
	if err != nil {
		t.Fatalf("encode self-host: %v", err)
	}

	cloudSpan := decodeSpan(t, cloud)
	selfSpan := decodeSpan(t, self)

	if cloudSpan["tally.deployment"] != "cloud" || selfSpan["tally.deployment"] != "self-host" {
		t.Fatalf("deployment labels wrong: %v / %v",
			cloudSpan["tally.deployment"], selfSpan["tally.deployment"])
	}

	delete(cloudSpan, "tally.deployment")
	delete(selfSpan, "tally.deployment")
	if len(cloudSpan) != len(selfSpan) {
		t.Fatalf("field count differs: cloud %d, self-host %d", len(cloudSpan), len(selfSpan))
	}
	for k, v := range cloudSpan {
		if selfSpan[k] != v {
			t.Fatalf("field %q differs: cloud %v, self-host %v", k, v, selfSpan[k])
		}
	}
}

// TestEncodeCarriesNoBodyContent guards the metadata-only invariant: the wire format must not grow
// a field that could carry a prompt, completion, or a credential. The tenant key in particular is
// an ai-tally credential and belongs on the Authorization header, never in the payload.
func TestEncodeCarriesNoBodyContent(t *testing.T) {
	body, err := Encode(DeploymentCloud, sampleRecord())
	if err != nil {
		t.Fatal(err)
	}
	var batch map[string]any
	if err := json.Unmarshal(body, &batch); err != nil {
		t.Fatal(err)
	}
	allowedEnvelope := map[string]bool{
		"tenant_id": true, "sdk_version": true, "resource_spans": true,
		"batch_id": true, "client_send_ts_ns": true,
	}
	for k := range batch {
		if !allowedEnvelope[k] {
			t.Fatalf("unexpected envelope field %q in telemetry wire format", k)
		}
	}

	allowedSpan := map[string]bool{
		"timestamp_ns": true, "trace_id": true, "span_id": true,
		"service_name": true, "span_name": true, "status_code": true, "duration_ns": true,
		"gen_ai.operation.name": true, "gen_ai.system": true, "gen_ai.response.model": true,
		"gen_ai.usage.input_tokens": true, "gen_ai.usage.output_tokens": true,
		"gen_ai.feature_tag": true,
		// account_id_hash is a hash, not an identifier: it cannot carry a name or a body (CTO-182).
		"gen_ai.account_id_hash": true,
		"tally.deployment":       true,
		"http.request.method":    true, "url.path": true, "http.response.status_code": true,
		"http.request.body.size": true, "http.response.body.size": true,
		"tally.upstream_failed": true,
	}
	for k := range decodeSpan(t, body) {
		if !allowedSpan[k] {
			t.Fatalf("unexpected span field %q in telemetry wire format", k)
		}
	}

	if string(body) == "" {
		t.Fatal("empty payload")
	}
	if got := batch["tenant_id"]; got != "7f1c3a2e-0000-4000-8000-000000000001" {
		t.Fatalf("tenant_id = %v, want the resolved UUID", got)
	}
}

// TestEncodeCarriesResolvedFields is the Initiative 2 sec 6.3 regression: the fields the request
// path resolves (tenant UUID, model, token counts) must actually reach the wire. Before this they
// were computed and thrown away, so no proxy traffic could ever be costed or attributed.
func TestEncodeCarriesResolvedFields(t *testing.T) {
	body, err := Encode(DeploymentCloud, sampleRecord())
	if err != nil {
		t.Fatal(err)
	}
	var batch map[string]any
	if err := json.Unmarshal(body, &batch); err != nil {
		t.Fatal(err)
	}
	if batch["tenant_id"] != "7f1c3a2e-0000-4000-8000-000000000001" {
		t.Errorf("tenant_id = %v", batch["tenant_id"])
	}
	span := decodeSpan(t, body)
	if span["gen_ai.response.model"] != "gpt-4o-2024-08-06" {
		t.Errorf("model = %v", span["gen_ai.response.model"])
	}
	if span["gen_ai.system"] != "openai" {
		t.Errorf("system = %v", span["gen_ai.system"])
	}
	if span["gen_ai.usage.input_tokens"] != float64(12) {
		t.Errorf("input tokens = %v, want 12", span["gen_ai.usage.input_tokens"])
	}
	if span["gen_ai.usage.output_tokens"] != float64(34) {
		t.Errorf("output tokens = %v, want 34", span["gen_ai.usage.output_tokens"])
	}
	if span["gen_ai.feature_tag"] != "support-bot" {
		t.Errorf("feature tag = %v", span["gen_ai.feature_tag"])
	}
}

// TestUnknownTokensSerializeAsNullNeverZero is the honest-under-uncertainty guard. A streamed
// response (or one past the metadata scan cap) reports no usage; that span must reach storage with
// NULL token counts. Emitting 0 would read downstream as a real call that consumed nothing.
func TestUnknownTokensSerializeAsNullNeverZero(t *testing.T) {
	rec := sampleRecord()
	rec.PromptTokens = nil
	rec.CompletionTokens = nil
	rec.Model = "" // model extraction can fail on the same responses

	body, err := Encode(DeploymentCloud, rec)
	if err != nil {
		t.Fatal(err)
	}
	span := decodeSpan(t, body)

	for _, k := range []string{"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.response.model"} {
		v, present := span[k]
		if present {
			t.Errorf("%s present as %#v; unknown must be omitted (NULL), never a value", k, v)
		}
	}
	// Belt and braces against a future struct-tag slip: the raw JSON must not contain a zeroed count.
	if s := string(body); strings.Contains(s, `"gen_ai.usage.input_tokens":0`) ||
		strings.Contains(s, `"gen_ai.usage.output_tokens":0`) {
		t.Fatalf("unknown token count serialized as 0: %s", s)
	}
}

// TestZeroTokensStillSerialize: a count the provider genuinely reported as 0 is a fact and must
// survive. This is the other half of the nullable contract: omitempty keys off nil, not off 0.
func TestZeroTokensStillSerialize(t *testing.T) {
	rec := sampleRecord()
	rec.PromptTokens = i64(0)
	rec.CompletionTokens = i64(0)

	body, err := Encode(DeploymentCloud, rec)
	if err != nil {
		t.Fatal(err)
	}
	span := decodeSpan(t, body)
	if v, ok := span["gen_ai.usage.input_tokens"]; !ok || v != float64(0) {
		t.Errorf("a reported 0 must serialize, got %#v (present=%v)", v, ok)
	}
	if v, ok := span["gen_ai.usage.output_tokens"]; !ok || v != float64(0) {
		t.Errorf("a reported 0 must serialize, got %#v (present=%v)", v, ok)
	}
}

func TestHTTPSinkPostsRecord(t *testing.T) {
	var (
		mu       sync.Mutex
		received [][]byte
		gotAuth  string
		gotProto string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("content-type = %q", r.Header.Get("Content-Type"))
		}
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		received = append(received, b)
		gotAuth = r.Header.Get("Authorization")
		gotProto = r.Header.Get("X-Ingest-Protocol")
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
	if m["sdk_version"] != SDKVersion {
		t.Fatalf("unexpected payload: %v", m)
	}
	if gotAuth != "Bearer tk_live_acme" {
		t.Fatalf("Authorization = %q, want the record's tenant key as a bearer", gotAuth)
	}
	if gotProto != IngestProtocol {
		t.Fatalf("X-Ingest-Protocol = %q, want %q", gotProto, IngestProtocol)
	}
	// The credential authenticates the POST and must never appear in the body.
	if strings.Contains(string(received[0]), "tk_live_acme") {
		t.Fatalf("tenant key leaked into the telemetry body: %s", received[0])
	}
}

// TestHTTPSinkFallsBackToIngestToken covers the single-tenant self-host where requests carry no
// tenant key: the configured ingest credential authenticates instead.
func TestHTTPSinkFallsBackToIngestToken(t *testing.T) {
	var (
		mu      sync.Mutex
		gotAuth string
		hits    int
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotAuth = r.Header.Get("Authorization")
		hits++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	rec := sampleRecord()
	rec.TenantKey = ""
	sink := NewHTTPSink(Options{URL: srv.URL, Deployment: DeploymentSelfHost, IngestToken: "svc_token"})
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if hits != 1 {
		t.Fatalf("collector got %d posts, want 1", hits)
	}
	if gotAuth != "Bearer svc_token" {
		t.Fatalf("Authorization = %q, want the configured ingest token", gotAuth)
	}
}

// TestHTTPSinkShedsUnauthenticatedRecords: with no tenant key and no configured token there is
// nothing to authenticate with. The sink counts the loss honestly instead of POSTing a batch that
// cannot be attributed.
func TestHTTPSinkShedsUnauthenticatedRecords(t *testing.T) {
	var hits int
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		hits++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	rec := sampleRecord()
	rec.TenantKey = ""
	sink := NewHTTPSink(Options{URL: srv.URL, Deployment: DeploymentCloud})
	sink.Record(rec)
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if hits != 0 {
		t.Fatalf("posted %d unauthenticated batches, want 0", hits)
	}
	if got := sink.Unauthenticated(); got != 1 {
		t.Fatalf("Unauthenticated() = %d, want 1", got)
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
