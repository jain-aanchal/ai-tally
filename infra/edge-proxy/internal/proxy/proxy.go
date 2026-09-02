// SPDX-License-Identifier: Apache-2.0
// Package proxy implements ai-tally's transparent edge reverse proxy (CTO-39).
//
// A customer points OPENAI_BASE_URL at this proxy and adds an X-Tenant-Key header. Every request
// is forwarded to the real provider byte-for-byte; the response streams back byte-for-byte. The
// only thing we keep is a TraceRecord of metadata + byte counts (never content), handed to a Sink.
//
// Design invariants:
//   - Bodies are never mutated or persisted. In the default pass-through mode they are never even
//     buffered — we count bytes as they stream; that's it. In the opt-in provider-protocol mode
//     (CTO-167) a bounded copy of the response is teed aside transiently to parse scalar usage
//     metadata (model, token counts) and then discarded: content is never logged, stored, or handed
//     to a Sink, and the bytes still stream to the client unbuffered on the wire.
//   - The customer's provider key (Authorization header, or Gemini's ?key= / x-goog-api-key) is
//     forwarded as-is and never read into any field, log line, or stored struct — it lives only in
//     the in-flight request.
//   - Stateless: no per-request state survives the response, so instances scale horizontally.
//   - FlushInterval -1 streams responses immediately, so SSE token streams pass through with no
//     added buffering latency (token *reconstruction* is CTO-40's job, not the proxy core's).
package proxy

import (
	"context"
	"errors"
	"net/http"
	"net/http/httputil"
	"strings"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/edgekeys"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/keybroker"
)

// TenantResolver maps the sha256 hash of a presented X-Tenant-Key to the canonical tenant UUID
// (Initiative 2 sec 6.2). It is consulted once per request off a local in-memory map, never with a
// gateway round-trip. ok is false for an unknown or revoked key, which the proxy fails closed on
// when RequireTenant is set. *edgekeys.Cache satisfies this.
type TenantResolver interface {
	Resolve(keyHash string) (tenantID string, ok bool)
}

// Proxy is an http.Handler that forwards to a configured upstream and emits telemetry copies.
type Proxy struct {
	cfg      config.Config
	rp       *httputil.ReverseProxy
	sink     Sink
	broker   keybroker.Broker
	router   *router
	resolver TenantResolver
	now      func() time.Time
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

// WithBroker enables key-broker mode (CTO-43): instead of forwarding the client's Authorization
// header, the proxy mints a short-lived provider credential from the broker for the request's
// tenant and injects it. Used for self-hosted deployments where the provider key lives in the
// customer's KMS and must never reach their application code.
func WithBroker(b keybroker.Broker) Option {
	return func(p *Proxy) {
		if b != nil {
			p.broker = b
		}
	}
}

// WithKeyResolver enables fast key-to-tenant resolution (Initiative 2 sec 6.2). With a resolver set
// the proxy hashes the presented X-Tenant-Key, looks up the tenant UUID locally, stamps it on the
// TraceRecord (sec 6.3), and (when RequireTenant is set) fails closed with 403 on an unknown or
// revoked key. Without a resolver the proxy behaves as the CTO-39 core: no resolution, no 403.
func WithKeyResolver(r TenantResolver) Option {
	return func(p *Proxy) {
		if r != nil {
			p.resolver = r
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
		cfg:    cfg,
		sink:   NopSink{},
		router: newRouter(cfg),
		now:    time.Now,
	}

	p.rp = &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			// The per-request route was resolved in ServeHTTP and stashed on the context. In
			// single-origin mode it is simply cfg.Upstream. Routing is a pre-forward origin swap;
			// the rest of the request (method, headers, body, credential) is untouched.
			rt := routeFromContext(pr.In.Context())
			// Path mode strips the provider prefix (e.g. /openai) before forwarding so the upstream
			// sees its own path (/v1/chat/completions), not the routing prefix.
			if rt.stripPrefix != "" {
				pr.In.URL.Path = strings.TrimPrefix(pr.In.URL.Path, rt.stripPrefix)
				if !strings.HasPrefix(pr.In.URL.Path, "/") {
					pr.In.URL.Path = "/" + pr.In.URL.Path
				}
			}
			// SetURL joins the (possibly prefix-stripped) inbound path/query onto the origin.
			pr.SetURL(rt.upstream)
			// Send the upstream's own Host so TLS SNI and provider routing are correct.
			pr.Out.Host = rt.upstream.Host
		},
		// Stream every write straight to the client — critical for SSE completions.
		FlushInterval: -1,
		Transport:     defaultTransport(),
		ErrorHandler:  errorHandler,
	}

	// CTO-167 / sec 6.1: tee each response through a bounded scanner that extracts scalar
	// model/usage metadata, using the per-route provider. captureMeta no-ops when the resolved
	// route has no provider, so pure pass-through routes never inspect a response body.
	p.rp.ModifyResponse = p.captureMeta

	for _, opt := range opts {
		opt(p)
	}
	return p
}

// routeCtxKey is the request-context key under which ServeHTTP stashes the resolved route so the
// ReverseProxy's Rewrite and captureMeta can read it.
type routeCtxKey struct{}

func routeFromContext(ctx context.Context) resolvedRoute {
	if rt, ok := ctx.Value(routeCtxKey{}).(resolvedRoute); ok {
		return rt
	}
	return resolvedRoute{}
}

// metaKey is the request-context key under which ServeHTTP stashes the responseMeta holder that
// captureMeta fills in as the response streams.
type metaKey struct{}

// captureMeta wraps the upstream response body so scalar metadata (model, token usage) is parsed
// out as it streams — without buffering it on the wire or retaining any content. It is installed as
// the ReverseProxy's ModifyResponse only when a provider protocol is configured.
func (p *Proxy) captureMeta(resp *http.Response) error {
	if resp.Body == nil || resp.Request == nil {
		return nil
	}
	rt := routeFromContext(resp.Request.Context())
	// Pure pass-through route: no provider protocol, so never inspect the response body.
	if rt.provider == "" {
		return nil
	}
	holder, _ := resp.Request.Context().Value(metaKey{}).(*responseMeta)
	if holder == nil {
		return nil
	}
	resp.Body = &metaCapture{
		inner:    resp.Body,
		provider: rt.provider,
		path:     resp.Request.URL.Path,
		out:      holder,
	}
	return nil
}

// ServeHTTP forwards the request and records a telemetry copy.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Select the forwarding origin before anything else. A configured route table that matches
	// nothing is a 404 rather than a forward to a wrong (or fallback) origin.
	route, ok := p.router.resolve(r)
	if !ok {
		http.Error(w, `{"error":"no route for host"}`+"\n", http.StatusNotFound)
		return
	}

	tenant := r.Header.Get(p.cfg.TenantHeader)
	if p.cfg.RequireTenant && tenant == "" {
		http.Error(w, `{"error":"missing tenant key"}`+"\n", http.StatusBadRequest)
		return
	}

	// Fast key-to-tenant resolution (sec 6.2): hash the presented key and look it up in the local
	// cache. The resolved UUID becomes the canonical TenantId tag (sec 6.3). Fail closed on an
	// unknown or revoked key when RequireTenant is set, never forwarding an unauthenticated call.
	var tenantID string
	if p.resolver != nil && tenant != "" {
		id, found := p.resolver.Resolve(edgekeys.HashKey(tenant))
		if !found {
			if p.cfg.RequireTenant {
				http.Error(w, `{"error":"unknown tenant key"}`+"\n", http.StatusForbidden)
				return
			}
		} else {
			tenantID = id
		}
	}
	// Feature tag is optional: capture if present, never reject when absent.
	var featureTag string
	if p.cfg.FeatureTagHeader != "" {
		featureTag = r.Header.Get(p.cfg.FeatureTagHeader)
	}
	// Account hash is optional on the same terms as the feature tag: capture if present, never
	// reject when absent (CTO-182). The value is already hashed by the caller and is passed through
	// untouched -- the proxy holds no HMAC key and must never receive a raw account id.
	var accountIdHash string
	if p.cfg.AccountIdHashHeader != "" {
		accountIdHash = r.Header.Get(p.cfg.AccountIdHashHeader)
	}

	start := p.now()

	// Broker mode: mint a short-lived provider credential for this tenant from the customer's KMS
	// and inject it, replacing whatever (if anything) the client sent. The minted token is applied
	// only to the outgoing request header — never logged or recorded in the TraceRecord — preserving
	// the in-memory-only key guarantee. A miss (unknown tenant / broker down) fails the request
	// rather than forwarding an unauthenticated call.
	if p.broker != nil {
		cred, err := p.broker.Mint(r.Context(), tenant)
		if err != nil {
			var unknown keybroker.ErrUnknownTenant
			if errors.As(err, &unknown) {
				http.Error(w, `{"error":"unknown tenant"}`+"\n", http.StatusForbidden)
			} else {
				http.Error(w, `{"error":"key broker unavailable"}`+"\n", http.StatusBadGateway)
			}
			return
		}
		r.Header.Set("Authorization", cred.Authorization)
	}

	var reqBytes int64
	if r.Body != nil {
		r.Body = &countingReadCloser{inner: r.Body, n: &reqBytes}
	}

	// Strip our control headers so the upstream provider never sees ai-tally internals. Everything
	// else (including the customer's Authorization key) is forwarded unmodified.
	r.Header.Del(p.cfg.TenantHeader)
	if p.cfg.FeatureTagHeader != "" {
		r.Header.Del(p.cfg.FeatureTagHeader)
	}
	if p.cfg.AccountIdHashHeader != "" {
		r.Header.Del(p.cfg.AccountIdHashHeader)
	}

	rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
	// Mark whether the upstream was reachable so the telemetry copy can distinguish a real 502
	// from us synthesizing one.
	ctx := context.WithValue(r.Context(), failedKey{}, &rec.failed)
	// Hand the resolved route to the ReverseProxy's Rewrite (origin/path) and captureMeta (provider).
	ctx = context.WithValue(ctx, routeCtxKey{}, route)
	// When this route has a provider protocol, hand captureMeta a holder to fill as the response
	// streams (CTO-167).
	var meta responseMeta
	if route.provider != "" {
		ctx = context.WithValue(ctx, metaKey{}, &meta)
	}
	p.rp.ServeHTTP(rec, r.WithContext(ctx))

	p.sink.Record(TraceRecord{
		TenantKey:        tenant,
		TenantId:         tenantID,
		FeatureTag:       featureTag,
		AccountIdHash:    accountIdHash,
		Method:           r.Method,
		Path:             r.URL.Path,
		Model:            meta.Model,
		PromptTokens:     meta.PromptTokens,
		CompletionTokens: meta.CompletionTokens,
		StatusCode:       rec.status,
		ReqBytes:         reqBytes,
		RespBytes:        rec.written,
		Duration:         p.now().Sub(start),
		StartedAt:        start,
		Failed:           rec.failed,
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
