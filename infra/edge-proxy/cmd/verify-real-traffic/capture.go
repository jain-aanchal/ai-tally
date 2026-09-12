// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Capture writes each real response into testdata as a fixture, with every piece of content removed
// (CTO-350). The no-bodies-in-telemetry invariant applies to fixtures too: a committed fixture is
// storage, and a prompt or completion in the repository is a body that reached storage.
//
// Stripping is an ALLOWLIST. A denylist of "content", "text", "parts" and so on would pass through the
// next content-bearing field a provider adds, and providers adding fields without notice is the very
// thing CTO-350 is checking for. Anything not named below is dropped.

// keepScalars are identifiers and reasons, never content. stop_sequence is deliberately absent: it
// echoes a caller-supplied string.
var keepScalars = map[string]bool{
	"id": true, "object": true, "created": true, "model": true, "modelVersion": true,
	"responseId": true, "type": true, "index": true, "role": true,
	"finish_reason": true, "finishReason": true, "stop_reason": true,
}

// descendKeys are containers that hold the scalars above (or the usage block) alongside content.
var descendKeys = map[string]bool{
	"choices": true, "candidates": true, "message": true, "delta": true,
}

// usageKeys are kept whole, minus any string that is not a known enum.
var usageKeys = map[string]bool{"usage": true, "usageMetadata": true}

// usageStrings are the only string values a usage block may keep.
var usageStrings = map[string]bool{"modality": true, "service_tier": true}

func strip(v any) any {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	out := map[string]any{}
	for k, val := range m {
		switch {
		case usageKeys[k]:
			if u := stripUsage(val); u != nil {
				out[k] = u
			}
		case descendKeys[k]:
			switch t := val.(type) {
			case map[string]any:
				out[k] = strip(t)
			case []any:
				items := make([]any, 0, len(t))
				for _, item := range t {
					if s := strip(item); s != nil {
						items = append(items, s)
					}
				}
				out[k] = items
			}
		case keepScalars[k]:
			switch val.(type) {
			case string, json.Number, bool, nil:
				out[k] = val
			}
		}
	}
	return out
}

func stripUsage(v any) any {
	switch t := v.(type) {
	case json.Number, bool:
		return t
	case map[string]any:
		out := map[string]any{}
		for k, val := range t {
			if s, isStr := val.(string); isStr {
				if usageStrings[k] {
					out[k] = s
				}
				continue
			}
			if u := stripUsage(val); u != nil {
				out[k] = u
			}
		}
		return out
	case []any:
		items := make([]any, 0, len(t))
		for _, item := range t {
			if u := stripUsage(item); u != nil {
				items = append(items, u)
			}
		}
		return items
	}
	return nil
}

// stripJSONDoc strips a single-document response.
func stripJSONDoc(body []byte) ([]byte, error) {
	m := decodeObject(body)
	if m == nil {
		return nil, fmt.Errorf("response is not a JSON object")
	}
	out, err := json.MarshalIndent(strip(m), "", "  ")
	if err != nil {
		return nil, err
	}
	return append(out, '\n'), nil
}

// stripSSE strips an event stream and keeps only the events that carry metadata: the first event (the
// model and id), any event with usage or a finish reason, the stream terminators, and nothing else.
// The dozens of pure text-delta events in between hold nothing a parser test needs.
func stripSSE(body []byte) ([]byte, error) {
	var buf bytes.Buffer
	for i, ev := range splitSSE(body) {
		if ev.Data == "[DONE]" {
			buf.WriteString("data: [DONE]\n\n")
			continue
		}
		m := decodeObject([]byte(ev.Data))
		if m == nil {
			continue
		}
		s, _ := strip(m).(map[string]any)
		keep := i == 0 || ev.Event == "message_start" || ev.Event == "message_delta" ||
			ev.Event == "message_stop" || carriesMetadata(s)
		if !keep {
			continue
		}
		data, err := json.Marshal(s)
		if err != nil {
			return nil, err
		}
		if ev.Event != "" {
			fmt.Fprintf(&buf, "event: %s\n", ev.Event)
		}
		fmt.Fprintf(&buf, "data: %s\n\n", data)
	}
	if buf.Len() == 0 {
		return nil, fmt.Errorf("stream had no metadata-bearing events")
	}
	return buf.Bytes(), nil
}

func carriesMetadata(m map[string]any) bool {
	if m == nil {
		return false
	}
	for k, v := range m {
		switch {
		case usageKeys[k] && v != nil:
			return true
		case (k == "finish_reason" || k == "finishReason" || k == "stop_reason") && v != nil:
			return true
		case descendKeys[k]:
			switch t := v.(type) {
			case map[string]any:
				if carriesMetadata(t) {
					return true
				}
			case []any:
				for _, item := range t {
					if im, ok := item.(map[string]any); ok && carriesMetadata(im) {
						return true
					}
				}
			}
		}
	}
	return false
}

// sidecar is written next to each captured fixture. It holds what the TraceRecord SHOULD contain,
// derived from the provider's own usage, so internal/proxy's TestCapturedRealTrafficFixtures turns
// every committed capture into a regression test without anyone transcribing numbers by hand.
type sidecar struct {
	Comment           string `json:"_comment"`
	CapturedAt        string `json:"captured_at"`
	Fixture           string `json:"fixture"`
	RequestPath       string `json:"request_path"`
	RequestID         string `json:"request_id,omitempty"`
	Model             string `json:"model"`
	PromptTokens      *int64 `json:"prompt_tokens"`
	CompletionTokens  *int64 `json:"completion_tokens"`
	CachedInputTokens *int64 `json:"cached_input_tokens"`
}

// writeCapture writes the stripped fixture and its sidecar. It returns the fixture path.
func writeCapture(dir string, r *callResult, now time.Time) (string, error) {
	var stripped []byte
	var err error
	ext := ".json"
	if r.stream {
		ext = ".sse"
		stripped, err = stripSSE(r.body)
	} else {
		stripped, err = stripJSONDoc(r.body)
	}
	if err != nil {
		return "", err
	}
	provDir := filepath.Join(dir, string(r.Spec.Provider))
	if err := os.MkdirAll(provDir, 0o755); err != nil {
		return "", err
	}
	base := "real_" + r.Spec.Name
	fixture := filepath.Join(provDir, base+ext)
	if err := os.WriteFile(fixture, stripped, 0o644); err != nil {
		return "", err
	}
	path, _, _ := strings.Cut(r.Spec.Path, "?")
	sc := sidecar{
		Comment: "CAPTURED from real provider traffic by cmd/verify-real-traffic (CTO-350). Content " +
			"stripped by allowlist; only ids, model, finish reasons and usage remain. The counts below " +
			"are derived from the provider's own usage block per its documented semantics.",
		CapturedAt:        now.UTC().Format(time.RFC3339),
		Fixture:           base + ext,
		RequestPath:       path,
		RequestID:         r.RequestID,
		Model:             r.wantModel(),
		PromptTokens:      r.Want.Prompt,
		CompletionTokens:  r.Want.Completion,
		CachedInputTokens: r.Want.Cached,
	}
	out, err := json.MarshalIndent(sc, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(provDir, base+".expected.json"), append(out, '\n'), 0o644); err != nil {
		return "", err
	}
	return fixture, nil
}
