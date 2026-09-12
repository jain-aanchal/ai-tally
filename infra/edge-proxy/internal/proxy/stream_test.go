// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-349: the streamed-usage scanner itself, independent of any one provider's schema.
//
// The fixture-driven per-provider expectations live in provider_anthropic_test.go and
// provider_payload_variations_test.go. What is tested here is the machinery around them: that the
// fold survives arbitrary chunk boundaries, that it never accumulates the body, that the bounded
// line buffer drops an event rather than half-parsing it, and that an absent count stays nil
// through every one of those paths.

// newProviderProxy builds a proxy speaking the given protocol in front of upstream. Mirrors
// newAnthropicProxy / newGeminiProxy for the providers those helpers do not cover.
func newProviderProxy(
	t *testing.T, p config.Provider, upstream http.Handler,
) (*httptest.Server, *recordingSink) {
	t.Helper()
	origin := httptest.NewServer(upstream)
	t.Cleanup(origin.Close)

	cfg, err := config.FromEnv(func(k string) string {
		switch k {
		case "EDGE_PROXY_UPSTREAM":
			return origin.URL
		case "EDGE_PROXY_PROVIDER":
			return string(p)
		default:
			return ""
		}
	})
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	sink := &recordingSink{}
	front := httptest.NewServer(New(cfg, WithSink(sink)))
	t.Cleanup(front.Close)
	return front, sink
}

// sseUpstream serves body as an event stream, flushing after each write so the client sees the
// events as they are produced rather than as one buffered response.
func sseUpstream(body []byte, chunk int) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		for i := 0; i < len(body); i += chunk {
			end := i + chunk
			if end > len(body) {
				end = len(body)
			}
			_, _ = w.Write(body[i:end])
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
	})
}

// TestStreamFoldIsChunkBoundaryIndependent feeds the same stream at every plausible packet size,
// down to one byte at a time. TCP decides where a response splits, so a scanner that only works
// when an event happens to land whole in one Read would meter correctly in tests and drop usage at
// random in production.
func TestStreamFoldIsChunkBoundaryIndependent(t *testing.T) {
	body := readFixture(t, "anthropic/stream_text.sse")
	for _, chunk := range []int{1, 7, 64, 512, len(body)} {
		var meta responseMeta
		mc := newMetaCapture(io.NopCloser(&chunkedReader{body: body, chunk: chunk}),
			config.ProviderAnthropic, "/v1/messages", true, &meta)
		if _, err := io.Copy(io.Discard, mc); err != nil {
			t.Fatalf("chunk %d: copy: %v", chunk, err)
		}
		if err := mc.Close(); err != nil {
			t.Fatalf("chunk %d: close: %v", chunk, err)
		}
		if meta.Model != "claude-opus-5" {
			t.Errorf("chunk %d: Model = %q", chunk, meta.Model)
		}
		assertTokens(t, "PromptTokens", meta.PromptTokens, ptr(25))
		assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(15))
	}
}

// chunkedReader hands out at most chunk bytes per Read, standing in for a stream that arrives in
// small packets.
type chunkedReader struct {
	body  []byte
	chunk int
	off   int
}

func (r *chunkedReader) Read(p []byte) (int, error) {
	if r.off >= len(r.body) {
		return 0, io.EOF
	}
	n := r.chunk
	if n > len(p) {
		n = len(p)
	}
	if r.off+n > len(r.body) {
		n = len(r.body) - r.off
	}
	copy(p, r.body[r.off:r.off+n])
	r.off += n
	return n, nil
}

// TestStreamIsNotBuffered is the request-path guarantee: the proxy must not hold a streamed
// response back while it looks for usage. The upstream here refuses to send its second event until
// the client has already received the first, so a proxy that buffered the body would deadlock and
// fail on the timeout rather than quietly adding latency to every chat token.
func TestStreamIsNotBuffered(t *testing.T) {
	gotFirst := make(chan struct{})
	released := make(chan struct{})
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "event: message_start\ndata: {\"type\":\"message_start\",\"message\":"+
			"{\"model\":\"claude-opus-5\",\"usage\":{\"input_tokens\":25,\"output_tokens\":1}}}\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-released:
		case <-time.After(5 * time.Second):
		}
		_, _ = io.WriteString(w, "event: message_delta\ndata: {\"type\":\"message_delta\","+
			"\"usage\":{\"output_tokens\":15}}\n\n")
		w.(http.Flusher).Flush()
	})
	front, sink := newProviderProxy(t, config.ProviderAnthropic, upstream)

	resp, err := http.Post(front.URL+"/v1/messages", "application/json",
		strings.NewReader(`{"stream":true}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	defer resp.Body.Close()

	go func() {
		buf := make([]byte, 256)
		if _, err := resp.Body.Read(buf); err == nil {
			close(gotFirst)
		}
	}()
	select {
	case <-gotFirst:
	case <-time.After(3 * time.Second):
		t.Fatal("first event did not reach the client before the upstream sent the second: " +
			"the response is being buffered")
	}
	close(released)
	_, _ = io.Copy(io.Discard, resp.Body)

	sink.waitFor(t, 1)
	rec := sink.last()
	assertTokens(t, "PromptTokens", rec.PromptTokens, ptr(25))
	assertTokens(t, "CompletionTokens", rec.CompletionTokens, ptr(15))
}

// TestStreamLineCapSkipsEventAndLeavesCountsUnknown exercises the bound. An event larger than the
// line buffer is skipped whole rather than truncated and parsed, so if it was the event carrying
// the usage the counts stay UNKNOWN. That is the deliberate trade: a missing number is recoverable,
// a number parsed out of half a JSON object is a wrong one nobody would question.
func TestStreamLineCapSkipsEventAndLeavesCountsUnknown(t *testing.T) {
	// A message_start whose (meaningless) padding field pushes the single data line past the cap.
	var b bytes.Buffer
	b.WriteString(`data: {"type":"message_start","message":{"model":"claude-opus-5","usage":` +
		`{"input_tokens":25,"output_tokens":1}},"pad":"`)
	b.Write(bytes.Repeat([]byte("x"), maxSSELineBytes))
	b.WriteString("\"}\n\n")
	// A following, ordinary event proves the scanner recovers rather than giving up on the stream.
	b.WriteString(`data: {"type":"message_delta","usage":{"output_tokens":15}}` + "\n\n")

	f := newStreamFolder(config.ProviderAnthropic)
	s := f.scanner()
	s.write(b.Bytes())
	s.close()

	if !s.capHit {
		t.Error("scanner did not record that an event was skipped for length")
	}
	meta := f.meta()
	// The oversized event held the model and the input count, so both stay unknown. Nothing is
	// salvaged from a partially read event.
	if meta.Model != "" {
		t.Errorf("Model = %q, want unknown", meta.Model)
	}
	assertTokens(t, "PromptTokens", meta.PromptTokens, nil)
	// The event after the oversized one still parsed.
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, ptr(15))
}

// TestStreamWithNoUsageAtAll is the blanket honesty case across providers: a stream that never
// reports usage yields nil counts, never 0. This is the shape of an OpenAI stream without
// stream_options.include_usage, and of any stream the client abandoned early.
func TestStreamWithNoUsageAtAll(t *testing.T) {
	for _, p := range []config.Provider{
		config.ProviderOpenAI, config.ProviderAnthropic, config.ProviderGemini,
	} {
		t.Run(string(p), func(t *testing.T) {
			meta := scanSSE(p, []byte("data: {\"type\":\"ping\"}\n\ndata: [DONE]\n\n"))
			assertTokens(t, "PromptTokens", meta.PromptTokens, nil)
			assertTokens(t, "CompletionTokens", meta.CompletionTokens, nil)
			assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, nil)
		})
	}
}

// TestEmptyStreamLeavesEverythingUnknown: a 200 with a text/event-stream content type and no bytes
// at all (an upstream that died before its first event) must not produce counts.
func TestEmptyStreamLeavesEverythingUnknown(t *testing.T) {
	var meta responseMeta
	mc := newMetaCapture(io.NopCloser(strings.NewReader("")),
		config.ProviderOpenAI, "/v1/chat/completions", true, &meta)
	_, _ = io.Copy(io.Discard, mc)
	if err := mc.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if meta.Model != "" {
		t.Errorf("Model = %q, want unknown", meta.Model)
	}
	assertTokens(t, "PromptTokens", meta.PromptTokens, nil)
	assertTokens(t, "CompletionTokens", meta.CompletionTokens, nil)
}

// TestOpenAIProxyStreamingEndToEnd drives the OpenAI protocol through the real proxy, including the
// "data: [DONE]" sentinel, which is not JSON and must not disturb the fold.
func TestOpenAIProxyStreamingEndToEnd(t *testing.T) {
	body := readFixture(t, "openai/stream_with_usage.sse")
	front, sink := newProviderProxy(t, config.ProviderOpenAI, sseUpstream(body, 41))

	resp, err := http.Post(front.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"stream":true,"stream_options":{"include_usage":true}}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, body) {
		t.Error("SSE stream altered in transit")
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "gpt-4o-2024-08-06" {
		t.Errorf("Model = %q", rec.Model)
	}
	assertTokens(t, "PromptTokens", rec.PromptTokens, ptr(19))
	assertTokens(t, "CompletionTokens", rec.CompletionTokens, ptr(10))
}

// TestGeminiProxyStreamingEndToEnd drives alt=sse streaming. Gemini repeats usageMetadata on every
// chunk, so the last chunk to report each count wins; the model comes from the request path as it
// does for a buffered call.
func TestGeminiProxyStreamingEndToEnd(t *testing.T) {
	body := readFixture(t, "gemini/stream_generate_content.sse")
	front, sink := newProviderProxy(t, config.ProviderGemini, sseUpstream(body, 97))

	path := "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
	resp, err := http.Post(front.URL+path, "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, body) {
		t.Error("SSE stream altered in transit")
	}

	sink.waitFor(t, 1)
	rec := sink.last()
	if rec.Model != "gemini-2.5-flash" {
		t.Errorf("Model = %q, want gemini-2.5-flash", rec.Model)
	}
	assertTokens(t, "PromptTokens", rec.PromptTokens, ptr(25))
	assertTokens(t, "CompletionTokens", rec.CompletionTokens, ptr(15))
}

// TestNonStreamResponseStillUsesTheBufferedScan guards the path that was already working: a normal
// JSON response through a provider proxy must keep parsing exactly as before, whatever the
// streaming code does.
func TestNonStreamResponseStillUsesTheBufferedScan(t *testing.T) {
	body := readFixture(t, "anthropic/message_end_turn.json")
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
	front, sink := newProviderProxy(t, config.ProviderAnthropic, upstream)

	resp, err := http.Post(front.URL+"/v1/messages", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	sink.waitFor(t, 1)
	rec := sink.last()
	assertTokens(t, "PromptTokens", rec.PromptTokens, ptr(10))
	assertTokens(t, "CompletionTokens", rec.CompletionTokens, ptr(15))
}

// TestSSEFallbackWhenContentTypeIsWrong: the request path picks the streaming scan from the
// response Content-Type, but an upstream (or an intermediary) that streams SSE while labelling it
// application/json should still be metered. The buffered scan recognizes the body as an event
// stream and folds it, subject to the ordinary capture cap.
func TestSSEFallbackWhenContentTypeIsWrong(t *testing.T) {
	body := readFixture(t, "anthropic/stream_text.sse")
	upstream := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	})
	front, sink := newProviderProxy(t, config.ProviderAnthropic, upstream)

	resp, err := http.Post(front.URL+"/v1/messages", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	sink.waitFor(t, 1)
	rec := sink.last()
	assertTokens(t, "PromptTokens", rec.PromptTokens, ptr(25))
	assertTokens(t, "CompletionTokens", rec.CompletionTokens, ptr(15))
}
