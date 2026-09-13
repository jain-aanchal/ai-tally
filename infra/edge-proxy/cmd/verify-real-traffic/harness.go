// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

// Exit codes. A mismatch outranks a refusal: a wrong number is the finding CTO-350 is looking for,
// and a refused call only means the run was incomplete.
const (
	exitOK       = 0
	exitMismatch = 1
	exitUsage    = 2
	exitRefused  = 3
)

var providerOrder = []config.Provider{config.ProviderOpenAI, config.ProviderAnthropic, config.ProviderGemini}

var keyEnv = map[config.Provider]string{
	config.ProviderOpenAI:    "OPENAI_API_KEY",
	config.ProviderAnthropic: "ANTHROPIC_API_KEY",
	config.ProviderGemini:    "GEMINI_API_KEY",
}

type options struct {
	MaxMicro   int64
	Capture    bool
	CaptureDir string
	DryRun     bool
	Timeout    time.Duration
	Only       map[config.Provider]bool
	Models     map[config.Provider]string
	Upstreams  map[config.Provider]string
	// Keys are read from the environment only. There is deliberately no flag for them: a key on a
	// command line lands in shell history and process listings.
	Keys map[config.Provider]string
}

func parseFlags(args []string, getenv func(string) string, stderr io.Writer) (options, error) {
	fs := flag.NewFlagSet("verify-real-traffic", flag.ContinueOnError)
	fs.SetOutput(stderr)
	maxUSD := fs.String("max-usd", "0.50", "hard spend cap in USD, enforced before every call against a worst-case bound")
	capture := fs.Bool("capture", false, "write content-stripped usage fixtures and expectation sidecars into --capture-dir")
	captureDir := fs.String("capture-dir", "internal/proxy/testdata", "fixture root for --capture (run from infra/edge-proxy)")
	dryRun := fs.Bool("dry-run", false, "print the call plan and worst-case spend, send nothing")
	only := fs.String("only", "", "comma list of providers to run (openai,anthropic,gemini); default all with a key")
	timeout := fs.Duration("timeout", 2*time.Minute, "per-call timeout")
	models := map[config.Provider]*string{
		config.ProviderOpenAI:    fs.String("openai-model", "gpt-4o-mini", "OpenAI model (must be in the price table)"),
		config.ProviderAnthropic: fs.String("anthropic-model", "claude-haiku-4-5", "Anthropic model (must be in the price table)"),
		config.ProviderGemini:    fs.String("gemini-model", "gemini-2.5-flash", "Gemini thinking-capable model (must be in the price table)"),
	}
	upstreams := map[config.Provider]*string{
		config.ProviderOpenAI:    fs.String("openai-upstream", "https://api.openai.com", "OpenAI origin (override only for testing)"),
		config.ProviderAnthropic: fs.String("anthropic-upstream", "https://api.anthropic.com", "Anthropic origin (override only for testing)"),
		config.ProviderGemini:    fs.String("gemini-upstream", "https://generativelanguage.googleapis.com", "Gemini origin (override only for testing)"),
	}
	if err := fs.Parse(args); err != nil {
		return options{}, err
	}
	capMicro, err := parseUSD(*maxUSD)
	if err != nil {
		fmt.Fprintf(stderr, "verify-real-traffic: --max-usd: %v\n", err)
		return options{}, err
	}
	o := options{
		MaxMicro: capMicro, Capture: *capture, CaptureDir: *captureDir, DryRun: *dryRun, Timeout: *timeout,
		Models: map[config.Provider]string{}, Upstreams: map[config.Provider]string{}, Keys: map[config.Provider]string{},
	}
	for _, p := range providerOrder {
		o.Models[p] = *models[p]
		o.Upstreams[p] = strings.TrimRight(*upstreams[p], "/")
		o.Keys[p] = strings.TrimSpace(getenv(keyEnv[p]))
	}
	if strings.TrimSpace(*only) != "" {
		o.Only = map[config.Provider]bool{}
		for _, name := range strings.Split(*only, ",") {
			p := config.Provider(strings.TrimSpace(name))
			if _, known := keyEnv[p]; !known {
				err := fmt.Errorf("unknown provider %q in --only", name)
				fmt.Fprintf(stderr, "verify-real-traffic: %v\n", err)
				return options{}, err
			}
			o.Only[p] = true
		}
	}
	return o, nil
}

// callResult is one executed call. body is the raw response, kept in memory only for the capture
// step and never printed; nothing but stripped metadata ever leaves the process.
type callResult struct {
	Spec       callSpec
	Status     int
	Err        error
	Usage      providerUsage
	Want       counts
	Got        counts
	GotModel   string
	RequestID  string
	Mismatches []string
	Notes      []string
	Gap        string
	SpentMicro int64

	stream bool
	body   []byte
}

// wantModel is the model the TraceRecord should carry: the request path's model for Gemini (the path
// is authoritative there) and the provider-reported model otherwise.
func (r *callResult) wantModel() string {
	if r.Spec.Provider == config.ProviderGemini {
		return r.Spec.Model
	}
	return r.Usage.Model
}

func (r *callResult) failed() bool {
	return r.Err != nil || r.Status < 200 || r.Status > 299 || len(r.Mismatches) > 0
}

type harness struct {
	opts   options
	out    io.Writer
	client *http.Client
	now    func() time.Time
}

func newHarness(o options, out io.Writer) *harness {
	return &harness{opts: o, out: out, client: &http.Client{Timeout: o.Timeout}, now: time.Now}
}

// chanSink hands each TraceRecord to the waiting call. Calls run one at a time, so records arrive in
// call order and need no correlation id.
type chanSink struct{ ch chan proxy.TraceRecord }

func (s chanSink) Record(r proxy.TraceRecord) {
	select {
	case s.ch <- r:
	default:
		// Never block the proxy's request path. A dropped record shows up as "no TraceRecord" on the
		// call, which is a failure, so nothing is hidden.
	}
}

// startProxy runs the real proxy handler in-process, configured through config.FromEnv exactly as the
// binary is, with path-mode routes to each provider. In-process rather than a spawned binary so the
// harness can read TraceRecords directly instead of standing up a gateway to receive them.
func startProxy(upstreams map[config.Provider]string, sink proxy.Sink) (string, func(), error) {
	var routes []string
	for _, p := range providerOrder {
		routes = append(routes, fmt.Sprintf("/%s=%s:%s", p, upstreams[p], p))
	}
	env := map[string]string{
		"EDGE_PROXY_ROUTE_MODE":     "path",
		"EDGE_PROXY_ROUTES":         strings.Join(routes, ","),
		"EDGE_PROXY_REQUIRE_TENANT": "false",
	}
	cfg, err := config.FromEnv(func(k string) string { return env[k] })
	if err != nil {
		return "", nil, fmt.Errorf("proxy config: %w", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	srv := &http.Server{Handler: proxy.New(cfg, proxy.WithSink(sink)), ReadHeaderTimeout: 15 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	return "http://" + ln.Addr().String(), func() { _ = srv.Close() }, nil
}

func (h *harness) run() int {
	o := h.opts
	enabled := map[config.Provider]bool{}
	for _, p := range providerOrder {
		switch {
		case o.Only != nil && !o.Only[p]:
			fmt.Fprintf(h.out, "SKIP %-9s not in --only\n", p)
		case o.Keys[p] == "" && !o.DryRun:
			fmt.Fprintf(h.out, "SKIP %-9s %s is not set\n", p, keyEnv[p])
		default:
			enabled[p] = true
		}
	}
	specs := buildMatrix(o.Models, enabled)
	if len(specs) == 0 {
		fmt.Fprintln(h.out, "Nothing to verify: no provider has a key set. Export at least one of "+
			"OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY.")
		return exitUsage
	}
	for _, s := range specs {
		if _, ok := lookupRate(s.Provider, s.Model); !ok {
			fmt.Fprintf(h.out, "REFUSED: %s model %q has no rate in the price table, so its spend cannot be "+
				"bounded. Pick a listed model or add the catalog row to price.go.\n", s.Provider, s.Model)
			return exitUsage
		}
	}
	if o.Capture && !o.DryRun {
		if st, err := os.Stat(o.CaptureDir); err != nil || !st.IsDir() {
			fmt.Fprintf(h.out, "--capture-dir %q is not a directory (run from infra/edge-proxy)\n", o.CaptureDir)
			return exitUsage
		}
	}

	if o.DryRun {
		return h.plan(specs)
	}

	sink := chanSink{ch: make(chan proxy.TraceRecord, 64)}
	base, stop, err := startProxy(o.Upstreams, sink)
	if err != nil {
		fmt.Fprintf(h.out, "could not start proxy: %v\n", err)
		return exitUsage
	}
	defer stop()

	started := h.now().UTC()
	fmt.Fprintf(h.out, "Proxy at %s. Spend cap %s. %d calls planned.\n\n", base, formatUSD(o.MaxMicro), len(specs))

	var results []*callResult
	var spent int64
	refused := false
	for _, s := range specs {
		r := lookupRateMust(s)
		est := worstCaseMicro(s, r)
		if spent+est > o.MaxMicro {
			fmt.Fprintf(h.out, "REFUSED %s/%s: worst case %s would take spend to %s, over the %s cap. "+
				"No further calls sent.\n", s.Provider, s.Name, formatUSD(est), formatUSD(spent+est), formatUSD(o.MaxMicro))
			refused = true
			break
		}
		res := h.do(base, s, sink)
		res.SpentMicro = spentMicro(s, res.Want, r)
		spent += res.SpentMicro
		results = append(results, res)
	}
	ended := h.now().UTC()

	h.report(results, started, ended, spent)

	failed := false
	for _, r := range results {
		if r.failed() {
			failed = true
		}
	}
	if o.Capture {
		h.capture(results)
	}
	switch {
	case failed:
		fmt.Fprintln(h.out, "\nRESULT: FAIL (a mismatch or provider error above). Exit 1.")
		return exitMismatch
	case refused:
		fmt.Fprintln(h.out, "\nRESULT: INCOMPLETE (spend cap refused a call). Raise --max-usd or use --only. Exit 3.")
		return exitRefused
	}
	fmt.Fprintln(h.out, "\nRESULT: every recorded count matches the provider's usage block. Now diff the totals "+
		"above against each provider's usage dashboard.")
	return exitOK
}

func lookupRateMust(s callSpec) rate {
	r, _ := lookupRate(s.Provider, s.Model)
	return r
}

func (h *harness) plan(specs []callSpec) int {
	fmt.Fprintf(h.out, "DRY RUN: nothing is sent. Spend cap %s.\n\n", formatUSD(h.opts.MaxMicro))
	tw := tabwriter.NewWriter(h.out, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "PROVIDER\tCALL\tMODE\tMODEL\tWORST CASE\tCUMULATIVE\tWITHIN CAP")
	var total int64
	for _, s := range specs {
		est := worstCaseMicro(s, lookupRateMust(s))
		total += est
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\t%s\t%v\n", s.Provider, s.Name, s.mode(), s.Model,
			formatUSD(est), formatUSD(total), total <= h.opts.MaxMicro)
	}
	_ = tw.Flush()
	fmt.Fprintf(h.out, "\nWorst-case total %s (prompt bounded by request bytes, output by max tokens).\n", formatUSD(total))
	return exitOK
}

func (h *harness) do(base string, s callSpec, sink chanSink) *callResult {
	res := &callResult{Spec: s, stream: s.Stream}
	req, err := http.NewRequest(http.MethodPost, base+"/"+string(s.Provider)+s.Path, bytes.NewReader(s.Body))
	if err != nil {
		res.Err = err
		return res
	}
	setAuth(req, s.Provider, h.opts.Keys[s.Provider])
	resp, err := h.client.Do(req)
	if err != nil {
		res.Err = err
	} else {
		// The body is read whole because the provider's usage block is in it. It stays in memory for
		// this call and the optional stripped capture, and is never logged.
		res.body, err = io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err != nil {
			res.Err = err
		}
		res.Status = resp.StatusCode
		res.stream = strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/event-stream") ||
			isSSEBody(res.body)
		if hdr := requestIDHeader(s.Provider); hdr != "" {
			res.RequestID = resp.Header.Get(hdr)
		}
	}

	res.Usage = parseProviderUsage(s.Provider, res.body, res.stream)
	if res.RequestID == "" {
		res.RequestID = res.Usage.ID
	}
	res.Want, res.Notes = res.Usage.expected(s.Provider)

	select {
	case rec := <-sink.ch:
		res.Got = counts{Prompt: rec.PromptTokens, Completion: rec.CompletionTokens, Cached: rec.CachedInputTokens}
		res.GotModel = rec.Model
		res.Mismatches = compareCounts(res.Want, res.Got)
		if res.Status >= 200 && res.Status <= 299 && rec.Model != res.wantModel() {
			res.Mismatches = append(res.Mismatches, fmt.Sprintf("model provider=%q proxy=%q", res.wantModel(), rec.Model))
		}
	case <-time.After(10 * time.Second):
		res.Mismatches = append(res.Mismatches, "proxy emitted no TraceRecord")
	}

	if res.Err == nil && res.Status >= 200 && res.Status <= 299 {
		switch s.Cover {
		case coverCacheRead:
			if res.Want.Cached == nil || *res.Want.Cached == 0 {
				res.Gap = "cache was not read, so the cached-token path was NOT exercised"
			}
		case coverThoughts:
			if t := res.Usage.thoughts(s.Provider); t == nil || *t == 0 {
				res.Gap = "model reported no thinking tokens, so the thoughts path was NOT exercised"
			}
		}
	}
	return res
}

func (r *callResult) resultCell() string {
	switch {
	case r.Err != nil:
		return "ERROR " + transportError(r.Err)
	case r.Status < 200 || r.Status > 299:
		cell := fmt.Sprintf("ERROR http %d", r.Status)
		if e := errorSummary(r.body); e != "" {
			cell += " (" + e + ")"
		}
		return cell
	case len(r.Mismatches) > 0:
		return "MISMATCH " + strings.Join(r.Mismatches, "; ")
	}
	return "MATCH"
}

// transportError reports the failure class without the error string, which carries the request URL.
func transportError(err error) string {
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return "timeout"
	}
	return "transport failure"
}

func (h *harness) report(results []*callResult, started, ended time.Time, spent int64) {
	tw := tabwriter.NewWriter(h.out, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "PROVIDER\tCALL\tMODE\tPROVIDER prompt/compl/cached/thoughts\tPROXY prompt/compl/cached\tRESULT")
	for _, r := range results {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s/%s/%s/%s\t%s/%s/%s\t%s\n",
			r.Spec.Provider, r.Spec.Name, r.Spec.mode(),
			fmtCount(r.Want.Prompt), fmtCount(r.Want.Completion), fmtCount(r.Want.Cached), fmtCount(r.Usage.thoughts(r.Spec.Provider)),
			fmtCount(r.Got.Prompt), fmtCount(r.Got.Completion), fmtCount(r.Got.Cached),
			r.resultCell())
	}
	_ = tw.Flush()
	fmt.Fprintln(h.out, "\nProvider columns are derived from the provider's own usage block per its documented "+
		"semantics (Anthropic prompt = input + cache writes + cache reads; Gemini completion = total - prompt).")

	var gaps, notes []string
	for _, r := range results {
		if r.Gap != "" {
			gaps = append(gaps, fmt.Sprintf("  %s/%s: %s", r.Spec.Provider, r.Spec.Name, r.Gap))
		}
		for _, n := range r.Notes {
			notes = append(notes, fmt.Sprintf("  %s/%s: %s", r.Spec.Provider, r.Spec.Name, n))
		}
	}
	if len(gaps) > 0 {
		fmt.Fprintln(h.out, "\nCOVERAGE GAPS (not failures, but the path below was not verified; rerun, caches are timing dependent):")
		fmt.Fprintln(h.out, strings.Join(gaps, "\n"))
	}
	if len(notes) > 0 {
		fmt.Fprintln(h.out, "\nNOTES:")
		fmt.Fprintln(h.out, strings.Join(notes, "\n"))
	}

	fmt.Fprintf(h.out, "\nCHECK AGAINST PROVIDER DASHBOARDS (window %s to %s UTC)\n",
		started.Format(time.RFC3339), ended.Add(time.Second).Format(time.RFC3339))
	byProvider := map[config.Provider][]*callResult{}
	for _, r := range results {
		byProvider[r.Spec.Provider] = append(byProvider[r.Spec.Provider], r)
	}
	for _, p := range providerOrder {
		rs := byProvider[p]
		if len(rs) == 0 {
			continue
		}
		fmt.Fprintf(h.out, "\n%s (%d calls, model %s)\n", p, len(rs), rs[0].Spec.Model)
		for _, line := range providerTotals(p, rs) {
			fmt.Fprintln(h.out, "  "+line)
		}
		for _, r := range rs {
			id := r.RequestID
			if id == "" {
				id = "(none returned)"
			}
			fmt.Fprintf(h.out, "  %-18s %-7s id=%s finish=%s\n    raw: %s\n", r.Spec.Name, r.Spec.mode(), id,
				strings.Join(r.Usage.FinishReasons, ","), r.Usage.raw(p))
		}
	}
	fmt.Fprintf(h.out, "\nCap accounting: %s of %s. This is an UPPER BOUND at seed-catalog rates with no cache "+
		"discount, not a bill; the provider dashboard is the bill.\n", formatUSD(spent), formatUSD(h.opts.MaxMicro))
}

// providerTotals sums each raw usage field across a provider's calls, in the provider's own field
// names. A field some call did not report is marked, rather than silently summed as if it were 0.
func providerTotals(p config.Provider, rs []*callResult) []string {
	type field struct {
		name string
		get  func(providerUsage) *int64
	}
	var fields []field
	switch p {
	case config.ProviderOpenAI:
		fields = []field{
			{"prompt_tokens", func(u providerUsage) *int64 { return u.PromptTokens }},
			{"completion_tokens", func(u providerUsage) *int64 { return u.CompletionTokens }},
			{"cached_tokens", func(u providerUsage) *int64 { return u.CachedTokens }},
		}
	case config.ProviderAnthropic:
		fields = []field{
			{"input_tokens", func(u providerUsage) *int64 { return u.InputTokens }},
			{"cache_creation_input_tokens", func(u providerUsage) *int64 { return u.CacheCreation }},
			{"cache_read_input_tokens", func(u providerUsage) *int64 { return u.CacheRead }},
			{"output_tokens", func(u providerUsage) *int64 { return u.OutputTokens }},
		}
	case config.ProviderGemini:
		fields = []field{
			{"promptTokenCount", func(u providerUsage) *int64 { return u.PromptTokenCount }},
			{"candidatesTokenCount", func(u providerUsage) *int64 { return u.CandidatesTokenCount }},
			{"thoughtsTokenCount", func(u providerUsage) *int64 { return u.ThoughtsTokenCount }},
			{"cachedContentTokenCount", func(u providerUsage) *int64 { return u.CachedContentTokenCount }},
		}
	}
	var proxyPrompt, proxyCompletion int64
	for _, r := range rs {
		if r.Got.Prompt != nil {
			proxyPrompt += *r.Got.Prompt
		}
		if r.Got.Completion != nil {
			proxyCompletion += *r.Got.Completion
		}
	}
	var parts []string
	for _, f := range fields {
		var sum int64
		missing := 0
		for _, r := range rs {
			if v := f.get(r.Usage); v != nil {
				sum += *v
			} else {
				missing++
			}
		}
		s := fmt.Sprintf("%s=%d", f.name, sum)
		if missing > 0 {
			s += fmt.Sprintf(" (%d unreported)", missing)
		}
		parts = append(parts, s)
	}
	return []string{
		"provider totals: " + strings.Join(parts, " "),
		fmt.Sprintf("proxy totals:    prompt=%d completion=%d", proxyPrompt, proxyCompletion),
	}
}

func (h *harness) capture(results []*callResult) {
	fmt.Fprintf(h.out, "\nCAPTURE into %s:\n", h.opts.CaptureDir)
	for _, r := range results {
		if r.Err != nil || r.Status < 200 || r.Status > 299 || len(r.body) == 0 {
			fmt.Fprintf(h.out, "  skip %s/%s: no successful response to capture\n", r.Spec.Provider, r.Spec.Name)
			continue
		}
		path, err := writeCapture(h.opts.CaptureDir, r, h.now())
		if err != nil {
			fmt.Fprintf(h.out, "  FAILED %s/%s: %v\n", r.Spec.Provider, r.Spec.Name, err)
			continue
		}
		warn := ""
		if len(r.Mismatches) > 0 {
			warn = "  (MISMATCH: TestCapturedRealTrafficFixtures will fail until the parser is fixed)"
		}
		fmt.Fprintf(h.out, "  wrote %s%s\n", path, warn)
	}
}
