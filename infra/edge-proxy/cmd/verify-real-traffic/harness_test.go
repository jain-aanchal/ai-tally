// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

// These tests drive the harness end to end against a fake upstream (CTO-350). The fake speaks all
// three providers' wire shapes, puts a sentinel string in every piece of content so a leak into the
// report or a captured fixture is detectable, and checks each request carries its key.

const sentinel = "SENTINEL-COMPLETION-TEXT"

var fakeKeys = map[string]string{
	"OPENAI_API_KEY":    "fake-openai-key",
	"ANTHROPIC_API_KEY": "fake-anthropic-key",
	"GEMINI_API_KEY":    "fake-gemini-key",
}

type fakeUpstream struct {
	srv       *httptest.Server
	requests  atomic.Int64
	openaiN   atomic.Int64
	anthropN  atomic.Int64
	geminiN   atomic.Int64
	geminiGap int64 // added to totalTokenCount to simulate an unaccounted output bucket
}

func newFakeUpstream(t *testing.T) *fakeUpstream {
	f := &fakeUpstream{}
	f.srv = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func unauthorized(w http.ResponseWriter, typ string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	fmt.Fprintf(w, `{"type":"error","error":{"type":%q,"message":"bad key, prompt was %s"}}`, typ, sentinel)
}

func (f *fakeUpstream) serve(w http.ResponseWriter, r *http.Request) {
	f.requests.Add(1)
	body, _ := io.ReadAll(r.Body)
	flusher, _ := w.(http.Flusher)
	sse := func(events ...string) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, e := range events {
			_, _ = io.WriteString(w, e+"\n\n")
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
	switch {
	case r.URL.Path == "/v1/chat/completions":
		if r.Header.Get("Authorization") != "Bearer "+fakeKeys["OPENAI_API_KEY"] {
			unauthorized(w, "invalid_api_key")
			return
		}
		prompt, cached := 12, 0
		if bytes.Contains(body, []byte(`"role":"system"`)) {
			prompt = 2048
			if f.openaiN.Add(1) > 1 {
				cached = 1920
			}
		}
		usage := fmt.Sprintf(`{"prompt_tokens":%d,"completion_tokens":3,"total_tokens":%d,"prompt_tokens_details":{"cached_tokens":%d},"completion_tokens_details":{"reasoning_tokens":0}}`,
			prompt, prompt+3, cached)
		w.Header().Set("x-request-id", "req_fake_openai")
		const model = "gpt-4o-mini-2024-07-18"
		if bytes.Contains(body, []byte(`"stream":true`)) {
			sse(
				`data: {"id":"chatcmpl-fake","object":"chat.completion.chunk","created":1,"model":"`+model+`","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}],"usage":null}`,
				`data: {"id":"chatcmpl-fake","object":"chat.completion.chunk","created":1,"model":"`+model+`","choices":[{"index":0,"delta":{"content":"`+sentinel+`"},"finish_reason":null}],"usage":null}`,
				`data: {"id":"chatcmpl-fake","object":"chat.completion.chunk","created":1,"model":"`+model+`","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":null}`,
				`data: {"id":"chatcmpl-fake","object":"chat.completion.chunk","created":1,"model":"`+model+`","choices":[],"usage":`+usage+`}`,
				`data: [DONE]`,
			)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"id":"chatcmpl-fake","object":"chat.completion","created":1,"model":%q,"choices":[{"index":0,"message":{"role":"assistant","content":%q,"refusal":null},"finish_reason":"stop"}],"usage":%s}`,
			model, sentinel, usage)

	case r.URL.Path == "/v1/messages":
		if r.Header.Get("x-api-key") != fakeKeys["ANTHROPIC_API_KEY"] {
			unauthorized(w, "authentication_error")
			return
		}
		input, create, read := 12, 0, 0
		if bytes.Contains(body, []byte(`"cache_control"`)) {
			input = 4
			if f.anthropN.Add(1) > 1 {
				read = 5000
			} else {
				create = 5000
			}
		}
		cacheUsage := fmt.Sprintf(`"input_tokens":%d,"cache_creation_input_tokens":%d,"cache_read_input_tokens":%d`, input, create, read)
		w.Header().Set("request-id", "req_fake_anthropic")
		const model = "claude-haiku-4-5-20251001"
		if bytes.Contains(body, []byte(`"stream":true`)) {
			sse(
				"event: message_start\ndata: "+`{"type":"message_start","message":{"id":"msg_fake","type":"message","role":"assistant","content":[],"model":"`+model+`","stop_reason":null,"stop_sequence":null,"usage":{`+cacheUsage+`,"output_tokens":1}}}`,
				"event: content_block_start\ndata: "+`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`,
				"event: content_block_delta\ndata: "+`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"`+sentinel+`"}}`,
				"event: content_block_stop\ndata: "+`{"type":"content_block_stop","index":0}`,
				"event: message_delta\ndata: "+`{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}`,
				"event: message_stop\ndata: "+`{"type":"message_stop"}`,
			)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"id":"msg_fake","type":"message","role":"assistant","model":%q,"content":[{"type":"text","text":%q}],"stop_reason":"end_turn","stop_sequence":null,"usage":{%s,"output_tokens":3}}`,
			model, sentinel, cacheUsage)

	case strings.HasPrefix(r.URL.Path, "/v1beta/models/"):
		if r.Header.Get("x-goog-api-key") != fakeKeys["GEMINI_API_KEY"] {
			unauthorized(w, "UNAUTHENTICATED")
			return
		}
		prompt, candidates, thoughts := int64(9), int64(4), int64(0)
		cached := ""
		if bytes.Contains(body, []byte(`"thinkingBudget":512`)) {
			thoughts = 300
		}
		if len(body) > 10_000 {
			prompt = 9000
			if f.geminiN.Add(1) > 1 {
				cached = `"cachedContentTokenCount":8000,`
			}
		}
		thoughtsField := ""
		if thoughts > 0 {
			thoughtsField = fmt.Sprintf(`"thoughtsTokenCount":%d,`, thoughts)
		}
		final := fmt.Sprintf(`{"promptTokenCount":%d,%s"candidatesTokenCount":%d,%s"totalTokenCount":%d}`,
			prompt, cached, candidates, thoughtsField, prompt+candidates+thoughts+f.geminiGap)
		if strings.Contains(r.URL.Path, ":streamGenerateContent") {
			sse(
				`data: {"candidates":[{"content":{"parts":[{"text":"`+sentinel+`"}],"role":"model"},"index":0}],"usageMetadata":{"promptTokenCount":`+fmt.Sprint(prompt)+`,"totalTokenCount":`+fmt.Sprint(prompt)+`},"modelVersion":"gemini-2.5-flash","responseId":"resp_fake_gemini"}`,
				`data: {"candidates":[{"content":{"parts":[{"text":"`+sentinel+`"}],"role":"model"},"finishReason":"STOP","index":0}],"usageMetadata":`+final+`,"modelVersion":"gemini-2.5-flash","responseId":"resp_fake_gemini"}`,
			)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"candidates":[{"content":{"parts":[{"text":%q}],"role":"model"},"finishReason":"STOP","index":0}],"usageMetadata":%s,"modelVersion":"gemini-2.5-flash","responseId":"resp_fake_gemini"}`,
			sentinel, final)

	default:
		http.NotFound(w, r)
	}
}

func (f *fakeUpstream) args(extra ...string) []string {
	return append([]string{
		"--openai-upstream", f.srv.URL,
		"--anthropic-upstream", f.srv.URL,
		"--gemini-upstream", f.srv.URL,
		"--timeout", "10s",
	}, extra...)
}

func envWith(keys ...string) func(string) string {
	set := map[string]string{}
	for _, k := range keys {
		set[k] = fakeKeys[k]
	}
	return func(k string) string { return set[k] }
}

var allKeys = []string{"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"}

func runHarness(t *testing.T, getenv func(string) string, args []string) (int, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(args, getenv, &out, &errOut)
	return code, out.String() + errOut.String()
}

func assertNoLeak(t *testing.T, where, text string) {
	t.Helper()
	for _, secret := range []string{sentinel, tinyPrompt, "bat and a ball", "Row 1:", "fake-openai-key", "fake-anthropic-key", "fake-gemini-key"} {
		if strings.Contains(text, secret) {
			t.Errorf("%s contains %q; content or a key leaked", where, secret)
		}
	}
}

func TestAllProvidersMatch(t *testing.T) {
	f := newFakeUpstream(t)
	code, out := runHarness(t, envWith(allKeys...), f.args())
	if code != exitOK {
		t.Fatalf("exit %d, want 0\n%s", code, out)
	}
	if n := strings.Count(out, "MATCH"); n != 14 {
		t.Errorf("want 14 MATCH rows, got %d\n%s", n, out)
	}
	if strings.Contains(out, "MISMATCH") || strings.Contains(out, "COVERAGE GAPS") {
		t.Errorf("unexpected mismatch or coverage gap\n%s", out)
	}
	if got := f.requests.Load(); got != 14 {
		t.Errorf("upstream saw %d requests, want 14", got)
	}
	// The dashboard section must carry the request ids and the provider's raw field names.
	for _, want := range []string{"req_fake_openai", "req_fake_anthropic", "resp_fake_gemini",
		"cache_read_input_tokens=5000", "thoughtsTokenCount=600", "cachedContentTokenCount=8000"} {
		if !strings.Contains(out, want) {
			t.Errorf("report missing %q\n%s", want, out)
		}
	}
	assertNoLeak(t, "report", out)
}

func TestMissingKeySkipsProvider(t *testing.T) {
	f := newFakeUpstream(t)
	code, out := runHarness(t, envWith("OPENAI_API_KEY"), f.args())
	if code != exitOK {
		t.Fatalf("exit %d, want 0\n%s", code, out)
	}
	for _, want := range []string{"ANTHROPIC_API_KEY is not set", "GEMINI_API_KEY is not set"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q\n%s", want, out)
		}
	}
	if got := f.requests.Load(); got != 4 {
		t.Errorf("upstream saw %d requests, want only the 4 OpenAI calls", got)
	}
}

func TestNoKeysIsAUsageError(t *testing.T) {
	f := newFakeUpstream(t)
	code, out := runHarness(t, envWith(), f.args())
	if code != exitUsage || f.requests.Load() != 0 {
		t.Fatalf("exit %d with %d requests, want exit 2 and none\n%s", code, f.requests.Load(), out)
	}
}

// TestMismatchDetected makes the provider report a total with an output bucket the proxy does not
// account for. The proxy then records candidates+thoughts while the provider's own total says more,
// which is precisely the shape of discrepancy a real endpoint could surprise us with.
func TestMismatchDetected(t *testing.T) {
	f := newFakeUpstream(t)
	f.geminiGap = 40
	code, out := runHarness(t, envWith("GEMINI_API_KEY"), f.args())
	if code != exitMismatch {
		t.Fatalf("exit %d, want 1\n%s", code, out)
	}
	if !strings.Contains(out, "MISMATCH completion provider=") || !strings.Contains(out, "(delta -40)") {
		t.Errorf("mismatch row does not name the field and delta\n%s", out)
	}
	if !strings.Contains(out, "gemini convention:") {
		t.Errorf("expected a convention note for the inconsistent total\n%s", out)
	}
}

func TestProviderErrorFailsWithoutPrintingItsMessage(t *testing.T) {
	f := newFakeUpstream(t)
	env := func(k string) string {
		if k == "ANTHROPIC_API_KEY" {
			return "wrong-key"
		}
		return ""
	}
	code, out := runHarness(t, env, f.args())
	if code != exitMismatch {
		t.Fatalf("exit %d, want 1\n%s", code, out)
	}
	if !strings.Contains(out, "ERROR http 401 (authentication_error)") {
		t.Errorf("error row missing\n%s", out)
	}
	assertNoLeak(t, "report", out)
}

func TestCompareCountsKeepsUnknownDistinctFromZero(t *testing.T) {
	five, zero := int64(5), int64(0)
	if d := compareCounts(counts{}, counts{}); len(d) != 0 {
		t.Errorf("nil vs nil should agree, got %v", d)
	}
	d := compareCounts(counts{Cached: nil}, counts{Cached: &five})
	if len(d) != 1 || d[0] != "cached provider=nil proxy=5" {
		t.Errorf("nil vs 5: got %v", d)
	}
	d = compareCounts(counts{Prompt: &zero}, counts{Prompt: nil})
	if len(d) != 1 || d[0] != "prompt provider=0 proxy=nil" {
		t.Errorf("0 vs nil: got %v", d)
	}
}

func TestSpendCapRefusesBeforeFirstCall(t *testing.T) {
	f := newFakeUpstream(t)
	code, out := runHarness(t, envWith(allKeys...), f.args("--max-usd", "0.000001"))
	if code != exitRefused {
		t.Fatalf("exit %d, want 3\n%s", code, out)
	}
	if !strings.Contains(out, "REFUSED openai/plain") {
		t.Errorf("refusal not reported\n%s", out)
	}
	if got := f.requests.Load(); got != 0 {
		t.Errorf("upstream saw %d requests; the cap must refuse BEFORE sending", got)
	}
}

// TestSpendCapStopsMidMatrix sets a cap the two small OpenAI calls fit under and the cached-prompt
// call does not, and checks nothing is sent from the refusal onward.
func TestSpendCapStopsMidMatrix(t *testing.T) {
	f := newFakeUpstream(t)
	specs := buildMatrix(map[config.Provider]string{config.ProviderOpenAI: "gpt-4o-mini"},
		map[config.Provider]bool{config.ProviderOpenAI: true})
	warm := worstCaseMicro(specs[2], priceTable[config.ProviderOpenAI]["gpt-4o-mini"])
	capUSD := strings.TrimPrefix(formatUSD(warm-1), "$")

	code, out := runHarness(t, envWith("OPENAI_API_KEY"), f.args("--max-usd", capUSD))
	if code != exitRefused {
		t.Fatalf("exit %d, want 3\n%s", code, out)
	}
	if !strings.Contains(out, "REFUSED openai/cache_warm") {
		t.Errorf("expected refusal at cache_warm\n%s", out)
	}
	if got := f.requests.Load(); got != 2 {
		t.Errorf("upstream saw %d requests, want exactly the 2 calls under the cap", got)
	}
}

func TestUnpricedModelIsRefused(t *testing.T) {
	f := newFakeUpstream(t)
	code, out := runHarness(t, envWith("OPENAI_API_KEY"), f.args("--openai-model", "gpt-unknown"))
	if code != exitUsage || f.requests.Load() != 0 {
		t.Fatalf("exit %d with %d requests, want 2 and none\n%s", code, f.requests.Load(), out)
	}
}

// TestCaptureStripsContentAndRoundTrips captures the whole matrix, checks no content or key reached
// disk, and replays every stripped fixture through the proxy to prove the stripping kept everything
// the parser needs and the sidecar describes what the proxy records.
func TestCaptureStripsContentAndRoundTrips(t *testing.T) {
	f := newFakeUpstream(t)
	dir := t.TempDir()
	code, out := runHarness(t, envWith(allKeys...), f.args("--capture", "--capture-dir", dir))
	if code != exitOK {
		t.Fatalf("exit %d, want 0\n%s", code, out)
	}

	sidecars, _ := filepath.Glob(filepath.Join(dir, "*", "real_*.expected.json"))
	if len(sidecars) != 14 {
		t.Fatalf("want 14 sidecars, got %d\n%s", len(sidecars), out)
	}
	err := filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		b, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		assertNoLeak(t, path, string(b))
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	for _, sc := range sidecars {
		var want sidecar
		b, _ := os.ReadFile(sc)
		if err := json.Unmarshal(b, &want); err != nil {
			t.Fatalf("%s: %v", sc, err)
		}
		provider := config.Provider(filepath.Base(filepath.Dir(sc)))
		fixture, err := os.ReadFile(filepath.Join(filepath.Dir(sc), want.Fixture))
		if err != nil {
			t.Fatal(err)
		}
		rec := replayThroughProxy(t, provider, want.RequestPath, fixture, strings.HasSuffix(want.Fixture, ".sse"))
		got := counts{Prompt: rec.PromptTokens, Completion: rec.CompletionTokens, Cached: rec.CachedInputTokens}
		if d := compareCounts(counts{Prompt: want.PromptTokens, Completion: want.CompletionTokens, Cached: want.CachedInputTokens}, got); len(d) > 0 {
			t.Errorf("%s: replay disagrees with sidecar: %v", sc, d)
		}
		if rec.Model != want.Model {
			t.Errorf("%s: replay model %q, sidecar %q", sc, rec.Model, want.Model)
		}
		if want.PromptTokens == nil || want.CompletionTokens == nil {
			t.Errorf("%s: sidecar lost the usage counts", sc)
		}
	}
}

func replayThroughProxy(t *testing.T, p config.Provider, path string, fixture []byte, sse bool) proxy.TraceRecord {
	t.Helper()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if sse {
			w.Header().Set("Content-Type", "text/event-stream")
		} else {
			w.Header().Set("Content-Type", "application/json")
		}
		_, _ = w.Write(fixture)
	}))
	defer up.Close()
	sink := chanSink{ch: make(chan proxy.TraceRecord, 1)}
	base, stop, err := startProxy(map[config.Provider]string{
		config.ProviderOpenAI: up.URL, config.ProviderAnthropic: up.URL, config.ProviderGemini: up.URL,
	}, sink)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	resp, err := http.Post(base+"/"+string(p)+path, "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	select {
	case rec := <-sink.ch:
		return rec
	case <-time.After(5 * time.Second):
		t.Fatal("no TraceRecord from replay")
	}
	return proxy.TraceRecord{}
}

// TestStripIsAnAllowlist feeds fields no provider sends today. An allowlist drops them; a denylist
// would have passed each one straight into a committed fixture.
func TestStripIsAnAllowlist(t *testing.T) {
	doc := []byte(`{
		"id": "x", "model": "m", "brand_new_text_field": "secret-a",
		"choices": [{"index": 0, "finish_reason": "stop",
			"message": {"role": "assistant", "content": "secret-b", "annotations": [{"text": "secret-c"}]}}],
		"usage": {"prompt_tokens": 7, "novel_detail": {"label": "secret-d", "tokens": 2},
			"promptTokensDetails": [{"modality": "TEXT", "tokenCount": 7}]},
		"stop_sequence": "secret-e"
	}`)
	out, err := stripJSONDoc(doc)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(out, []byte("secret")) {
		t.Fatalf("content survived stripping:\n%s", out)
	}
	for _, keep := range []string{`"prompt_tokens": 7`, `"tokens": 2`, `"modality": "TEXT"`, `"finish_reason": "stop"`, `"model": "m"`} {
		if !bytes.Contains(out, []byte(keep)) {
			t.Errorf("stripped doc lost %s:\n%s", keep, out)
		}
	}
}
