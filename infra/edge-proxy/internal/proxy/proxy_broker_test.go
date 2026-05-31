package proxy

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/keybroker"
)

// fakeBroker is a minimal keybroker.Broker for proxy-level tests.
type fakeBroker struct {
	auth string
	err  error
}

func (f fakeBroker) Mint(_ context.Context, _ string) (keybroker.Credential, error) {
	if f.err != nil {
		return keybroker.Credential{}, f.err
	}
	return keybroker.Credential{Authorization: f.auth, ExpiresAt: time.Now().Add(time.Minute)}, nil
}

// brokerProxy wires a proxy in broker mode in front of an upstream that echoes the Authorization
// header it received, so a test can assert what the proxy injected.
func brokerProxy(t *testing.T, b keybroker.Broker) *httptest.Server {
	t.Helper()
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Echo back exactly what the upstream provider would have seen.
		w.Header().Set("X-Echo-Auth", r.Header.Get("Authorization"))
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(origin.Close)

	cfg, err := config.FromEnv(func(k string) string {
		if k == "EDGE_PROXY_UPSTREAM" {
			return origin.URL
		}
		return ""
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}

	front := httptest.NewServer(New(cfg, WithBroker(b)))
	t.Cleanup(front.Close)
	return front
}

func TestBrokerInjectsMintedCredential(t *testing.T) {
	front := brokerProxy(t, fakeBroker{auth: "Bearer sk-from-kms"})

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/chat/completions", nil)
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	// The client deliberately sends NO Authorization header — broker mode supplies it.
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if got := resp.Header.Get("X-Echo-Auth"); got != "Bearer sk-from-kms" {
		t.Fatalf("upstream Authorization = %q, want the broker-minted token", got)
	}
}

func TestBrokerOverridesClientAuthorization(t *testing.T) {
	front := brokerProxy(t, fakeBroker{auth: "Bearer sk-from-kms"})

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/x", nil)
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	// Even if a client tries to smuggle its own key, broker mode replaces it.
	req.Header.Set("Authorization", "Bearer sk-client-attempt")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if got := resp.Header.Get("X-Echo-Auth"); got != "Bearer sk-from-kms" {
		t.Fatalf("upstream Authorization = %q, want broker token to override client", got)
	}
}

func TestBrokerUnknownTenantReturns403(t *testing.T) {
	front := brokerProxy(t, fakeBroker{err: keybroker.ErrUnknownTenant{Tenant: "tk_live_nope"}})

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/x", nil)
	req.Header.Set("X-Tenant-Key", "tk_live_nope")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", resp.StatusCode)
	}
}

func TestBrokerUnavailableReturns502(t *testing.T) {
	front := brokerProxy(t, fakeBroker{err: errors.New("kms timeout")})

	req, _ := http.NewRequest(http.MethodPost, front.URL+"/v1/x", nil)
	req.Header.Set("X-Tenant-Key", "tk_live_acme")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", resp.StatusCode)
	}
}
