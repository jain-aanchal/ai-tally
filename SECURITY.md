# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in ai-tally, please report it privately.
**Do not open a public issue for security problems.**

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Report a vulnerability" under the repository's **Security** tab), or
- Email the maintainer at **jain.aanchal@gmail.com** with the details.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof of concept.
- Affected component(s) and version/commit.

We aim to acknowledge reports within 5 business days and will keep you informed as
we work on a fix. We ask that you give us a reasonable opportunity to remediate before
any public disclosure.

## Scope and handling

ai-tally handles potentially sensitive telemetry. A few security invariants the project
holds itself to — issues that violate any of these are in scope:

- **Secrets are never persisted in plaintext.** Customer/provider API keys are held in
  memory only and never logged. Stored credentials are KMS references; API keys are stored
  as SHA-256 hashes only.
- **Payloads are redacted/PII-rejected at ingest** per the configured per-tenant policy.
- **Tenant isolation** — no query path should let one tenant read another's data.

## Supported versions

ai-tally is in active early development; security fixes are applied to `main`. Until a
stable release line exists, please test against the latest `main`.
