// Package proxy implements ai-tally's transparent edge reverse proxy (CTO-39).
//
// A customer points OPENAI_BASE_URL at this proxy and adds an X-Tenant-Key header. Every request
// is forwarded to the real provider byte-for-byte; the response streams back byte-for-byte. The
// only thing we keep is a TraceRecord of metadata + byte counts (never content), handed to a Sink.
//
// Design invariants:
//   - Bodies are never mutated, buffered, or persisted. We count bytes as they stream; that's it.
//   - The customer's provider key (Authorization header) is forwarded as-is and never read into
//     any field, log line, or stored struct — it lives only in the in-flight request.
//   - Stateless: no per-request state survives the response, so instances scale horizontally.
//   - FlushInterval -1 streams responses immediately, so SSE token streams pass through with no
//     added buffering latency (token *reconstruction* is CTO-40's job, not the proxy core's).
package proxy

import (
	"context"
	"net/http"
	"net/http/httputil"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// Proxy is an http.Handler that forwards to a configured upstream and emits telemetry copies.
type Proxy struct {
	cfg  config.Config
	rp   *httputil.ReverseProxy
	sink Sink
	now  func() time.Time
}

// Option customizes a Proxy at construction.
type Option func(*Proxy)

// WithSink sets the telemetry sink (default NopSink).
func WithSink(s Sink) Option {
	return func(p *Proxy) {
		if s != nil {
			p.sink = s
		}
	}
}

// WithTransport overrides the http.RoundTripper used to reach the upstream. Mainly for tests;
// production uses a pooled transport tuned for low connection-setup overhead.
func WithTransport(rt http.RoundTripper) Option {
	return func(p *Proxy) {
		if rt != nil {
			p.rp.Transport = rt
		}
	}
}

// withClock overrides the time source (tests only).
func withClock(now func() time.Time) Option {
	return func(p *Proxy) {
		if now != nil {
			p.now = now
		}
	}
}

// New builds a Proxy for the given config.
func New(cfg config.Config, opts ...Option) *Proxy {
	p := &Proxy{
		cfg:  cfg,
		sink: NopSink{},
		now:  time.Now,
	}

	p.rp = &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			// SetURL joins the inbound path/query onto the upstream origin, leaving the rest of
			// the request (method, headers, body) untouched.
			pr.SetURL(cfg.Upstream)
			// Send the upstream's own Host so TLS SNI and provider routing are correct.
			pr.Out.Host = cfg.Upstream.Host
		},
		// Stream every write straight to the client — critical for SSE completions.
		FlushInterval: -1,
		Transport:     defaultTransport(),
		ErrorHandler:  errorHandler,
	}

	for _, opt := range opts {
		opt(p)
	}
	return p
}

// ServeHTTP forwards the request and records a telemetry copy.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	tenant := r.Header.Get(p.cfg.TenantHeader)
	if p.cfg.RequireTenant && tenant == "" {
		http.Error(w, `{"error":"missing tenant key"}`+"\n", http.StatusBadRequest)
		return
	}

	start := p.now()

	var reqBytes int64
	if r.Body != nil {
		r.Body = &countingReadCloser{inner: r.Body, n: &reqBytes}
	}

	// Strip our control header so the upstream provider never sees ai-tally internals. Everything
	// else (including the customer's Authorization key) is forwarded unmodified.
	r.Header.Del(p.cfg.TenantHeader)

	rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
	// Mark whether the upstream was reachable so the telemetry copy can distinguish a real 502
	// from us synthesizing one.
	ctx := context.WithValue(r.Context(), failedKey{}, &rec.failed)
	p.rp.ServeHTTP(rec, r.WithContext(ctx))

	p.sink.Record(TraceRecord{
		TenantKey:  tenant,
		Method:     r.Method,
		Path:       r.URL.Path,
		StatusCode: rec.status,
		ReqBytes:   reqBytes,
		RespBytes:  rec.written,
		Duration:   p.now().Sub(start),
		StartedAt:  start,
		Failed:     rec.failed,
	})
}

type failedKey struct{}

// errorHandler turns an unreachable/erroring upstream into a clean 502 instead of a panic or a
// hung connection. It never leaks the underlying error (which can contain the upstream host) to
// the client body.
func errorHandler(w http.ResponseWriter, r *http.Request, _ error) {
	if f, ok := r.Context().Value(failedKey{}).(*bool); ok {
		*f = true
	}
	http.Error(w, `{"error":"upstream unavailable"}`+"\n", http.StatusBadGateway)
}

// statusRecorder captures the relayed status and counts response-body bytes written to the client,
// without buffering or altering them. It forwards Flush so streaming responses keep streaming.
type statusRecorder struct {
	http.ResponseWriter
	status      int
	written     int64
	wroteHeader bool
	failed      bool
}

func (s *statusRecorder) WriteHeader(code int) {
	if s.wroteHeader {
		return
	}
	s.status = code
	s.wroteHeader = true
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if !s.wroteHeader {
		s.WriteHeader(http.StatusOK)
	}
	n, err := s.ResponseWriter.Write(b)
	s.written += int64(n)
	return n, err
}

// Flush implements http.Flusher so FlushInterval streaming reaches the client.
func (s *statusRecorder) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Handler returns an http.Handler with sane top-level timeouts already applied.
func (p *Proxy) Handler() http.Handler { return p }
