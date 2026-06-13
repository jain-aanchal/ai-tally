# Contributing to ai-tally

Thanks for your interest in contributing. ai-tally is cost-and-value observability
for AI products, and we welcome issues, discussion, and pull requests.

## Ways to contribute

- **Report bugs** — open an issue with steps to reproduce, expected vs. actual behavior,
  and your environment (OS, Python/Node versions, Docker version).
- **Propose features** — open an issue describing the problem first, before the solution.
- **Improve docs** — fixes to `README.md`, [`RUNNING.md`](./RUNNING.md), or inline docs are
  always welcome and a great first contribution.
- **Send patches** — see the workflow below.

## Development setup

The full end-to-end runbook lives in **[RUNNING.md](./RUNNING.md)**. Quick start:

```bash
cd infra && make up && make seed && make demo   # local stack + tenant + sample telemetry
cd web && npm install && npm run dev            # dashboard at http://localhost:3000
```

### Python SDK / gateway

Uses [uv](https://docs.astral.sh/uv/), `ruff`, and `pytest`:

```bash
cd sdk/python
uv sync
uv run ruff check .
uv run pytest
```

### Web dashboard

```bash
cd web
npm install
npm run typecheck     # tsc
npm run lint          # next lint
npx vitest run        # unit tests
```

## Pull request workflow

1. Fork the repo and create a topic branch from `main`
   (e.g. `feat/connector-anthropic`, `fix/clock-skew-clamp`).
2. Make your change with tests. Keep PRs focused — one logical change per PR.
3. Run the relevant checks above; CI must be green.
4. Open a PR with a clear description: what changed, why, and how you verified it.
5. A maintainer will review. Please be responsive to feedback.

### Commit and PR conventions

- Use clear, imperative commit subjects (e.g. `fix: clamp negative skew to zero`).
- Reference related issues in the PR description (`Closes #123`).
- Don't commit secrets, `.env` files, or generated artifacts. `.gitignore` covers the
  common cases; double-check before staging.

## Code of conduct

This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md). By participating,
you agree to uphold it.

## Developer Certificate of Origin (DCO)

ai-tally uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a Contributor License Agreement (CLA). The DCO is a lightweight,
per-commit attestation that you have the right to submit the contribution under
the project's license.

To sign off a commit, add the `-s` (or `--signoff`) flag:

```bash
git commit -s -m "fix: clamp negative skew to zero"
```

This appends a trailer to the commit message like:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding this line you certify the statements in the
[DCO](https://developercertificate.org/) — paraphrased: that you wrote the
patch (or have the right to submit it) and that you're licensing it under the
project's open-source license.

Configure git to use your real name and a valid email:

```bash
git config user.name "Jane Doe"
git config user.email "jane@example.com"
```

Every commit in a pull request must be signed off. If you forget, you can fix
the last commit with `git commit --amend -s --no-edit` (and force-push to your
branch) or, for several commits, `git rebase --signoff main`.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](./LICENSE), the same license that covers the project. The
DCO sign-off above is your affirmation that you have the right to submit the
contribution under that license.
