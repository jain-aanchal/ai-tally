package proxy

import (
	"io"
	"time"
)

// TraceRecord is the telemetry copy emitted for one proxied request. It carries only metadata and
// byte counts — never request or response *content*. The forwarded bytes are streamed straight
// through untouched; we count them as they pass but never buffer or persist them. This keeps the
// "bodies never written to logs/DB/disk" guarantee (CTO-42) structurally true: there is no field
// here that could hold a prompt, completion, or the customer's provider key.
type TraceRecord struct {
	// TenantKey is the ai-tally tenant identifier from the control header (X-Tenant-Key).
	TenantKey string
	Method    string
	Path      string
	// StatusCode is the upstream response status relayed to the client (0 if the upstream failed
	// before any status, e.g. connection refused — see Failed).
	StatusCode int
	// ReqBytes / RespBytes are the body sizes that transited the proxy, measured by counting.
	ReqBytes  int64
	RespBytes int64
	// Duration is wall time from receiving the request to finishing the response relay.
	Duration  time.Duration
	StartedAt time.Time
	// Failed is true when the upstream could not be reached (gateway returned 502).
	Failed bool
}

// Sink consumes telemetry copies. Implementations must be safe for concurrent use and must not
// block the request path — Record is called inline after the response is fully relayed, so a slow
// sink directly inflates tail latency. The real ingest sink (CTO-40/41) hands off to a buffered
// async channel; the default here is a no-op.
type Sink interface {
	Record(TraceRecord)
}

// NopSink discards every record. It is the default so the proxy core has zero telemetry overhead
// unless a real sink is wired in.
type NopSink struct{}

// Record implements Sink.
func (NopSink) Record(TraceRecord) {}

// countingReadCloser wraps a request/response body, tallying bytes as they are read by the
// downstream copier without altering the stream. Closing delegates to the wrapped closer.
type countingReadCloser struct {
	inner io.ReadCloser
	n     *int64
}

func (c *countingReadCloser) Read(p []byte) (int, error) {
	n, err := c.inner.Read(p)
	if n > 0 {
		*c.n += int64(n)
	}
	return n, err
}

func (c *countingReadCloser) Close() error { return c.inner.Close() }
