// SPDX-License-Identifier: Apache-2.0
package main

import (
	"os"
	"path/filepath"
	"regexp"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

func TestParseUSD(t *testing.T) {
	good := map[string]int64{
		"0.50": 500_000, "$0.50": 500_000, "1": 1_000_000, ".25": 250_000, "0.000001": 1, "12.3": 12_300_000,
	}
	for in, want := range good {
		got, err := parseUSD(in)
		if err != nil || got != want {
			t.Errorf("parseUSD(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
	for _, in := range []string{"", "-1", "0.0000001", "abc", "1.", "1e3", "0,5"} {
		if _, err := parseUSD(in); err == nil {
			t.Errorf("parseUSD(%q) accepted a bad amount", in)
		}
	}
}

func TestCostRoundsUp(t *testing.T) {
	// 1 token at $0.15/MTok is 0.15 micro-USD; the cap must count it as 1, never 0.
	if got := costMicro(1, 150_000); got != 1 {
		t.Errorf("costMicro(1, 150000) = %d, want 1", got)
	}
	if got := costMicro(1_000_000, 150_000); got != 150_000 {
		t.Errorf("costMicro(1M, 150000) = %d, want 150000", got)
	}
	if got := cacheWriteRate(1_000_000); got != 1_250_000 {
		t.Errorf("cacheWriteRate = %d, want 1250000", got)
	}
}

// TestPriceTableMatchesCatalog keeps the spend cap honest: it fails when a rate here drifts from the
// seed catalog the rest of the product prices with (CTO-350). It skips only when the Python source is
// not present, as in a build context that copied the edge proxy alone.
func TestPriceTableMatchesCatalog(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "sdk", "python", "src", "tally", "pricing.py")
	src, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("catalog source not available (%v)", err)
	}
	row := regexp.MustCompile(`\("(openai|anthropic|google)", "([^"]+)", PriceType\.(INPUT|CACHED_INPUT|OUTPUT), "([0-9.]+)"\)`)
	catalog := map[string]map[string]int64{}
	for _, m := range row.FindAllStringSubmatch(string(src), -1) {
		micro, err := parseUSD(m[4])
		if err != nil {
			t.Fatalf("catalog rate %q: %v", m[4], err)
		}
		key := m[1] + "/" + m[2]
		if catalog[key] == nil {
			catalog[key] = map[string]int64{}
		}
		catalog[key][m[3]] = micro
	}
	for p, models := range priceTable {
		for model, r := range models {
			key := catalogProvider(p) + "/" + model
			c, ok := catalog[key]
			if !ok {
				t.Errorf("%s is in the harness price table but not in pricing.py", key)
				continue
			}
			for typ, want := range map[string]int64{"INPUT": r.Input, "CACHED_INPUT": r.CachedInput, "OUTPUT": r.Output} {
				if c[typ] != want {
					t.Errorf("%s %s: harness %d micro-USD/MTok, catalog %d", key, typ, want, c[typ])
				}
			}
		}
	}
	// The default models must be priced, or the harness refuses to run at all.
	for p, model := range map[config.Provider]string{
		config.ProviderOpenAI: "gpt-4o-mini", config.ProviderAnthropic: "claude-haiku-4-5", config.ProviderGemini: "gemini-2.5-flash",
	} {
		if _, ok := lookupRate(p, model); !ok {
			t.Errorf("default model %s/%s has no rate", p, model)
		}
	}
}
