package proxy

import (
	"net"
	"net/http"
	"time"
)

// defaultTransport is tuned for low per-request overhead: a warm connection pool so the common
// case reuses a keep-alive connection and pays ~zero setup cost, plus HTTP/2 to the provider.
// Keeping connections hot is what makes the p99 < 3ms overhead budget achievable — a cold TLS
// handshake would blow it instantly, so we never want to pay one on the hot path.
func defaultTransport() *http.Transport {
	return &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          256,
		MaxIdleConnsPerHost:   64,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
}
