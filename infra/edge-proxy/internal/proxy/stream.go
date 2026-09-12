// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"bytes"
	"encoding/json"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-349: usage extraction from streamed (SSE) responses.
//
// Streaming is the common case for chat, so before this the majority of proxied traffic reached
// storage with no counts at all: the metadata scanner only understood a single JSON document, and a
// stream is a sequence of events whose usage arrives in one or two of them. The counts were never
// fabricated (the invariant held), but a blank where real spend belongs is still the wrong answer
// when the number IS on the wire.
//
// The scan is incremental rather than buffered. A completion can stream for minutes and run to
// megabytes, so the body is never accumulated: bytes pass through to the client untouched and the
// scanner keeps only the current SSE line, parsing each event's small JSON payload as the line
// completes and folding the scalar counts into a running responseMeta. Retained memory is therefore
// one event, not one response, and a stream of any length still yields its usage. Content never
// outlives the line buffer: only the scalar counts and the model id are kept.

// maxSSELineBytes bounds the single-event line buffer. Published event payloads are a few KB at the
// outside (a tool-use input_json_delta is the largest), so 1 MiB is generous headroom while keeping
// a pathological or non-SSE body from growing the buffer without limit. A line past the cap is
// skipped, not truncated-and-parsed: a half-read JSON object could decode to a plausible-looking
// wrong number, and a missing count is the honest answer where a wrong one is not.
const maxSSELineBytes = metaCaptureCap

// sseScanner feeds provider event payloads to fold as they arrive. It is not safe for concurrent
// use; one scanner belongs to one response body.
type sseScanner struct {
	fold func(payload []byte)

	line    []byte
	dropped bool // current line exceeded maxSSELineBytes; skip it to the next newline
	// capHit records that at least one event was skipped for length, so a caller can tell "the
	// provider sent no usage" apart from "we declined to scan the event that may have held it".
	capHit bool
}

// write feeds the next chunk of response bytes. It copies nothing beyond the current partial line.
func (s *sseScanner) write(p []byte) {
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		if i < 0 {
			s.appendLine(p)
			return
		}
		s.appendLine(p[:i])
		s.emit()
		p = p[i+1:]
	}
}

func (s *sseScanner) appendLine(b []byte) {
	if s.dropped {
		return
	}
	if len(s.line)+len(b) > maxSSELineBytes {
		s.dropped = true
		s.capHit = true
		s.line = s.line[:0]
		return
	}
	s.line = append(s.line, b...)
}

// emit hands the completed line to fold when it is a data: field, then resets the line buffer.
func (s *sseScanner) emit() {
	line, dropped := s.line, s.dropped
	s.line, s.dropped = s.line[:0], false
	if dropped {
		return
	}
	line = bytes.TrimSuffix(line, []byte("\r"))
	rest, ok := bytes.CutPrefix(line, []byte("data:"))
	if !ok {
		// event:, id:, retry:, a comment, or the blank line between events. Only data carries the
		// JSON payload; the event: name is redundant with the payload's own "type" field.
		return
	}
	rest = bytes.TrimSpace(rest)
	// OpenAI terminates with a literal "data: [DONE]" sentinel, which is not JSON.
	if len(rest) == 0 || bytes.Equal(rest, []byte("[DONE]")) {
		return
	}
	s.fold(rest)
}

// close flushes a trailing line that arrived without its newline, which is what a stream cut short
// mid-event looks like (client disconnect, upstream reset).
func (s *sseScanner) close() {
	if len(s.line) > 0 || s.dropped {
		s.emit()
	}
}

// looksLikeSSE reports whether a body is an event stream rather than a single JSON document. It is
// the fallback for a response whose Content-Type did not say text/event-stream; the streaming path
// normally decides from the header, before any body arrives.
func looksLikeSSE(body []byte) bool {
	b := bytes.TrimLeft(body, " \t\r\n")
	return bytes.HasPrefix(b, []byte("data:")) || bytes.HasPrefix(b, []byte("event:"))
}

// streamFolder accumulates the scalar usage a provider spreads across its event sequence.
//
// Every count is a pointer and starts nil, and a field the provider never sent stays nil all the way
// to the TraceRecord: a stream the client abandoned, a provider that omitted usage, and an event we
// skipped for length are all "unknown", never 0.
type streamFolder struct {
	provider config.Provider
	model    string

	// OpenAI / Gemini report the totals directly; last event carrying the field wins, since these
	// counts are cumulative-final rather than incremental.
	prompt     *int64
	completion *int64
	cached     *int64

	// Anthropic splits the prompt across three separately billed buckets, folded in anthropicPrompt.
	anthInput       *int64
	anthCacheCreate *int64
	anthCacheRead   *int64

	// Gemini reports thinking tokens outside candidatesTokenCount, folded in geminiCompletion
	// (CTO-376). The total rides along as that fold's cross-check against double counting.
	gemThoughts *int64
	gemTotal    *int64
}

func newStreamFolder(p config.Provider) *streamFolder { return &streamFolder{provider: p} }

func (f *streamFolder) scanner() *sseScanner { return &sseScanner{fold: f.feed} }

// feed parses one event payload. An unparseable payload is skipped rather than failing the scan:
// metadata is best-effort and must never affect the proxied request.
func (f *streamFolder) feed(payload []byte) {
	switch f.provider {
	case config.ProviderOpenAI:
		f.feedOpenAI(payload)
	case config.ProviderAnthropic:
		f.feedAnthropic(payload)
	case config.ProviderGemini:
		f.feedGemini(payload)
	}
}

// feedOpenAI reads a chat.completion.chunk. The model is on every chunk; the usage block rides on a
// single final chunk, the one whose choices array is empty.
//
// That final chunk exists ONLY when the request set stream_options.include_usage. Without the
// opt-in the counts are on no chunk at all, so they stay nil and the span lands unpriced with its
// model known. That is the honest answer and the only available one: we do not estimate tokens from
// the streamed text, and we deliberately do not rewrite the customer's request to add the opt-in,
// which would mutate a body the proxy promises to forward byte-for-byte and change what their own
// SDK sees on the stream. Getting those spans priced is a one-line change at the caller.
func (f *streamFolder) feedOpenAI(payload []byte) {
	var c struct {
		Model string          `json:"model"`
		Usage *openAIUsageDoc `json:"usage"`
	}
	if json.Unmarshal(payload, &c) != nil {
		return
	}
	if c.Model != "" {
		f.model = c.Model
	}
	// "usage": null rides on every non-final chunk, so an absent object must not clear what a
	// previous event established.
	if c.Usage != nil {
		setIfNotNil(&f.prompt, c.Usage.PromptTokens)
		setIfNotNil(&f.completion, c.Usage.CompletionTokens)
		if c.Usage.PromptTokensDetails != nil {
			setIfNotNil(&f.cached, c.Usage.PromptTokensDetails.CachedTokens)
		}
	}
}

// feedAnthropic reads one Messages SSE event. Input tokens and the model arrive on message_start,
// the final output count on message_delta, and the two cache buckets on either, so the fold has to
// span events rather than read one of them.
func (f *streamFolder) feedAnthropic(payload []byte) {
	var e struct {
		Message *struct {
			Model string             `json:"model"`
			Usage *anthropicUsageDoc `json:"usage"`
		} `json:"message"`
		Usage *anthropicUsageDoc `json:"usage"`
	}
	if json.Unmarshal(payload, &e) != nil {
		return
	}
	if e.Message != nil {
		if e.Message.Model != "" {
			f.model = e.Message.Model
		}
		f.foldAnthropicUsage(e.Message.Usage)
	}
	f.foldAnthropicUsage(e.Usage)
}

func (f *streamFolder) foldAnthropicUsage(u *anthropicUsageDoc) {
	if u == nil {
		return
	}
	setIfNotNil(&f.anthInput, u.InputTokens)
	setIfNotNil(&f.anthCacheCreate, u.CacheCreationInputTokens)
	setIfNotNil(&f.anthCacheRead, u.CacheReadInputTokens)
	// message_start reports the output tokens generated so far (typically 1) and message_delta the
	// cumulative total, so last-wins lands on the final count. A stream cut off after message_start
	// keeps the partial count the provider actually reported rather than discarding it.
	setIfNotNil(&f.completion, u.OutputTokens)
}

// feedGemini reads a streamGenerateContent chunk. usageMetadata repeats on every chunk with the
// running totals, and the trailing chunk carries the final ones.
func (f *streamFolder) feedGemini(payload []byte) {
	var c struct {
		ModelVersion  string          `json:"modelVersion"`
		UsageMetadata *geminiUsageDoc `json:"usageMetadata"`
	}
	if json.Unmarshal(payload, &c) != nil {
		return
	}
	if c.ModelVersion != "" {
		f.model = c.ModelVersion
	}
	if u := c.UsageMetadata; u != nil {
		setIfNotNil(&f.prompt, u.PromptTokenCount)
		// The first chunks carry promptTokenCount with no candidatesTokenCount yet; an absent count
		// must not overwrite one a later chunk supplied (nor invent a 0 for the early chunks).
		setIfNotNil(&f.completion, u.CandidatesTokenCount)
		// CTO-375/376: same last-wins rule. The cached share is fixed for the whole stream, while
		// thoughts and the total climb with each chunk, so the trailing chunk wins and the fold in
		// meta() runs once over the final numbers rather than per chunk.
		setIfNotNil(&f.cached, u.CachedContentTokenCount)
		setIfNotNil(&f.gemThoughts, u.ThoughtsTokenCount)
		setIfNotNil(&f.gemTotal, u.TotalTokenCount)
	}
}

// meta renders the folded state as a responseMeta.
func (f *streamFolder) meta() responseMeta {
	m := responseMeta{Model: f.model, CompletionTokens: f.completion}
	if f.provider == config.ProviderAnthropic {
		m.PromptTokens = anthropicPrompt(f.anthInput, f.anthCacheCreate, f.anthCacheRead)
		m.CachedInputTokens = f.anthCacheRead
		return m
	}
	m.PromptTokens = f.prompt
	m.CachedInputTokens = f.cached
	if f.provider == config.ProviderGemini {
		// CTO-376: fold the thinking tokens into the output count, with the same double-count guard
		// the non-streamed path uses. A streamed reasoning-heavy call is the common case here.
		m.CompletionTokens = geminiCompletion(f.completion, f.gemThoughts, f.gemTotal, f.prompt)
	}
	return m
}

// setIfNotNil copies v into *dst when the provider actually reported it, leaving a previously
// established value (and the nil-means-unknown default) alone otherwise.
func setIfNotNil(dst **int64, v *int64) {
	if v != nil {
		*dst = v
	}
}

// scanSSE folds a complete SSE body in one pass. Used for the non-streaming fallback in extractMeta
// and by the tests that feed a recorded fixture; the request path uses the incremental scanner.
func scanSSE(p config.Provider, body []byte) responseMeta {
	f := newStreamFolder(p)
	s := f.scanner()
	s.write(body)
	s.close()
	return f.meta()
}
