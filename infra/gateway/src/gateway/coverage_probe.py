# SPDX-License-Identifier: Apache-2.0
"""Per-layer instrumentation coverage (CTO-261, Initiative onboarding-agent §7).

WHAT THIS IS. Initiative 2 §9 gave onboarding one binary signal: has ANY span landed for this
tenant yet. That answers "are you connected" and nothing else, so a developer who wired the LLM
one-liner and stopped there sees a green tick while tools, vector, embeddings and per-customer
attribution are all still dark. This module widens that single existence check into one check per
layer, so the onboarding agent can say which layers are proven flowing and name every layer that
is not, with the reason.

THE HONESTY RULE, which is the whole point (§7). A layer is reported ``covered`` only when a real
span proves it. There is no path in :func:`build_coverage` that reaches ``covered`` from anything
but a positive span count, and a probe that could not run yields ``unknown`` rather than the
definite negative ``not_wired``. Collapsing "we could not reach ClickHouse" into "you have not
wired tools" would be inventing a negative fact out of an absence of knowledge, which is the same
sin as rendering a fabricated zero (CLAUDE.md, "honest under uncertainty").

FOUR STATES, not three, because "not covered" is really two different situations and the developer
needs to tell them apart:

  * ``covered``               a span exists for the layer. Proven, with the count that proves it.
  * ``awaiting_first_event``  the onboarding agent wired this layer, but the code path has not run
                              yet. Nothing is wrong; the developer has to exercise it.
  * ``not_wired``             no span, and nothing claims to have wired it. This is the real gap.
  * ``unknown``               the probe did not run. We say so instead of guessing.

Which layers were wired is an INPUT, never an inference: only the agent that proposed the diff
knows what it wired, and ClickHouse cannot tell a layer that was never wired from one that was
wired and never exercised. An unwired-but-firing layer still reports ``covered``, because the span
is the proof and it outranks the claim.

THE ACCOUNT LAYER IS DIFFERENT and reads a different table. See :func:`build_coverage`.

No bodies, no identifiers: this module handles operation names and row counts only. The account
signal is a count of rows whose ``AccountIdHash`` is non-empty, never a hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

CoverageState = Literal["covered", "awaiting_first_event", "not_wired", "unknown"]

# The four layers whose signal is an operation on the span itself (§7's table). The value is the
# `GenAiOperation` promoted column, which is `gen_ai.operation.name` (db/clickhouse/otel_spans.sql).
LAYER_OPERATIONS: Mapping[str, str] = {
    "llm": "chat",
    "tools": "tool",
    "vector": "vector",
    "embeddings": "embeddings",
}

# The attribution layer. Not an operation: a span of ANY operation proves it, as long as it carries
# an account hash. Kept separate from LAYER_OPERATIONS because it is read from a different table.
ACCOUNT_LAYER = "account"

# Report order. Deliberately the order a developer wires things in: the one-liner first, then the
# explicit records, then attribution, which is the layer that needs the question only they can
# answer (§6).
LAYERS: tuple[str, ...] = ("llm", "tools", "vector", "embeddings", ACCOUNT_LAYER)

_LAYER_LABELS: Mapping[str, str] = {
    "llm": "LLM calls",
    "tools": "tool calls",
    "vector": "vector search",
    "embeddings": "embeddings",
    ACCOUNT_LAYER: "per-customer attribution",
}


@dataclass(frozen=True, slots=True)
class AccountSignal:
    """What the per-account rollup says about attribution for one tenant.

    ``total_rows`` is carried alongside ``attributed_rows`` because a rollup that is empty for a
    tenant is ambiguous and must not be read as "no attribution". ``daily_account_rollup`` is a
    materialized view, an INSERT trigger that captures nothing that predates it, and a deployment
    that created it without running the documented backfill has a legitimately empty table under a
    tenant with millions of attributed spans (db/clickhouse/account_rollups.sql). Reporting
    ``not_wired`` off that would tell the developer their instrumentation is missing when it is
    ours that is. So an empty rollup for a tenant that HAS spans resolves to ``unknown``.
    """

    total_rows: int
    attributed_rows: int


@dataclass(frozen=True, slots=True)
class LayerCoverage:
    """One layer's honest state, the reason for it, and the evidence behind ``covered``."""

    layer: str
    state: CoverageState
    reason: str
    # Spans that prove the layer, or None when we could not count them. Never 0-as-a-fact under
    # `unknown`: an unknown count is null, not zero (CLAUDE.md).
    proving_spans: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "state": self.state,
            "reason": self.reason,
            "proving_spans": self.proving_spans,
        }


def normalize_wired(values: Iterable[object] | None) -> frozenset[str]:
    """Accept a caller's claim about which layers it wired, keeping only real layer names.

    An unrecognised name is dropped rather than rejected: the claim only ever softens a dark layer's
    wording from ``not_wired`` to ``awaiting_first_event``, so a typo costs the developer a slightly
    blunter message and nothing else. It can never manufacture coverage.
    """
    if values is None:
        return frozenset()
    return frozenset(v for v in values if isinstance(v, str) and v in LAYERS)


def parse_wired_param(raw: str | None) -> frozenset[str]:
    """Parse the ``wired=tools,vector`` query parameter."""
    if not raw:
        return frozenset()
    return normalize_wired(part.strip() for part in raw.split(","))


def _dark(layer: str, wired: frozenset[str], detail: str) -> LayerCoverage:
    """The two flavours of "no span proves this layer", which are not the same news."""
    if layer in wired:
        return LayerCoverage(
            layer=layer,
            state="awaiting_first_event",
            reason=(
                f"wired, awaiting first event: {detail}. Exercise the code path that "
                f"records {_LAYER_LABELS[layer]} and this flips on its own."
            ),
            proving_spans=0,
        )
    return LayerCoverage(
        layer=layer,
        state="not_wired",
        reason=f"not wired: {detail}, and no instrumentation for this layer was reported.",
        proving_spans=0,
    )


def build_coverage(
    operation_counts: Mapping[str, int] | None,
    account: AccountSignal | None,
    wired: frozenset[str] = frozenset(),
) -> list[LayerCoverage]:
    """Turn two raw probe results into the per-layer report.

    ``operation_counts`` maps ``GenAiOperation`` to a span count for the tenant, or is None when the
    span probe could not run. ``account`` is the rollup signal, or None when that probe could not
    run. The two are separate inputs because they read different tables and can fail independently:
    an unreadable rollup must not blank out the four layers the span probe answered fine.

    WHY THE ACCOUNT LAYER READS THE ROLLUP. On ``otel_spans`` the ORDER BY is
    (TenantId, FeatureTag, ServiceName, SpanName, Timestamp), so ``AccountIdHash`` is not in the key
    at all and "does any attributed span exist" degrades into a scan of the tenant's whole history
    every time the answer is no, which is exactly the case onboarding polls. ``daily_account_rollup``
    is ordered (TenantId, AccountIdHash, Day, ...), so the same question is a key-prefix range read
    over a table that holds one row per account per day per feature per operation instead of one row
    per span. That is the one genuinely cheaper rollup read here; the four operation layers get no
    such benefit (GenAiOperation is the LAST key column there) and are answered in a single grouped
    pass over the spans, which is also what supplies the proving counts.
    """
    out: list[LayerCoverage] = []

    for layer in LAYER_OPERATIONS:
        operation = LAYER_OPERATIONS[layer]
        if operation_counts is None:
            out.append(
                LayerCoverage(
                    layer=layer,
                    state="unknown",
                    reason=(
                        "could not read otel_spans, so we cannot tell whether this layer is "
                        "flowing. This is not a report that it is dark."
                    ),
                    proving_spans=None,
                )
            )
            continue
        count = int(operation_counts.get(operation, 0))
        if count > 0:
            out.append(
                LayerCoverage(
                    layer=layer,
                    state="covered",
                    reason=(
                        f"{count} span(s) with GenAiOperation = '{operation}' prove this layer "
                        "is flowing."
                    ),
                    proving_spans=count,
                )
            )
        else:
            out.append(_dark(layer, wired, f"no span with GenAiOperation = '{operation}'"))

    out.append(_account_coverage(operation_counts, account, wired))
    return out


def _account_coverage(
    operation_counts: Mapping[str, int] | None,
    account: AccountSignal | None,
    wired: frozenset[str],
) -> LayerCoverage:
    if account is None:
        return LayerCoverage(
            layer=ACCOUNT_LAYER,
            state="unknown",
            reason=(
                "could not read daily_account_rollup, so we cannot tell whether spans carry an "
                "account. This is not a report that attribution is missing."
            ),
            proving_spans=None,
        )
    if account.attributed_rows > 0:
        return LayerCoverage(
            layer=ACCOUNT_LAYER,
            state="covered",
            reason=(
                f"{account.attributed_rows} rollup row(s) carry a non-empty AccountIdHash, so "
                "spend is attributed to a customer."
            ),
            proving_spans=account.attributed_rows,
        )
    spans_seen = operation_counts is not None and any(v > 0 for v in operation_counts.values())
    if account.total_rows == 0 and spans_seen:
        # Spans exist but the rollup has nothing for this tenant. That is a rollup that was never
        # backfilled, not an app that never called with_account(), and we must not blame the
        # developer for our own missing migration step (see AccountSignal).
        return LayerCoverage(
            layer=ACCOUNT_LAYER,
            state="unknown",
            reason=(
                "spans exist for this tenant but daily_account_rollup has no rows for it, so the "
                "rollup is behind (see the backfill note in db/clickhouse/account_rollups.sql) "
                "and cannot answer this."
            ),
            proving_spans=None,
        )
    return _dark(ACCOUNT_LAYER, wired, "no span carries an account hash")
