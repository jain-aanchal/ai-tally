// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// providerUsage is the usage the provider reported to the client, read straight off the bytes the
// client received.
//
// It is decoded here with a separate, map-based reader instead of the proxy's own extractors. That
// separation is the point of CTO-350: a check that reused the proxy's parser could only prove the
// parser agrees with itself, which is the same gap the schema-built fixtures left open.
type providerUsage struct {
	Model         string
	ID            string
	FinishReasons []string

	// OpenAI usage.
	PromptTokens     *int64
	CompletionTokens *int64
	CachedTokens     *int64
	ReasoningTokens  *int64

	// Anthropic usage.
	InputTokens   *int64
	OutputTokens  *int64
	CacheCreation *int64
	CacheRead     *int64

	// Gemini usageMetadata.
	PromptTokenCount        *int64
	CandidatesTokenCount    *int64
	CachedContentTokenCount *int64
	ThoughtsTokenCount      *int64
	TotalTokenCount         *int64
}

// counts is the triple the TraceRecord carries. nil is unknown, never 0.
type counts struct {
	Prompt     *int64
	Completion *int64
	Cached     *int64
}

type sseEvent struct {
	Event string
	Data  string
}

// splitSSE splits an event stream into events. Multiple data: lines in one event join with a newline,
// as the SSE spec says.
func splitSSE(body []byte) []sseEvent {
	text := strings.ReplaceAll(string(body), "\r\n", "\n")
	var out []sseEvent
	for _, block := range strings.Split(text, "\n\n") {
		var ev sseEvent
		var data []string
		for _, line := range strings.Split(block, "\n") {
			switch {
			case strings.HasPrefix(line, "event:"):
				ev.Event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
			case strings.HasPrefix(line, "data:"):
				data = append(data, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
			}
		}
		if ev.Event == "" && len(data) == 0 {
			continue
		}
		ev.Data = strings.Join(data, "\n")
		out = append(out, ev)
	}
	return out
}

func isSSEBody(body []byte) bool {
	b := bytes.TrimLeft(body, " \t\r\n")
	return bytes.HasPrefix(b, []byte("data:")) || bytes.HasPrefix(b, []byte("event:"))
}

// decodeObject decodes one JSON object keeping numbers exact. nil when it is not an object.
func decodeObject(b []byte) map[string]any {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var m map[string]any
	if d.Decode(&m) != nil {
		return nil
	}
	return m
}

func obj(m map[string]any, key string) map[string]any {
	if m == nil {
		return nil
	}
	v, _ := m[key].(map[string]any)
	return v
}

func arr(m map[string]any, key string) []any {
	if m == nil {
		return nil
	}
	v, _ := m[key].([]any)
	return v
}

func str(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	v, _ := m[key].(string)
	return v
}

// num reads an integer count. An absent or non-numeric field is nil, so "not reported" survives.
func num(m map[string]any, key string) *int64 {
	if m == nil {
		return nil
	}
	n, ok := m[key].(json.Number)
	if !ok {
		return nil
	}
	v, err := n.Int64()
	if err != nil {
		return nil
	}
	return &v
}

func lastWins(dst **int64, v *int64) {
	if v != nil {
		*dst = v
	}
}

func setString(dst *string, v string) {
	if v != "" {
		*dst = v
	}
}

func (u *providerUsage) addFinish(reason string) {
	if reason == "" {
		return
	}
	if n := len(u.FinishReasons); n > 0 && u.FinishReasons[n-1] == reason {
		return
	}
	u.FinishReasons = append(u.FinishReasons, reason)
}

// parseProviderUsage reads the provider's usage from a response body, event by event for a stream.
// Counts are last-wins across events because every provider reports cumulative totals on its later
// events, never increments.
func parseProviderUsage(p config.Provider, body []byte, stream bool) providerUsage {
	var u providerUsage
	if stream || isSSEBody(body) {
		for _, ev := range splitSSE(body) {
			if ev.Data == "" || ev.Data == "[DONE]" {
				continue
			}
			if m := decodeObject([]byte(ev.Data)); m != nil {
				u.absorb(p, m)
			}
		}
		return u
	}
	if m := decodeObject(body); m != nil {
		u.absorb(p, m)
	}
	return u
}

func (u *providerUsage) absorb(p config.Provider, m map[string]any) {
	switch p {
	case config.ProviderOpenAI:
		setString(&u.Model, str(m, "model"))
		setString(&u.ID, str(m, "id"))
		for _, c := range arr(m, "choices") {
			if cm, ok := c.(map[string]any); ok {
				u.addFinish(str(cm, "finish_reason"))
			}
		}
		if us := obj(m, "usage"); us != nil {
			lastWins(&u.PromptTokens, num(us, "prompt_tokens"))
			lastWins(&u.CompletionTokens, num(us, "completion_tokens"))
			lastWins(&u.CachedTokens, num(obj(us, "prompt_tokens_details"), "cached_tokens"))
			lastWins(&u.ReasoningTokens, num(obj(us, "completion_tokens_details"), "reasoning_tokens"))
		}
	case config.ProviderAnthropic:
		// A non-streamed body is the message itself; message_start wraps it in "message".
		msg := m
		if inner := obj(m, "message"); inner != nil {
			msg = inner
		}
		setString(&u.Model, str(msg, "model"))
		if str(msg, "type") == "message" {
			setString(&u.ID, str(msg, "id"))
		}
		u.addFinish(str(msg, "stop_reason"))
		u.addFinish(str(obj(m, "delta"), "stop_reason"))
		for _, us := range []map[string]any{obj(msg, "usage"), obj(m, "usage")} {
			if us == nil {
				continue
			}
			lastWins(&u.InputTokens, num(us, "input_tokens"))
			lastWins(&u.OutputTokens, num(us, "output_tokens"))
			lastWins(&u.CacheCreation, num(us, "cache_creation_input_tokens"))
			lastWins(&u.CacheRead, num(us, "cache_read_input_tokens"))
		}
	case config.ProviderGemini:
		setString(&u.Model, str(m, "modelVersion"))
		setString(&u.ID, str(m, "responseId"))
		for _, c := range arr(m, "candidates") {
			if cm, ok := c.(map[string]any); ok {
				u.addFinish(str(cm, "finishReason"))
			}
		}
		if us := obj(m, "usageMetadata"); us != nil {
			lastWins(&u.PromptTokenCount, num(us, "promptTokenCount"))
			lastWins(&u.CandidatesTokenCount, num(us, "candidatesTokenCount"))
			lastWins(&u.CachedContentTokenCount, num(us, "cachedContentTokenCount"))
			lastWins(&u.ThoughtsTokenCount, num(us, "thoughtsTokenCount"))
			lastWins(&u.TotalTokenCount, num(us, "totalTokenCount"))
		}
	}
}

func sumPresent(vals ...*int64) *int64 {
	var total int64
	seen := false
	for _, v := range vals {
		if v != nil {
			total += *v
			seen = true
		}
	}
	if !seen {
		return nil
	}
	return &total
}

// expected derives what the TraceRecord should hold from the provider's own usage, following each
// provider's DOCUMENTED semantics (docs/anthropic-cache-tokens.md), not the proxy's code:
//
//   - OpenAI: prompt_tokens already includes the cached share.
//   - Anthropic: input_tokens excludes both cache buckets, so the prompt is the sum of all three and
//     the cached share is the cache-read bucket.
//   - Gemini: promptTokenCount includes the cached share, and totalTokenCount - promptTokenCount is
//     thoughts + candidates under either reporting convention, so it is the billable output when a
//     total was reported.
//
// The notes flag anything worth a human's eye that is not itself a mismatch, such as which Gemini
// reporting convention the live endpoint actually used.
func (u providerUsage) expected(p config.Provider) (counts, []string) {
	var notes []string
	switch p {
	case config.ProviderOpenAI:
		return counts{Prompt: u.PromptTokens, Completion: u.CompletionTokens, Cached: u.CachedTokens}, nil
	case config.ProviderAnthropic:
		return counts{
			Prompt:     sumPresent(u.InputTokens, u.CacheCreation, u.CacheRead),
			Completion: u.OutputTokens,
			Cached:     u.CacheRead,
		}, nil
	case config.ProviderGemini:
		c := counts{Prompt: u.PromptTokenCount, Cached: u.CachedContentTokenCount}
		summed := sumPresent(u.CandidatesTokenCount, u.ThoughtsTokenCount)
		if u.TotalTokenCount != nil && u.PromptTokenCount != nil {
			billable := *u.TotalTokenCount - *u.PromptTokenCount
			c.Completion = &billable
			if summed != nil && *summed != billable {
				notes = append(notes, fmt.Sprintf(
					"gemini convention: candidates+thoughts=%d but total-prompt=%d", *summed, billable))
			}
		} else {
			c.Completion = summed
		}
		return c, notes
	}
	return counts{}, nil
}

// thoughts is the reasoning-token figure for the report column: Gemini thoughtsTokenCount, or OpenAI
// reasoning_tokens (already inside completion_tokens). Anthropic reports none separately.
func (u providerUsage) thoughts(p config.Provider) *int64 {
	switch p {
	case config.ProviderOpenAI:
		return u.ReasoningTokens
	case config.ProviderGemini:
		return u.ThoughtsTokenCount
	}
	return nil
}

// raw renders the provider's own field names and values, which is what a human reads off a provider
// dashboard or log line.
func (u providerUsage) raw(p config.Provider) string {
	f := func(name string, v *int64) string { return name + "=" + fmtCount(v) }
	switch p {
	case config.ProviderOpenAI:
		return strings.Join([]string{f("prompt_tokens", u.PromptTokens), f("completion_tokens", u.CompletionTokens),
			f("cached_tokens", u.CachedTokens), f("reasoning_tokens", u.ReasoningTokens)}, " ")
	case config.ProviderAnthropic:
		return strings.Join([]string{f("input_tokens", u.InputTokens), f("cache_creation_input_tokens", u.CacheCreation),
			f("cache_read_input_tokens", u.CacheRead), f("output_tokens", u.OutputTokens)}, " ")
	case config.ProviderGemini:
		return strings.Join([]string{f("promptTokenCount", u.PromptTokenCount), f("candidatesTokenCount", u.CandidatesTokenCount),
			f("thoughtsTokenCount", u.ThoughtsTokenCount), f("cachedContentTokenCount", u.CachedContentTokenCount),
			f("totalTokenCount", u.TotalTokenCount)}, " ")
	}
	return ""
}

// errorSummary names a provider error by its type or code only. The message field is dropped: some
// providers echo request content into it, and the report must not print a prompt.
func errorSummary(body []byte) string {
	m := decodeObject(body)
	e := obj(m, "error")
	if e == nil {
		return ""
	}
	var parts []string
	for _, k := range []string{"type", "code", "status"} {
		if v := str(e, k); v != "" {
			parts = append(parts, v)
		}
	}
	if n := num(e, "code"); n != nil {
		parts = append(parts, fmt.Sprint(*n))
	}
	return strings.Join(parts, "/")
}

func fmtCount(v *int64) string {
	if v == nil {
		return "nil"
	}
	return fmt.Sprint(*v)
}

// diffCounts compares one field. Both nil is agreement (both say unknown); one nil is a mismatch,
// because the invariant is that unknown stays unknown and known stays known.
func diffCounts(field string, want, got *int64) string {
	switch {
	case want == nil && got == nil:
		return ""
	case want == nil || got == nil:
		return fmt.Sprintf("%s provider=%s proxy=%s", field, fmtCount(want), fmtCount(got))
	case *want != *got:
		return fmt.Sprintf("%s provider=%d proxy=%d (delta %+d)", field, *want, *got, *got-*want)
	}
	return ""
}

func compareCounts(want, got counts) []string {
	var out []string
	for _, d := range []string{
		diffCounts("prompt", want.Prompt, got.Prompt),
		diffCounts("completion", want.Completion, got.Completion),
		diffCounts("cached", want.Cached, got.Cached),
	} {
		if d != "" {
			out = append(out, d)
		}
	}
	return out
}
