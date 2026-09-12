// SPDX-License-Identifier: Apache-2.0
// Command verify-real-traffic sends a small, fixed matrix of REAL provider calls through an
// in-process edge proxy and checks the TraceRecord token counts the proxy recorded against the usage
// block each provider returned to the client (CTO-350).
//
// Why it exists: every usage parser in internal/proxy was verified only against fixtures built from
// published schemas. A real endpoint can emit something its documentation omits, and only real
// traffic can show that. The proxy forwards bodies byte-for-byte, so the bytes the client receives
// carry the provider's own usage block, which is an independent reference for each call. The part of
// CTO-350 no program can do, diffing the totals against each provider's usage dashboard, is left to
// the human running this, and the report prints exactly what to compare.
//
// Keys come from OPENAI_API_KEY, ANTHROPIC_API_KEY and GEMINI_API_KEY in the environment and nowhere
// else. A provider whose key is unset is skipped, and the run says so. Spend is capped by --max-usd,
// enforced before each call against a worst-case bound, so the run refuses rather than overruns.
//
// See docs/real-traffic-verification.md for the procedure.
package main

import (
	"io"
	"os"
)

func main() {
	os.Exit(run(os.Args[1:], os.Getenv, os.Stdout, os.Stderr))
}

// run is main without the process globals, so the tests drive the whole command, flags included,
// against a fake upstream.
func run(args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	opts, err := parseFlags(args, getenv, stderr)
	if err != nil {
		return exitUsage
	}
	return newHarness(opts, stdout).run()
}
