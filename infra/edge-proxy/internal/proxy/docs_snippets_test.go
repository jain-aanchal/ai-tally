// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/edgekeys"
)

// CTO-377: the docs and the dashboard show customers these exact curl commands for the hosted proxy
// (docs/public-api/connect-snippets.json, generated from web/lib/connectSnippets.ts). This test runs
// each one with bash and curl against an in-process proxy configured like the hosted deployment
// (path routes, tenant key required, org switch on) and fake provider upstreams. A changed header,
// base path or route that would break the documented command fails here. Hermetic: only loopback
// servers are contacted and the provider key is a fake.

const docsPlaceholderKey = "YOUR_TALLY_KEY"

type docsSnippet struct {
	ID   string `json:"id"`
	Code string `json:"code"`
}

type docsSnippetFile struct {
	Endpoints struct {
		OpenAIProxyBaseURL    string `json:"openaiProxyBaseUrl"`
		AnthropicProxyBaseURL string `json:"anthropicProxyBaseUrl"`
	} `json:"endpoints"`
	Snippets struct {
		Proxy []docsSnippet `json:"proxy"`
	} `json:"snippets"`
}

func loadDocsSnippets(t *testing.T) docsSnippetFile {
	t.Helper()
	// The package directory is infra/edge-proxy/internal/proxy; the file lives at the repo root.
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "docs", "public-api", "connect-snippets.json"))
	if err != nil {
		t.Fatalf("read connect-snippets.json: %v", err)
	}
	var f docsSnippetFile
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("parse connect-snippets.json: %v", err)
	}
	return f
}

// fakeProvider answers like a provider and records what reached it.
type fakeProvider struct {
	hits          atomic.Int32
	sawTenantKey  atomic.Bool
	sawCredential atomic.Bool
	path          atomic.Value
}

func (p *fakeProvider) handler(credentialHeader, body string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.hits.Add(1)
		p.path.Store(r.URL.Path)
		if r.Header.Get("X-Tenant-Key") != "" {
			p.sawTenantKey.Store(true)
		}
		if strings.Contains(r.Header.Get(credentialHeader), "fake-provider-key") {
			p.sawCredential.Store(true)
		}
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	})
}

func TestDocumentedProxySnippetsRunAgainstTheProxy(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not available")
	}
	if _, err := exec.LookPath("curl"); err != nil {
		t.Skip("curl not available")
	}

	snippets := loadDocsSnippets(t)
	if len(snippets.Snippets.Proxy) == 0 {
		t.Fatal("connect-snippets.json has no proxy snippets; the docs would show none")
	}

	openai := &fakeProvider{}
	anthropic := &fakeProvider{}
	openaiUpstream := httptest.NewServer(openai.handler("Authorization",
		`{"model":"gpt-4o-mini","usage":{"prompt_tokens":12,"completion_tokens":5}}`))
	t.Cleanup(openaiUpstream.Close)
	anthropicUpstream := httptest.NewServer(anthropic.handler("x-api-key",
		`{"model":"claude-haiku-4-5","usage":{"input_tokens":12,"output_tokens":5}}`))
	t.Cleanup(anthropicUpstream.Close)

	cfg := config.Config{
		TenantHeader:  "X-Tenant-Key",
		RequireTenant: true,
		RouteMode:     config.RouteModePath,
		Routes: []config.Route{
			{Match: "/openai", Upstream: mustURL(t, openaiUpstream.URL), Provider: config.ProviderOpenAI},
			{Match: "/anthropic", Upstream: mustURL(t, anthropicUpstream.URL), Provider: config.ProviderAnthropic},
		},
	}
	resolver := switchResolver{keyHash: edgekeys.HashKey(docsPlaceholderKey), tenant: "uuid-docs", enabled: true}
	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink), WithKeyResolver(resolver)))
	t.Cleanup(front.Close)

	cases := map[string]struct {
		provider *fakeProvider
		path     string
		model    string
	}{
		"proxy-openai":    {provider: openai, path: "/v1/chat/completions", model: "gpt-4o-mini"},
		"proxy-anthropic": {provider: anthropic, path: "/v1/messages", model: "claude-haiku-4-5"},
	}

	for _, snippet := range snippets.Snippets.Proxy {
		want, ok := cases[snippet.ID]
		if !ok {
			t.Fatalf("new proxy snippet %q has no test case; add one so it is executed", snippet.ID)
		}
		t.Run(snippet.ID, func(t *testing.T) {
			if !strings.Contains(snippet.Code, "https://ingest.ai-tally.com") {
				t.Fatalf("snippet no longer targets the hosted ingest host:\n%s", snippet.Code)
			}
			before := sink.count()
			code := strings.ReplaceAll(snippet.Code, "https://ingest.ai-tally.com", front.URL)
			cmd := exec.Command("bash", "-c", "set -euo pipefail\n"+code)
			cmd.Env = []string{
				"PATH=" + os.Getenv("PATH"),
				"OPENAI_API_KEY=fake-provider-key",
				"ANTHROPIC_API_KEY=fake-provider-key",
			}
			out, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("snippet failed: %v\n%s", err, out)
			}
			if want.provider.hits.Load() == 0 {
				t.Fatalf("the provider was never reached; proxy output:\n%s", out)
			}
			if got, _ := want.provider.path.Load().(string); got != want.path {
				t.Fatalf("provider saw path %q, want %q", got, want.path)
			}
			if want.provider.sawTenantKey.Load() {
				t.Fatal("X-Tenant-Key reached the provider; the docs say it is removed")
			}
			if !want.provider.sawCredential.Load() {
				t.Fatal("the provider credential did not pass through unchanged")
			}
			sink.waitFor(t, before+1)
			rec := sink.last()
			if rec.TenantId != "uuid-docs" || rec.Model != want.model {
				t.Fatalf("recorded tenant %q model %q, want uuid-docs / %s", rec.TenantId, rec.Model, want.model)
			}
			if rec.PromptTokens == nil || *rec.PromptTokens != 12 || rec.CompletionTokens == nil || *rec.CompletionTokens != 5 {
				t.Fatalf("token counts not recorded from the documented call: %+v", rec)
			}
		})
	}
}
