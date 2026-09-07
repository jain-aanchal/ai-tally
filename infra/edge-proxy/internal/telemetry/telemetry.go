// SPDX-License-Identifier: Apache-2.0
// Package telemetry ships the proxy's metadata-only TraceRecords to the ai-tally ingest gateway.
//
// This is the real Sink that CTO-39 left as a NopSink. It exists here (rather than in package
// proxy) so the self-hostable binary (CTO-43) and the cloud binary share one wire format: a
// self-hosted proxy in a customer VPC emits byte-identical telemetry to the cloud proxy, differing
// only in the `deployment` label. Encode is the single source of truth for that format, so the
// parity guarantee is structural — both deployments call the same encoder.
//
// Initiative 2 sec 6.3 / sec 8: the destination is the gateway's existing POST /v1/batches, and the
// payload is a tally.wire.BatchRequest carrying one span. Reusing the SDK's ingest contract rather
// than inventing a proxy-only collector endpoint means proxied traffic runs the same authenticated,
// validated, cost-enriched, idempotent write path SDK traffic does, and lands in the same
// otel_spans columns. Nothing new is needed on the gateway side.
//
// Like the rest of the proxy, the record carries only metadata + byte counts, never request or
// response content and never the customer's provider key. The tenant's own ai-tally key
// authenticates the POST as an Authorization bearer and is deliberately NOT a body field: a
// credential travels by reference, never inside telemetry.
package telemetry

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

// Deployment labels where a record was produced so the cloud can tell self-hosted ingest apart.
type Deployment string

const (
	DeploymentCloud    Deployment = "cloud"
	DeploymentSelfHost Deployment = "self-host"
)

// SDKVersion identifies the producer in the batch envelope, so ingest can tell proxy-emitted spans
// from SDK-emitted ones without inspecting the span.
const SDKVersion = "edge-proxy/1"

// ServiceName is the ServiceName column value for every proxy-emitted span.
const ServiceName = "edge-proxy"

// IngestProtocol is the X-Ingest-Protocol version this encoder speaks (gateway/protocol.py).
const IngestProtocol = "ingest-v1"

// OTel status codes (semconv): the StatusCode column is 0=Unset, 1=Ok, 2=Error, which is what the
// dashboard's failure queries read (web/lib/clickhouse.ts). It is NOT the HTTP status; that ships
// separately as an attribute.
const (
	statusOk    = 1
	statusError = 2
)

// wireSpan is one span in the batch's resource_spans list. Field names are the gateway's ingest
// contract (gateway/mapping.py): structural keys plus gen_ai.* attributes, which the mapper
// promotes to typed otel_spans columns. Anything not promoted lands in the SpanAttributes map, so
// the proxy-only fields below (deployment, method, path, byte counts) are preserved without a
// schema change.
//
// Every optional field is omitempty on purpose. The gateway's validator rejects a known gen_ai
// string key present but empty, and more importantly an omitted field reads back as "unknown"
// while an empty or zero one would read back as a fact. Unknown must stay unknown.
type wireSpan struct {
	TimestampNs int64  `json:"timestamp_ns"`
	TraceId     string `json:"trace_id"`
	SpanId      string `json:"span_id"`
	ServiceName string `json:"service_name"`
	SpanName    string `json:"span_name"`
	StatusCode  int    `json:"status_code"`
	DurationNs  int64  `json:"duration_ns"`

	// Operation is always "chat": the proxy meters LLM API calls, and the value is lowercase because
	// the gateway's validator requires it.
	Operation string `json:"gen_ai.operation.name"`
	// System / ResponseModel are the (provider, model) pair the gateway prices against. Both are
	// omitted when unextracted (pure pass-through, streamed response) so the span is honestly
	// unpriceable rather than priced against a guess.
	System        string `json:"gen_ai.system,omitempty"`
	ResponseModel string `json:"gen_ai.response.model,omitempty"`
	// InputTokens / OutputTokens are pointers so an unknown count is omitted from the JSON entirely
	// rather than serialized as a fabricated 0. A count the provider genuinely reported as 0 still
	// serializes as 0, because omitempty on a pointer keys off nil, not off the pointed-to value.
	//
	// What the proxy guarantees is the wire: unknown is absent, never 0. It does NOT currently
	// guarantee a NULL in storage. The gateway's mapping coerces a missing count to 0 and the
	// ClickHouse columns are non-nullable UInt32, so today an omitted count still lands as 0 in
	// otel_spans. Making unknown survive as NULL end to end needs a gateway + schema change that is
	// tracked separately; this encoder is the half of it that can be honest without one.
	InputTokens  *int64 `json:"gen_ai.usage.input_tokens,omitempty"`
	OutputTokens *int64 `json:"gen_ai.usage.output_tokens,omitempty"`
	// FeatureTag / AccountIdHash mirror the same span attributes the Python SDK emits, so a proxied
	// request lands in the same FeatureTag / AccountIdHash columns (CTO-104, CTO-182). Omitted when
	// the caller did not tag or attribute the request: the unattributed bucket, not a customer
	// named "unknown".
	FeatureTag    string `json:"gen_ai.feature_tag,omitempty"`
	AccountIdHash string `json:"gen_ai.account_id_hash,omitempty"`

	// Long-tail attributes: not promoted to columns, kept in SpanAttributes. Counts and labels only.
	Deployment Deployment `json:"tally.deployment"`
	Method     string     `json:"http.request.method"`
	Path       string     `json:"url.path"`
	HTTPStatus int        `json:"http.response.status_code,omitempty"`
	ReqBytes   int64      `json:"http.request.body.size"`
	RespBytes  int64      `json:"http.response.body.size"`
	// UpstreamFailed marks a request the upstream never answered (the synthesized 502). Such a span
	// carries no HTTP status and no usage, which is the honest record of a call that produced
	// nothing, not a zero-cost success.
	UpstreamFailed bool `json:"tally.upstream_failed,omitempty"`
}

// wireBatch is the POST /v1/batches envelope (tally.wire.BatchRequest). The proxy sends one span
// per batch: records arrive one at a time off the async channel, and a per-record batch keeps the
// tenant boundary trivially correct, since a batch belongs to exactly one tenant.
type wireBatch struct {
	// TenantId is the UUID the edge key cache resolved (sec 6.3). The gateway treats the
	// authenticating key's tenant as authoritative and refuses a batch claiming a different one, so
	// this is a claim it verifies, never a way to write into another tenant. Empty (no resolver
	// configured) is accepted: the key still decides.
	TenantId       string     `json:"tenant_id"`
	SdkVersion     string     `json:"sdk_version"`
	ResourceSpans  []wireSpan `json:"resource_spans"`
	BatchId        string     `json:"batch_id"`
	ClientSendTsNs int64      `json:"client_send_ts_ns"`
}

// ids supplies the identifiers and send timestamp that vary per batch. It is injectable so the
// parity test can encode the same record twice and compare byte for byte.
type ids struct {
	newHex func(nBytes int) string
	nowNs  func() int64
}

func defaultIds() ids {
	return ids{newHex: randomHex, nowNs: func() int64 { return time.Now().UnixNano() }}
}

// randomHex returns nBytes of crypto-random data as lowercase hex. Trace and span ids only need to
// be unique; they carry no meaning and must not encode anything about the request.
//
// There is deliberately no error branch: crypto/rand.Read never returns an error and always fills
// the buffer entirely (it panics rather than degrading), so there is no partial-success case to
// handle. An earlier version returned the same all-zeros encoding on both paths, which would have
// been actively harmful here: batch_id also comes from this function, and a run of zero batch ids
// would collide on the gateway's (tenant_id, batch_id) idempotency key so every batch after the
// first is discarded as a replay for the cache TTL.
func randomHex(nBytes int) string {
	b := make([]byte, nBytes)
	rand.Read(b) //nolint:errcheck // documented never to fail; see above
	return hex.EncodeToString(b)
}

// genAISystem maps the proxy's route protocol label to the gen_ai.system value the rest of the
// product uses. openai and anthropic are already the catalog's provider strings, but Google models
// are catalogued under "google" (sdk/python/src/tally/pricing.py, where the comment states the
// catalog string IS the gen_ai.system value), and PriceCatalog._best compares the provider by exact
// string equality. Shipping the route label "gemini" verbatim would therefore miss the catalog and
// leave every hosted Gemini span permanently uncostable, and would split the provider dimension so
// one org's SDK Gemini spans and its proxy Gemini spans never aggregate together.
func genAISystem(provider string) string {
	if provider == string(config.ProviderGemini) {
		return "google"
	}
	return provider
}

func toWire(dep Deployment, rec proxy.TraceRecord, id ids) wireBatch {
	// OTel status: a 4xx/5xx or an unreachable upstream is an Error span, everything else Ok. The
	// raw HTTP status rides along as an attribute rather than being crammed into this column.
	status := statusOk
	if rec.Failed || rec.StatusCode >= 400 {
		status = statusError
	}
	span := wireSpan{
		TimestampNs:    rec.StartedAt.UnixNano(),
		TraceId:        id.newHex(16),
		SpanId:         id.newHex(8),
		ServiceName:    ServiceName,
		SpanName:       "llm.call",
		StatusCode:     status,
		DurationNs:     rec.Duration.Nanoseconds(),
		Operation:      "chat",
		System:         genAISystem(rec.Provider),
		ResponseModel:  rec.Model,
		InputTokens:    rec.PromptTokens,
		OutputTokens:   rec.CompletionTokens,
		FeatureTag:     rec.FeatureTag,
		AccountIdHash:  rec.AccountIdHash,
		Deployment:     dep,
		Method:         rec.Method,
		Path:           rec.Path,
		HTTPStatus:     rec.StatusCode,
		ReqBytes:       rec.ReqBytes,
		RespBytes:      rec.RespBytes,
		UpstreamFailed: rec.Failed,
	}
	return wireBatch{
		TenantId:       rec.TenantId,
		SdkVersion:     SDKVersion,
		ResourceSpans:  []wireSpan{span},
		BatchId:        id.newHex(16),
		ClientSendTsNs: id.nowNs(),
	}
}

// Encode serializes one record to its canonical wire JSON: a single-span POST /v1/batches request.
// Both the cloud and self-hosted proxy call this, so given identical inputs the output differs only
// in the deployment label, which is exactly the parity property CTO-43 requires.
func Encode(dep Deployment, rec proxy.TraceRecord) ([]byte, error) {
	return encode(dep, rec, defaultIds())
}

func encode(dep Deployment, rec proxy.TraceRecord, id ids) ([]byte, error) {
	return json.Marshal(toWire(dep, rec, id))
}

// HTTPSink is a proxy.Sink that batches records and POSTs them to a collector URL out of band of
// the request hot path. Record never blocks the proxy: it drops onto a buffered channel and a
// background worker flushes. If the buffer is full (collector down / slow), records are dropped
// rather than back-pressuring customer traffic — telemetry must never degrade the proxied call.
type HTTPSink struct {
	url         string
	deployment  Deployment
	ingestToken string
	client      *http.Client
	ch          chan proxy.TraceRecord
	logf        func(format string, args ...any)

	wg       sync.WaitGroup
	closeOne sync.Once
	done     chan struct{}

	// dropped counts records shed because the buffer was full, unauthenticated counts records shed
	// because no ingest credential was available for them, and rejected counts spans the gateway
	// declined per item (observability for the operator: all three are real telemetry loss and none
	// is silently papered over).
	mu              sync.Mutex
	dropped         int64
	unauthenticated int64
	rejected        int64
	// reported is the last snapshot logged, so a periodic report says what changed rather than
	// re-reporting a total that has stopped moving.
	reported Stats
}

// Stats is a snapshot of the sink's telemetry-loss counters.
type Stats struct {
	// Dropped is records shed because the buffer was full (collector down or slow).
	Dropped int64
	// Unauthenticated is records shed because no credential was available to authenticate them.
	Unauthenticated int64
	// Rejected is spans the gateway declined per item, reported as partial_errors. The gateway
	// reports per-item validation failures inside an HTTP 200 body, so without this counter a
	// deployment whose every span is refused (PII_DETECTED, PAYLOAD_TOO_LARGE, load shedding) looks
	// perfectly healthy from here.
	Rejected int64
}

// Any reports whether anything at all has been shed. An operator only needs to hear from the sink
// when the answer is yes.
func (s Stats) Any() bool { return s.Dropped > 0 || s.Unauthenticated > 0 || s.Rejected > 0 }

// Options configures an HTTPSink.
type Options struct {
	// URL is the collector ingest endpoint records are POSTed to.
	URL string
	// Deployment labels every record (cloud vs self-host).
	Deployment Deployment
	// IngestToken is the fallback bearer used when a record carries no tenant key of its own (a
	// single-tenant self-host running with EDGE_PROXY_REQUIRE_TENANT unset). In the hosted
	// multi-tenant deployment the per-request tenant key is what authenticates, so this stays empty.
	IngestToken string
	// Buffer is the channel depth; defaults to 1024.
	Buffer int
	// Client overrides the HTTP client (mainly for tests); defaults to a short-timeout client.
	Client *http.Client
	// ReportInterval, when > 0, starts a ticker that logs newly shed records at that cadence. The
	// counters are otherwise unreachable from outside the process, so a total telemetry failure (a
	// misconfigured credential, a gateway rejecting every span) would be completely silent.
	ReportInterval time.Duration
	// Logf overrides where the sink logs (tests); defaults to log.Printf.
	Logf func(format string, args ...any)
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
	logf := o.Logf
	if logf == nil {
		logf = log.Printf
	}
	s := &HTTPSink{
		url:         o.URL,
		deployment:  dep,
		ingestToken: o.IngestToken,
		client:      client,
		ch:          make(chan proxy.TraceRecord, buf),
		logf:        logf,
		done:        make(chan struct{}),
	}
	s.wg.Add(1)
	go s.run()
	if o.ReportInterval > 0 {
		s.wg.Add(1)
		go s.reportLoop(o.ReportInterval)
	}
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

// Unauthenticated returns the number of records shed because neither a resolved tenant key nor a
// configured ingest token was available to authenticate the POST.
func (s *HTTPSink) Unauthenticated() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.unauthenticated
}

// Rejected returns the number of spans the gateway declined per item (partial_errors).
func (s *HTTPSink) Rejected() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rejected
}

// Stats snapshots all three loss counters at once, so an operator report cannot mix reads from
// different instants.
func (s *HTTPSink) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return Stats{Dropped: s.dropped, Unauthenticated: s.unauthenticated, Rejected: s.rejected}
}

// ReportLoss logs what has been shed since the previous report and stays silent when nothing has.
// It is exported so the binary can also report once at shutdown, which is the only chance a
// short-lived process gets to say that it shipped nothing.
func (s *HTTPSink) ReportLoss() {
	s.mu.Lock()
	delta := Stats{
		Dropped:         s.dropped - s.reported.Dropped,
		Unauthenticated: s.unauthenticated - s.reported.Unauthenticated,
		Rejected:        s.rejected - s.reported.Rejected,
	}
	total := Stats{Dropped: s.dropped, Unauthenticated: s.unauthenticated, Rejected: s.rejected}
	s.reported = total
	s.mu.Unlock()

	if !delta.Any() {
		return
	}
	s.logf(
		"edge-proxy telemetry: shed since last report: %d buffer-full, %d unauthenticated, "+
			"%d rejected by ingest (totals: %d / %d / %d)",
		delta.Dropped, delta.Unauthenticated, delta.Rejected,
		total.Dropped, total.Unauthenticated, total.Rejected,
	)
}

func (s *HTTPSink) reportLoop(every time.Duration) {
	defer s.wg.Done()
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-t.C:
			s.ReportLoss()
		case <-s.done:
			return
		}
	}
}

func (s *HTTPSink) run() {
	defer s.wg.Done()
	for rec := range s.ch {
		s.post(rec)
	}
}

// credentialFor picks the bearer that authenticates this record's batch.
//
// The record's own tenant key wins, but ONLY when the edge-key cache actually resolved it, which is
// exactly the condition TenantId being non-empty encodes (proxy.ServeHTTP sets TenantId only for a
// key found in the cache with write scope). The header value is client-controlled, so an unresolved
// one is not a credential, it is an arbitrary string: a value carrying bytes Go refuses in a header
// (a newline, DEL) makes client.Do fail on every proxied request, which an attacker can trigger at
// will to flood the log. Falling back to the configured IngestToken for anything unresolved keeps
// that string off the wire entirely. The gateway independently validates the token and enforces
// TENANT_MISMATCH, so this was never a cross-tenant write risk, only a self-inflicted one.
//
// The key is read here and put on a header; it is never written into the batch body and never
// logged.
func (s *HTTPSink) credentialFor(rec proxy.TraceRecord) string {
	if rec.TenantKey != "" && rec.TenantId != "" {
		return rec.TenantKey
	}
	return s.ingestToken
}

func (s *HTTPSink) post(rec proxy.TraceRecord) {
	token := s.credentialFor(rec)
	if token == "" {
		// Nothing can authenticate this batch. Shed it and count it rather than POSTing an
		// unauthenticated body the gateway will reject anyway.
		s.mu.Lock()
		s.unauthenticated++
		s.mu.Unlock()
		return
	}
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
	// The ingest protocol the gateway negotiates against (gateway/protocol.py). Sending it
	// explicitly means a future protocol bump is a clean 400 here rather than a silent
	// misinterpretation of our payload.
	req.Header.Set("X-Ingest-Protocol", IngestProtocol)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := s.client.Do(req)
	if err != nil {
		// The error can name the collector host but never the credential (it is only ever a header
		// value, which net/http does not include in its error strings).
		log.Printf("edge-proxy telemetry: post failed: %v", err)
		return
	}
	// Read a bounded prefix of the body, then drain the remainder and close, so the connection goes
	// back to the pool for reuse (net/http only reuses a connection whose body was read to EOF).
	ackBody, _ := io.ReadAll(io.LimitReader(resp.Body, maxAckBytes))
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()

	if resp.StatusCode >= 400 {
		// A 401/403 here is the operator's signal that the ingest credential or the key's scope is
		// wrong.
		log.Printf("edge-proxy telemetry: ingest rejected batch with status %d", resp.StatusCode)
	}
	s.recordAck(resp.StatusCode, ackBody)
}

// maxAckBytes bounds how much of the ingest response is read. The ack is a small JSON object; the
// cap is only there so a misconfigured URL pointing at something that streams cannot make the sink
// buffer without limit.
const maxAckBytes = 64 << 10

// ingestAck is the subset of the gateway's BatchResponse the sink needs (gateway/app.py
// _response_dict). Deliberately partial: `message` is not decoded at all, because it can echo
// request detail and a telemetry failure must not become its own leak.
type ingestAck struct {
	Status        string `json:"status"`
	AcceptedSpans int    `json:"accepted_spans"`
	PartialErrors []struct {
		ItemId string `json:"item_id"`
		Code   string `json:"code"`
	} `json:"partial_errors"`
}

// recordAck accounts for per-item outcomes the gateway reports INSIDE a 200 body.
//
// The gateway answers a batch whose items were validated away with HTTP 200 and status "partial"
// (or "accepted" when the only entries are non-fatal flags), listing each refusal as a
// partial_errors entry. Only a batch where every item was rejected becomes a 422. So a status check
// alone hides the interesting failures: a caller sending a non-hex X-Tally-Account-Id-Hash
// (PII_DETECTED), an oversized span (PAYLOAD_TOO_LARGE), or ingest shedding under load would all
// read as success here. Count the shortfall against what was sent, and log the codes (never the
// messages) so the operator can act.
func (s *HTTPSink) recordAck(status int, body []byte) {
	var ack ingestAck
	if len(body) == 0 || json.Unmarshal(body, &ack) != nil {
		return
	}
	// The proxy sends exactly one span per batch, so the shortfall is exact rather than inferred.
	notAccepted := spansPerBatch - ack.AcceptedSpans
	if notAccepted <= 0 {
		return
	}
	s.mu.Lock()
	s.rejected += int64(notAccepted)
	s.mu.Unlock()
	codes := make([]string, 0, len(ack.PartialErrors))
	for _, e := range ack.PartialErrors {
		codes = append(codes, e.Code)
	}
	s.logf("edge-proxy telemetry: ingest accepted %d/%d spans (http %d, status %q, codes %v)",
		ack.AcceptedSpans, spansPerBatch, status, ack.Status, codes)
}

// spansPerBatch is the batch size the encoder emits: one record, one span, one batch.
const spansPerBatch = 1

// Close stops accepting records, waits for the worker to drain the buffer, and reports any
// telemetry that never made it. The final report matters most for a short-lived process: without it
// a proxy that shipped nothing at all for its whole life would exit without ever saying so.
func (s *HTTPSink) Close() {
	s.closeOne.Do(func() {
		close(s.ch)
		close(s.done)
	})
	s.wg.Wait()
	s.ReportLoss()
}
