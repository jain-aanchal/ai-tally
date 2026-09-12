// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"encoding/json"
	"io"
	"strings"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// CTO-167: provider-protocol metadata extraction.
//
// When a provider protocol is configured, the proxy reads a handful of scalar fields (model id,
// prompt/completion token counts) out of the relayed response and hangs them on the TraceRecord.
// This is metadata only: no prompt, no completion, no key is ever retained. The response still
// streams to the client byte-for-byte and unbuffered on the wire; we merely tee a bounded copy
// aside to parse the usage block, then discard it (see metaCapture). In pure pass-through mode
// (empty Provider) none of this runs and the hot path is byte-identical to CTO-39.

// responseMeta holds the scalar metadata pulled from one response. It never carries content.
//
// The token counts are pointers, not plain int64s, because "the provider did not report usage"
// (streaming, an error response, a body past the scan cap) and "the provider reported zero" are
// different facts and telemetry must not conflate them. A nil count is omitted from the emitted
// wire rather than serialized, so the proxy never invents the number; a fabricated 0 would read
// downstream as a real, free call. (Storage does not yet preserve the distinction: see the note in
// internal/telemetry on the pending gateway and schema change.)
type responseMeta struct {
	Model            string
	PromptTokens     *int64
	CompletionTokens *int64
	// CachedInputTokens is the portion of PromptTokens the provider served from a prompt cache
	// (Anthropic usage.cache_read_input_tokens, OpenAI usage.prompt_tokens_details.cached_tokens).
	// It is a SUBSET of PromptTokens, not an addition to it, which is the semantics the rest of the
	// product already uses (sdk/python/src/tally/pricing.py prices input - cached at the standard
	// rate and cached at the cached rate). Nil when the provider did not report it, which is not the
	// same fact as a reported 0 (CTO-349).
	CachedInputTokens *int64
}

// metaCaptureCap bounds how many response bytes we tee aside for parsing. A generateContent /
// chat.completion / messages JSON body is a few KB, so 1 MiB is comfortable headroom while still
// capping memory for a pathological (or streaming) response. Past the cap we stop capturing and
// fall back to whatever metadata we could derive without the body (e.g. Gemini's path-based model).
const metaCaptureCap = 1 << 20

// extractMeta parses provider metadata from a response body and, for Gemini, the request path.
// It is a pure function so the CTO-167 test can feed it a recorded response and assert the mapping.
// Unknown providers and unparseable bodies yield a zero responseMeta rather than an error; metadata
// is best-effort and must never fail the proxied request.
func extractMeta(p config.Provider, path string, body []byte) responseMeta {
	switch p {
	case config.ProviderOpenAI, config.ProviderAnthropic, config.ProviderGemini:
	default:
		return responseMeta{}
	}
	// A body that turns out to be an event stream is folded event by event (CTO-349). The request
	// path normally decides from the response Content-Type before any bytes arrive; this is the
	// fallback for an upstream that streamed without the header, and the entry point the fixture
	// tests use.
	if looksLikeSSE(body) {
		m := scanSSE(p, body)
		if p == config.ProviderGemini {
			m.Model = geminiModel(path, m.Model)
		}
		return m
	}
	switch p {
	case config.ProviderOpenAI:
		return openAIMeta(body)
	case config.ProviderAnthropic:
		return anthropicMeta(body)
	default:
		return geminiMeta(path, body)
	}
}

// openAIUsageDoc is the OpenAI usage block, shared by the single-document and streamed paths.
// Pointer fields throughout: an absent "usage" object, or an absent count inside it, must stay
// absent rather than decode to 0 (see responseMeta).
type openAIUsageDoc struct {
	PromptTokens        *int64 `json:"prompt_tokens"`
	CompletionTokens    *int64 `json:"completion_tokens"`
	PromptTokensDetails *struct {
		CachedTokens *int64 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
}

// anthropicUsageDoc is the Anthropic usage block. input_tokens counts ONLY the uncached prompt
// tokens: the two cache buckets are reported alongside it and are not included in it, which is why
// anthropicPrompt has to add them up (CTO-349).
type anthropicUsageDoc struct {
	InputTokens              *int64 `json:"input_tokens"`
	OutputTokens             *int64 `json:"output_tokens"`
	CacheCreationInputTokens *int64 `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     *int64 `json:"cache_read_input_tokens"`
}

// geminiUsageDoc is the Generative Language usageMetadata block.
//
// promptTokenCount and candidatesTokenCount are not the whole story, which is what CTO-375 and
// CTO-376 were about. See geminiCompletion for why totalTokenCount is carried too.
type geminiUsageDoc struct {
	PromptTokenCount     *int64 `json:"promptTokenCount"`
	CandidatesTokenCount *int64 `json:"candidatesTokenCount"`
	// CachedContentTokenCount is the context-cache share of the prompt (CTO-375). The API reference
	// is explicit that promptTokenCount "is still the total effective prompt size meaning this
	// includes the number of tokens in the cached content", so this is the OpenAI shape, not the
	// Anthropic one: it is a SUBSET of the prompt and must not be added to it.
	CachedContentTokenCount *int64 `json:"cachedContentTokenCount"`
	// ThoughtsTokenCount is reasoning tokens on thinking models (CTO-376). Billed: the thinking
	// docs state "response pricing is the sum of output tokens and thinking tokens".
	ThoughtsTokenCount *int64 `json:"thoughtsTokenCount"`
	// TotalTokenCount is documented as prompt + thoughts + candidates. Carried as the cross-check
	// in geminiCompletion rather than for its own sake.
	TotalTokenCount *int64 `json:"totalTokenCount"`
}

// geminiCompletion returns the billable output token count: generated candidates plus the thinking
// tokens billed at the same rate (CTO-376).
//
// WHY THIS IS NOT JUST candidates+thoughts. The API reference defines totalTokenCount as
// "prompt + thoughts + response candidates", which makes the two disjoint and the sum correct. But
// the inclusion is reported inconsistently in the field: Vertex AI excludes thinking tokens from
// candidatesTokenCount while the Generative Language API has been observed including them, and
// some models omit thoughtsTokenCount entirely while still charging for thinking. A blind sum
// therefore double-counts on whichever platform already folded them in, and double-counting a
// reasoning-heavy call is a large error in the direction of over-billing.
//
// So the sum is cross-checked against the provider's own total when it gave us one. total-prompt is
// thoughts+candidates by definition, whatever the reporting convention, so it is the authority: if
// candidates+thoughts exceeds it, candidates already contained the thoughts and adding them again
// would invent tokens.
//
// Returns nil when the provider reported no output count at all, which stays unknown rather than 0.
func geminiCompletion(candidates, thoughts, total, prompt *int64) *int64 {
	if candidates == nil && thoughts == nil {
		return nil
	}
	var summed int64
	if candidates != nil {
		summed += *candidates
	}
	if thoughts != nil {
		summed += *thoughts
	}
	if total != nil && prompt != nil {
		if billable := *total - *prompt; billable >= 0 && billable < summed {
			return &billable
		}
	}
	return &summed
}

func openAIMeta(body []byte) responseMeta {
	var r struct {
		Model string          `json:"model"`
		Usage *openAIUsageDoc `json:"usage"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		return responseMeta{}
	}
	m := responseMeta{Model: r.Model}
	if r.Usage != nil {
		m.PromptTokens = r.Usage.PromptTokens
		m.CompletionTokens = r.Usage.CompletionTokens
		if r.Usage.PromptTokensDetails != nil {
			m.CachedInputTokens = r.Usage.PromptTokensDetails.CachedTokens
		}
	}
	return m
}

func anthropicMeta(body []byte) responseMeta {
	var r struct {
		Model string             `json:"model"`
		Usage *anthropicUsageDoc `json:"usage"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		return responseMeta{}
	}
	m := responseMeta{Model: r.Model}
	if r.Usage != nil {
		m.PromptTokens = anthropicPrompt(
			r.Usage.InputTokens, r.Usage.CacheCreationInputTokens, r.Usage.CacheReadInputTokens)
		m.CompletionTokens = r.Usage.OutputTokens
		m.CachedInputTokens = r.Usage.CacheReadInputTokens
	}
	return m
}

// anthropicPrompt totals the three buckets Anthropic bills a prompt under (CTO-349).
//
// usage.input_tokens excludes both cache buckets, so reporting it alone under-counts a cached
// prompt by however much the cache served: a request that read 18923 tokens from cache and sent 21
// fresh ones was reported as 21 prompt tokens. The total is the honest token count, and
// CachedInputTokens carries the cache-read share so the gateway prices that share at the cached
// rate rather than the standard one.
//
// The bucket the record CANNOT represent is cache CREATION, which Anthropic bills at a premium over
// standard input. It is counted in this total (those tokens really were prompt input, and omitting
// them would under-count by 100% of the write rather than mis-rate it) but priced at the standard
// input rate, so a cache-write-heavy call is under-priced by the write premium. Representing it
// honestly needs a wire attribute and a price type that do not exist yet; see
// docs/anthropic-cache-tokens.md rather than a forced approximation here.
//
// Returns nil when the provider reported none of the three, because "no usage" and "zero prompt
// tokens" are different facts.
func anthropicPrompt(input, cacheCreate, cacheRead *int64) *int64 {
	if input == nil && cacheCreate == nil && cacheRead == nil {
		return nil
	}
	var total int64
	for _, v := range []*int64{input, cacheCreate, cacheRead} {
		if v != nil {
			total += *v
		}
	}
	return &total
}

func geminiMeta(path string, body []byte) responseMeta {
	var r struct {
		ModelVersion  string          `json:"modelVersion"`
		UsageMetadata *geminiUsageDoc `json:"usageMetadata"`
	}
	// The body may be missing/unparseable (e.g. an error response); still return the path-derived
	// model so an errored call is at least attributable to a model.
	_ = json.Unmarshal(body, &r)

	m := responseMeta{Model: geminiModel(path, r.ModelVersion)}
	if u := r.UsageMetadata; u != nil {
		m.PromptTokens = u.PromptTokenCount
		// CTO-375: promptTokenCount already includes the cached share, so this is recorded as the
		// subset it is and nothing is added to the total. Without it a 100k-token cached context
		// priced at the full input rate.
		m.CachedInputTokens = u.CachedContentTokenCount
		m.CompletionTokens = geminiCompletion(
			u.CandidatesTokenCount, u.ThoughtsTokenCount, u.TotalTokenCount, u.PromptTokenCount,
		)
	}
	return m
}

// geminiModel prefers the model named in the request path, falling back to the one the response
// reported. The path is authoritative for Generative Language calls and is available even when the
// body carried nothing parseable (an error response, a stream past the cap).
func geminiModel(path, fromBody string) string {
	if m := geminiModelFromPath(path); m != "" {
		return m
	}
	return fromBody
}

// geminiModelFromPath pulls the model id out of a Generative Language request path of the form
// /v1beta/models/{model}:generateContent (or :streamGenerateContent). Returns "" if the path
// doesn't match that shape.
func geminiModelFromPath(path string) string {
	const marker = "/models/"
	i := strings.LastIndex(path, marker)
	if i < 0 {
		return ""
	}
	rest := path[i+len(marker):]
	// Drop the ":method" suffix (":generateContent", ":streamGenerateContent", ...).
	if c := strings.IndexByte(rest, ':'); c >= 0 {
		rest = rest[:c]
	}
	// Guard against a trailing slash or empty segment.
	if s := strings.IndexByte(rest, '/'); s >= 0 {
		rest = rest[:s]
	}
	return rest
}

// metaCapture wraps a response body, streaming it through untouched while teeing a bounded copy
// aside so the usage block can be parsed on Close. It preserves the streaming contract: every Read
// returns the provider's bytes immediately, adding no buffering latency, and never retains content
// beyond the transient capture, which is freed as soon as the scalar metadata is extracted. The
// parsed result lands in *out; the raw bytes are discarded.
type metaCapture struct {
	inner    io.ReadCloser
	provider config.Provider
	path     string
	out      *responseMeta

	buf  []byte
	over bool // capture exceeded the cap; stop teeing and skip body-derived metadata

	// folder/scan are set instead of buf when the response is an event stream (CTO-349). A stream
	// is folded incrementally as it passes, so nothing is accumulated: a completion that streams for
	// minutes still yields its usage, and retained memory stays one SSE line rather than one body.
	folder *streamFolder
	scan   *sseScanner
}

// newMetaCapture wraps body for the given provider. stream selects the incremental SSE fold (the
// caller decides from the response Content-Type) over the buffered single-document scan.
func newMetaCapture(
	inner io.ReadCloser, p config.Provider, path string, stream bool, out *responseMeta,
) *metaCapture {
	m := &metaCapture{inner: inner, provider: p, path: path, out: out}
	if stream {
		m.folder = newStreamFolder(p)
		m.scan = m.folder.scanner()
	}
	return m
}

func (m *metaCapture) Read(p []byte) (int, error) {
	n, err := m.inner.Read(p)
	if n <= 0 {
		return n, err
	}
	if m.scan != nil {
		m.scan.write(p[:n])
		return n, err
	}
	if !m.over {
		if len(m.buf)+n > metaCaptureCap {
			// Oversized response: drop the partial capture rather than grow unbounded.
			m.over = true
			m.buf = nil
		} else {
			m.buf = append(m.buf, p[:n]...)
		}
	}
	return n, err
}

func (m *metaCapture) Close() error {
	if m.out != nil {
		switch {
		case m.scan != nil:
			// Flush a final event that arrived without its trailing newline, which is what a stream
			// cut short mid-event looks like. Whatever the provider reported before the cut stands;
			// what it never reported stays nil.
			m.scan.close()
			meta := m.folder.meta()
			if m.provider == config.ProviderGemini {
				meta.Model = geminiModel(m.path, meta.Model)
			}
			*m.out = meta
		case m.over:
			// We never saw the whole body; still try path-based metadata (Gemini model).
			*m.out = extractMeta(m.provider, m.path, nil)
		default:
			*m.out = extractMeta(m.provider, m.path, m.buf)
		}
	}
	m.buf = nil
	return m.inner.Close()
}
