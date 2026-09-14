// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"compress/gzip"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-367: metering must survive a compressed upstream response.
//
// The first live call through the hosted proxy returned 200 to the client and stored a span with no
// model and no tokens. The client (Caddy's reverse_proxy transport, and the official openai /
// anthropic Python SDKs by default) sent Accept-Encoding: gzip, the proxy forwarded it, the provider
// gzipped the body, and metaCapture tried to parse gzip bytes as JSON. Every fixture test before this
// served an uncompressed body, so none of them could see it. These tests make the upstream behave like
// the real one: it compresses whenever the request says it may.

// gzipWhenAsked serves body with the given content type, gzipped only when the request advertises
// gzip, the way a real provider edge does. sse flushes after every write so a stream stays a stream.
func gzipWhenAsked(t *testing.T, body []byte, contentType string, sse bool) http.Handler {
	t.Helper()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentType)
		if !strings.Contains(r.Header.Get("Accept-Encoding"), "gzip") {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
		w.Header().Set("Content-Encoding", "gzip")
		w.WriteHeader(http.StatusOK)
		gz := gzip.NewWriter(w)
		if !sse {
			_, _ = gz.Write(body)
			_ = gz.Close()
			return
		}
		flusher, _ := w.(http.Flusher)
		for _, event := range strings.SplitAfter(string(body), "\n\n") {
			_, _ = gz.Write([]byte(event))
			_ = gz.Flush()
			if flusher != nil {
				flusher.Flush()
			}
		}
		_ = gz.Close()
	})
}

// doGzipRequest posts to the proxy advertising gzip, as Caddy and httpx do, and returns the decoded
// body the client would end up with. Setting Accept-Encoding by hand turns off Go's transparent
// decompression, so a gzip response is decoded here explicitly.
func doGzipRequest(t *testing.T, url string) []byte {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url,
		strings.NewReader(`{"model":"claude-opus-5","max_tokens":1024,"messages":[]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", "sk-ant-test")
	req.Header.Set("anthropic-version", "2023-06-01")
	req.Header.Set("Accept-Encoding", "gzip")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var r io.Reader = resp.Body
	if resp.Header.Get("Content-Encoding") == "gzip" {
		gz, err := gzip.NewReader(resp.Body)
		if err != nil {
			t.Fatalf("gzip reader: %v", err)
		}
		defer gz.Close()
		r = gz
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return got
}

func TestAnthropicProxyMetersGzipResponse(t *testing.T) {
	body := readFixture(t, "anthropic/message_end_turn.json")
	front, sink := newAnthropicProxy(t, gzipWhenAsked(t, body, "application/json", false))

	got := doGzipRequest(t, front.URL+"/v1/messages")
	if string(got) != string(body) {
		t.Errorf("client body altered in transit:\n got %q\nwant %q", got, body)
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "claude-opus-5" {
		t.Errorf("trace Model = %q, want claude-opus-5 (a compressed body was metered as unparseable)", rec.Model)
	}
	if !tokensEq(rec.PromptTokens, 10) || !tokensEq(rec.CompletionTokens, 15) {
		t.Errorf("trace tokens = %s/%s, want 10/15",
			tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens))
	}
}

func TestAnthropicProxyMetersGzipStream(t *testing.T) {
	body := readFixture(t, "anthropic/stream_text.sse")
	// The uncompressed fold is the reference: compression must not change what gets metered.
	want := extractMeta(config.ProviderAnthropic, "/v1/messages", body)
	if want.Model == "" || want.CompletionTokens == nil {
		t.Fatalf("fixture sanity: stream_text.sse folds to model %q, output %s", want.Model, tokensStr(want.CompletionTokens))
	}
	front, sink := newAnthropicProxy(t, gzipWhenAsked(t, body, "text/event-stream", true))

	got := doGzipRequest(t, front.URL+"/v1/messages")
	if string(got) != string(body) {
		t.Errorf("client stream altered in transit:\n got %q\nwant %q", got, body)
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != want.Model {
		t.Errorf("trace Model = %q, want %q", rec.Model, want.Model)
	}
	if tokensStr(rec.PromptTokens) != tokensStr(want.PromptTokens) ||
		tokensStr(rec.CompletionTokens) != tokensStr(want.CompletionTokens) {
		t.Errorf("trace tokens = %s/%s, want %s/%s",
			tokensStr(rec.PromptTokens), tokensStr(rec.CompletionTokens),
			tokensStr(want.PromptTokens), tokensStr(want.CompletionTokens))
	}
}
