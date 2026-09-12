// SPDX-License-Identifier: Apache-2.0
package proxy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
)

// TestCapturedRealTrafficFixtures turns every fixture captured from real provider traffic into a
// regression test (CTO-350).
//
// cmd/verify-real-traffic --capture writes testdata/<provider>/real_<call>.{json,sse} with content
// stripped, plus a real_<call>.expected.json sidecar holding the counts derived from the provider's
// own usage block. Unlike the fixtures transcribed from published schemas, these record what a live
// endpoint actually sent, so a parser change that breaks on a real payload fails here rather than in
// a variance report. It skips until a human has run the capture and committed the output.
func TestCapturedRealTrafficFixtures(t *testing.T) {
	sidecars, err := filepath.Glob(filepath.Join("testdata", "*", "real_*.expected.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(sidecars) == 0 {
		t.Skip("no captured real-traffic fixtures yet; see docs/real-traffic-verification.md (CTO-350)")
	}
	for _, sc := range sidecars {
		t.Run(filepath.ToSlash(sc), func(t *testing.T) {
			raw, err := os.ReadFile(sc)
			if err != nil {
				t.Fatal(err)
			}
			var want struct {
				Fixture           string `json:"fixture"`
				RequestPath       string `json:"request_path"`
				Model             string `json:"model"`
				PromptTokens      *int64 `json:"prompt_tokens"`
				CompletionTokens  *int64 `json:"completion_tokens"`
				CachedInputTokens *int64 `json:"cached_input_tokens"`
			}
			if err := json.Unmarshal(raw, &want); err != nil {
				t.Fatalf("sidecar: %v", err)
			}
			body, err := os.ReadFile(filepath.Join(filepath.Dir(sc), want.Fixture))
			if err != nil {
				t.Fatalf("fixture named by sidecar: %v", err)
			}
			provider := config.Provider(filepath.Base(filepath.Dir(sc)))
			meta := extractMeta(provider, want.RequestPath, body)
			assertTokens(t, "PromptTokens", meta.PromptTokens, want.PromptTokens)
			assertTokens(t, "CompletionTokens", meta.CompletionTokens, want.CompletionTokens)
			assertTokens(t, "CachedInputTokens", meta.CachedInputTokens, want.CachedInputTokens)
			if meta.Model != want.Model {
				t.Errorf("Model = %q, want %q", meta.Model, want.Model)
			}
		})
	}
}
