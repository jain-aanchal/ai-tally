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
// This is metadata only — no prompt, no completion, no key is ever retained. The response still
// streams to the client byte-for-byte and unbuffered on the wire; we merely tee a bounded copy
// aside to parse the usage block, then discard it (see metaCapture). In pure pass-through mode
// (empty Provider) none of this runs and the hot path is byte-identical to CTO-39.

// responseMeta holds the scalar metadata pulled from one response. It never carries content.
type responseMeta struct {
	Model            string
	PromptTokens     int64
	CompletionTokens int64
}

// metaCaptureCap bounds how many response bytes we tee aside for parsing. A generateContent /
// chat.completion / messages JSON body is a few KB, so 1 MiB is comfortable headroom while still
// capping memory for a pathological (or streaming) response. Past the cap we stop capturing and
// fall back to whatever metadata we could derive without the body (e.g. Gemini's path-based model).
const metaCaptureCap = 1 << 20

// extractMeta parses provider metadata from a response body and, for Gemini, the request path.
// It is a pure function so the CTO-167 test can feed it a recorded response and assert the mapping.
// Unknown providers and unparseable bodies yield a zero responseMeta rather than an error — metadata
// is best-effort and must never fail the proxied request.
func extractMeta(p config.Provider, path string, body []byte) responseMeta {
	switch p {
	case config.ProviderOpenAI:
		return openAIMeta(body)
	case config.ProviderAnthropic:
		return anthropicMeta(body)
	case config.ProviderGemini:
		return geminiMeta(path, body)
	default:
		return responseMeta{}
	}
}

func openAIMeta(body []byte) responseMeta {
	var r struct {
		Model string `json:"model"`
		Usage struct {
			PromptTokens     int64 `json:"prompt_tokens"`
			CompletionTokens int64 `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		return responseMeta{}
	}
	return responseMeta{
		Model:            r.Model,
		PromptTokens:     r.Usage.PromptTokens,
		CompletionTokens: r.Usage.CompletionTokens,
	}
}

func anthropicMeta(body []byte) responseMeta {
	var r struct {
		Model string `json:"model"`
		Usage struct {
			InputTokens  int64 `json:"input_tokens"`
			OutputTokens int64 `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		return responseMeta{}
	}
	return responseMeta{
		Model:            r.Model,
		PromptTokens:     r.Usage.InputTokens,
		CompletionTokens: r.Usage.OutputTokens,
	}
}

func geminiMeta(path string, body []byte) responseMeta {
	var r struct {
		ModelVersion  string `json:"modelVersion"`
		UsageMetadata struct {
			PromptTokenCount     int64 `json:"promptTokenCount"`
			CandidatesTokenCount int64 `json:"candidatesTokenCount"`
		} `json:"usageMetadata"`
	}
	// The body may be missing/unparseable (e.g. an error response); still return the path-derived
	// model so an errored call is at least attributable to a model.
	_ = json.Unmarshal(body, &r)

	model := geminiModelFromPath(path)
	if model == "" {
		model = r.ModelVersion
	}
	return responseMeta{
		Model:            model,
		PromptTokens:     r.UsageMetadata.PromptTokenCount,
		CompletionTokens: r.UsageMetadata.CandidatesTokenCount,
	}
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
// aside so the usage block can be parsed on Close. It preserves the streaming contract — every Read
// returns the provider's bytes immediately, adding no buffering latency — and never retains content
// beyond the transient capture, which is freed as soon as the scalar metadata is extracted. The
// parsed result lands in *out; the raw bytes are discarded.
type metaCapture struct {
	inner    io.ReadCloser
	provider config.Provider
	path     string
	out      *responseMeta

	buf  []byte
	over bool // capture exceeded the cap; stop teeing and skip body-derived metadata
}

func (m *metaCapture) Read(p []byte) (int, error) {
	n, err := m.inner.Read(p)
	if n > 0 && !m.over {
		if len(m.buf)+n > metaCaptureCap {
			// Oversized/streaming response: drop the partial capture rather than grow unbounded.
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
		if m.over {
			// We never saw the whole body; still try path-based metadata (Gemini model).
			*m.out = extractMeta(m.provider, m.path, nil)
		} else {
			*m.out = extractMeta(m.provider, m.path, m.buf)
		}
	}
	m.buf = nil
	return m.inner.Close()
}
