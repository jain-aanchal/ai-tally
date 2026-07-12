# Price fetchers + daily scraper job (CTO-165)

Concrete per-provider price fetchers that **realize** the CTO-53 scaffold in
`tally.pricing_scraper` (which shipped with only a fake fetcher). Each fetcher satisfies the
scaffold's `PriceFetcher` Protocol and flows through the existing `PriceScraper` unchanged — this
package adds no changes to the scaffold and **does not edit `pricing.seed_catalog()`**.

## What ships

| Fetcher | Provider id | Source parsed | Fixture |
| --- | --- | --- | --- |
| `OpenAIPriceFetcher` | `openai` | OpenAI API pricing (JSON) | `tests/fixtures/pricing/openai.json` |
| `AnthropicPriceFetcher` | `anthropic` | Anthropic pricing table (HTML, stdlib `html.parser`) | `tests/fixtures/pricing/anthropic.html` |
| `GooglePriceFetcher` | `google` | Gemini API pricing (JSON) | `tests/fixtures/pricing/google.json` |

Each emits `INPUT` / `CACHED_INPUT` / `OUTPUT` rows (USD per million tokens) for the models we
price. A model with no published cached tier simply gets no `CACHED_INPUT` row.

### No network in tests

Every fetcher takes an injectable `fetch_raw: Callable[[], str] | None`. Tests inject a reader over
a recorded fixture, so **the suite never touches the network** (an autouse guard in the test files
makes any real `urlopen`/`socket` call fail loudly). When `fetch_raw` is `None`, the fetcher does a
best-effort live GET using the standard library only — no new hard HTTP dependency.

## Running the daily job

Live (best-effort network fetch of each provider source):

```
cd sdk/python
python -m tally.price_fetchers
```

Offline / deterministic (read the recorded fixtures instead of the network):

```
python -m tally.price_fetchers --fixtures tests/fixtures/pricing
```

Flags: `--version TAG` (default `scrape-<today>`), `--valid-from YYYY-MM-DD` (default today),
`--threshold N` (large-diff threshold, default 10). Exit code is `2` if any provider was skipped
(so a scheduler/CI can alert), else `0`.

The job **only proposes** — it fetches, builds a candidate, diffs it against a catalog
(default `seed_catalog()`), and prints a review artifact. It never publishes.

## Reading the review artifact

`render_review(result)` prints:

- **diff magnitude** and a `** LARGE DIFF **` banner when it exceeds the threshold (publish then
  requires `ack_large_diff=True`);
- **`** SKIPPED PROVIDERS **`** — providers whose fetcher failed this run (network/parse). Because
  `PriceScraper.build_candidate` swallows per-fetcher exceptions, this line (and a WARNING log) is
  how a silently-missing provider becomes visible. `skipped_providers(fetchers, candidate)` computes
  the set and is independently testable;
- **Added / Changed / Removed** entries, with old→new rates on changes.

> Note on **Removed**: it lists catalog keys the configured fetchers did not produce this run (e.g.
> embeddings, tool/vector rates, models no fetcher covers). Publish is **additive** and never
> deletes, so "removed" is informational — it does not drop anything from the catalog.

## Approving / publishing

Publishing is a separate, explicit human step — the job never auto-publishes:

```python
from datetime import date
from tally.pricing import seed_catalog
from tally.price_fetchers import default_fetchers
from tally.pricing_scraper import PriceScraper, Approval

catalog = seed_catalog()
scraper = PriceScraper(default_fetchers())
candidate = scraper.build_candidate(version="scrape-2026-07-12", valid_from=date.today())
# Review render_review(run_job(...)) first, then:
scraper.publish(catalog, candidate, Approval(approved=True, reviewer="you", ack_large_diff=True))
```

`publish()` is **additive**: it appends the candidate entries (newest `valid_from` wins on lookup)
and retains old versions so historical cost stays recomputable. Published fetched rates therefore
**supersede** the `[unverified]` seed markers on Gemini/Vertex/tool rates without any
`seed_catalog()` edit — e.g. the seed's `[unverified]` Gemini rates (CTO-149) resolve to the
freshly-fetched rates after the first publish, and a model absent from the catalog becomes
resolvable.
