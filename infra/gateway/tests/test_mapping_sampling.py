# SPDX-License-Identifier: Apache-2.0
"""CTO-119: gateway mapping for stratified-sampling provenance.

The SDK emits ``gen_ai.sampling.stratum`` and ``gen_ai.sampling.rate``; the gateway promotes
them onto typed columns (``SamplingStratum`` / ``SamplingRate``). Pre-CTO-119 spans land in the
``unsampled`` bucket with rate 1.0 so the DQ surface can group them honestly.
"""

from __future__ import annotations

from gateway.mapping import COLUMNS, span_to_row


def _row_dict(row: tuple[object, ...]) -> dict[str, object]:
    assert len(row) == len(COLUMNS)
    return dict(zip(COLUMNS, row, strict=True))


def test_sampling_attrs_promoted_to_columns() -> None:
    span = {"gen_ai.sampling.stratum": "tail", "gen_ai.sampling.rate": 1.0}
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    assert row["SamplingStratum"] == "tail"
    assert row["SamplingRate"] == 1.0


def test_sampling_columns_default_when_absent() -> None:
    """A span without sampling attrs lands in the 'unsampled' bucket at rate 1.0."""
    row = _row_dict(span_to_row({}, tenant_id="t1", effective_ts_ns=0))
    assert row["SamplingStratum"] == "unsampled"
    assert row["SamplingRate"] == 1.0


def test_sampling_attrs_not_duplicated_in_span_attributes_map() -> None:
    span = {"gen_ai.sampling.stratum": "body", "gen_ai.sampling.rate": 0.1}
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    assert "gen_ai.sampling.stratum" not in extra
    assert "gen_ai.sampling.rate" not in extra
