// SPDX-License-Identifier: Apache-2.0
// Package telemetry ships the proxy's metadata-only TraceRecords to an ai-tally collector.
//
// This is the real Sink that CTO-39 left as a NopSink. It exists here (rather than in package
// proxy) so the self-hostable binary (CTO-43) and the cloud binary share one wire format: a
// self-hosted proxy in a customer VPC emits byte-identical telemetry to the cloud proxy, differing
// only in the `deployment` label. Encode is the single source of truth for that format, so the
// parity guarantee is structural — both deployments call the same encoder.
//
// Like the rest of the proxy, the record carries only metadata + byte counts, never request or
// response content and never the customer's provider key.
package telemetry

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

// Deployment labels where a record was produced so the cloud can tell self-hosted ingest apart.
type Deployment string

const (
	DeploymentCloud    Deployment = "cloud"
	DeploymentSelfHost Deployment = "self-host"
)

// wireRecord is the JSON envelope posted to the collector. The field set is the full TraceRecord
// plus the deployment label; nothing else. Durations are serialized as integer nanoseconds and
// timestamps as Unix nanoseconds so the format is language-neutral and stable.
type wireRecord struct {
	Deployment  Deployment `json:"deployment"`
	TenantKey   string     `json:"tenant_key"`
	FeatureTag  string     `json:"feature_tag"`
	Method      string     `json:"method"`
	Path        string     `json:"path"`
	StatusCode  int        `json:"status_code"`
	ReqBytes    int64      `json:"req_bytes"`
	RespBytes   int64      `json:"resp_bytes"`
	DurationNs  int64      `json:"duration_ns"`
	StartedAtNs int64      `json:"started_at_ns"`
	Failed      bool       `json:"failed"`
}

func toWire(dep Deployment, rec proxy.TraceRecord) wireRecord {
	return wireRecord{
		Deployment:  dep,
		TenantKey:   rec.TenantKey,
		FeatureTag:  rec.FeatureTag,
		Method:      rec.Method,
		Path:        rec.Path,
		StatusCode:  rec.StatusCode,
		ReqBytes:    rec.ReqBytes,
		RespBytes:   rec.RespBytes,
		DurationNs:  rec.Duration.Nanoseconds(),
		StartedAtNs: rec.StartedAt.UnixNano(),
		Failed:      rec.Failed,
	}
}

// Encode serializes one record to its canonical wire JSON. Both the cloud and self-hosted proxy
// call this, so given identical inputs the output differs only in the deployment field — which is
// exactly the parity property CTO-43 requires.
func Encode(dep Deployment, rec proxy.TraceRecord) ([]byte, error) {
	return json.Marshal(toWire(dep, rec))
}

// HTTPSink is a proxy.Sink that batches records and POSTs them to a collector URL out of band of
// the request hot path. Record never blocks the proxy: it drops onto a buffered channel and a
// background worker flushes. If the buffer is full (collector down / slow), records are dropped
// rather than back-pressuring customer traffic — telemetry must never degrade the proxied call.
type HTTPSink struct {
	url        string
	deployment Deployment
	client     *http.Client
	ch         chan proxy.TraceRecord

	wg       sync.WaitGroup
	closeOne sync.Once

	// dropped counts records shed because the buffer was full (observability for the operator).
	mu      sync.Mutex
	dropped int64
}

// Options configures an HTTPSink.
type Options struct {
	// URL is the collector ingest endpoint records are POSTed to.
	URL string
	// Deployment labels every record (cloud vs self-host).
	Deployment Deployment
	// Buffer is the channel depth; defaults to 1024.
	Buffer int
	// Client overrides the HTTP client (mainly for tests); defaults to a short-timeout client.
	Client *http.Client
}

// NewHTTPSink builds and starts an HTTPSink. Call Close to flush and stop the worker.
func NewHTTPSink(o Options) *HTTPSink {
	buf := o.Buffer
	if buf <= 0 {
		buf = 1024
	}
	client := o.Client
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	dep := o.Deployment
	if dep == "" {
		dep = DeploymentCloud
	}
	s := &HTTPSink{
		url:        o.URL,
		deployment: dep,
		client:     client,
		ch:         make(chan proxy.TraceRecord, buf),
	}
	s.wg.Add(1)
	go s.run()
	return s
}

// Record implements proxy.Sink. It is non-blocking: a full buffer drops the record.
func (s *HTTPSink) Record(rec proxy.TraceRecord) {
	select {
	case s.ch <- rec:
	default:
		s.mu.Lock()
		s.dropped++
		s.mu.Unlock()
	}
}

// Dropped returns the number of records shed due to a full buffer.
func (s *HTTPSink) Dropped() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dropped
}

func (s *HTTPSink) run() {
	defer s.wg.Done()
	for rec := range s.ch {
		s.post(rec)
	}
}

func (s *HTTPSink) post(rec proxy.TraceRecord) {
	body, err := Encode(s.deployment, rec)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.client.Do(req)
	if err != nil {
		log.Printf("edge-proxy telemetry: post failed: %v", err)
		return
	}
	// Drain and close so the connection can be reused from the pool.
	_ = resp.Body.Close()
}

// Close stops accepting records and waits for the worker to drain the buffer.
func (s *HTTPSink) Close() {
	s.closeOne.Do(func() { close(s.ch) })
	s.wg.Wait()
}
