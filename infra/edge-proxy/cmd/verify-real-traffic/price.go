// SPDX-License-Identifier: Apache-2.0
package main

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// rate is one model's price in integer micro-USD per million tokens. Money is never a float in this
// codebase, and a spend cap is exactly the place a float rounding error would turn "refuse" into
// "overrun by a hair".
type rate struct {
	Input       int64
	CachedInput int64
	Output      int64
}

// priceTable mirrors the token rows of the seed price catalog in sdk/python/src/tally/pricing.py for
// the models this harness can be pointed at. Go cannot import the Python catalog, so the rows are
// copied, and TestPriceTableMatchesCatalog fails the moment the two drift apart (CTO-350). A model
// that is not listed here is refused before any call is sent: without a rate there is no way to
// bound the spend, and an unbounded run is what the cap exists to prevent.
var priceTable = map[config.Provider]map[string]rate{
	config.ProviderOpenAI: {
		"gpt-4o-mini": {Input: 150_000, CachedInput: 75_000, Output: 600_000},
		"gpt-4o":      {Input: 2_500_000, CachedInput: 1_250_000, Output: 10_000_000},
		"gpt-5-mini":  {Input: 250_000, CachedInput: 25_000, Output: 2_000_000},
	},
	config.ProviderAnthropic: {
		"claude-haiku-4-5":  {Input: 1_000_000, CachedInput: 100_000, Output: 5_000_000},
		"claude-sonnet-4-5": {Input: 3_000_000, CachedInput: 300_000, Output: 15_000_000},
	},
	config.ProviderGemini: {
		"gemini-2.5-flash": {Input: 300_000, CachedInput: 75_000, Output: 2_500_000},
		"gemini-2.5-pro":   {Input: 1_250_000, CachedInput: 310_000, Output: 10_000_000},
	},
}

// catalogProvider is the provider string the Python catalog keys Gemini under. The proxy calls the
// protocol "gemini"; the catalog and gen_ai.system call the vendor "google".
func catalogProvider(p config.Provider) string {
	if p == config.ProviderGemini {
		return "google"
	}
	return string(p)
}

func lookupRate(p config.Provider, model string) (rate, bool) {
	r, ok := priceTable[p][model]
	return r, ok
}

// costMicro prices tokens at a per-million rate, rounding UP. Rounding down would let many small calls
// each shave a fraction of a micro-dollar off the cap accounting.
func costMicro(tokens, perMillion int64) int64 {
	if tokens <= 0 || perMillion <= 0 {
		return 0
	}
	return (tokens*perMillion + 999_999) / 1_000_000
}

// cacheWriteRate applies Anthropic's 5-minute cache-write premium (1.25x input), rounded up. The seed
// catalog has no cache-write price type (docs/anthropic-cache-tokens.md), so the cap derives it here
// rather than under-bounding a cache-warming call at the plain input rate.
func cacheWriteRate(input int64) int64 {
	return (input*5 + 3) / 4
}

// worstCaseMicro bounds what one call can cost BEFORE it is sent.
//
// The prompt bound is the request body's byte length. Every tokenizer these providers use emits at
// least one byte per token, so a body of N bytes cannot bill more than N prompt tokens, and the JSON
// framing only makes the bound looser. It overestimates a plain English prompt by roughly 4x, which
// is the right direction for a cap. The output bound is the call's own max output tokens plus any
// thinking budget, both of which the request sets.
func worstCaseMicro(s callSpec, r rate) int64 {
	in := r.Input
	if s.CacheWrite {
		in = cacheWriteRate(in)
	}
	return costMicro(int64(len(s.Body)), in) + costMicro(s.MaxOutput, r.Output)
}

// spentMicro is the cap accounting for a call that has been sent. It uses the counts the provider
// reported, still at the uncached (and for a cache write, premium) rate, so the running total stays an
// upper bound. Where the provider reported nothing, the pre-call bound stands in: an unknown count is
// not a free call, and treating it as 0 would let the cap under-count.
func spentMicro(s callSpec, want counts, r rate) int64 {
	prompt := int64(len(s.Body))
	if want.Prompt != nil {
		prompt = *want.Prompt
	}
	completion := s.MaxOutput
	if want.Completion != nil {
		completion = *want.Completion
	}
	in := r.Input
	if s.CacheWrite {
		in = cacheWriteRate(in)
	}
	return costMicro(prompt, in) + costMicro(completion, r.Output)
}

// parseUSD turns "0.50" or "$0.50" into micro-USD without ever touching a float.
func parseUSD(s string) (int64, error) {
	s = strings.TrimPrefix(strings.TrimSpace(s), "$")
	if s == "" {
		return 0, fmt.Errorf("empty amount")
	}
	whole, frac, hasDot := strings.Cut(s, ".")
	if whole == "" {
		whole = "0"
	}
	if hasDot && frac == "" {
		return 0, fmt.Errorf("invalid amount %q", s)
	}
	if len(frac) > 6 {
		return 0, fmt.Errorf("amount %q has more precision than one micro-dollar", s)
	}
	for _, part := range []string{whole, frac} {
		for _, c := range part {
			if c < '0' || c > '9' {
				return 0, fmt.Errorf("invalid amount %q", s)
			}
		}
	}
	w, err := strconv.ParseInt(whole, 10, 64)
	if err != nil || w > 1_000_000 {
		return 0, fmt.Errorf("invalid amount %q", s)
	}
	frac += strings.Repeat("0", 6-len(frac))
	f, err := strconv.ParseInt(frac, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid amount %q", s)
	}
	return w*1_000_000 + f, nil
}

// formatUSD renders micro-USD for humans. Display only; nothing is computed from the string.
func formatUSD(micro int64) string {
	return fmt.Sprintf("$%d.%06d", micro/1_000_000, micro%1_000_000)
}
