# SPDX-License-Identifier: Apache-2.0
"""CSV revenue upload parsing and row shaping (CTO-198).

Pure-logic coverage: every test here runs without Postgres or ClickHouse. The two properties that
matter are that a malformed row is rejected with a LINE NUMBER rather than silently skipped, and
that the ids a file produces are derived, so a second upload of the same period replaces rather
than accumulates.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.revenue_upload import (
    MAX_ACCOUNT_ID_LEN,
    UPLOAD_EVENT_NAME,
    UPLOAD_SOURCE,
    ParsedUpload,
    RevenueUploadError,
    build_period_snapshots,
    business_event_id,
    normalize_period,
    parse_revenue_csv,
    period_id_prefix,
    period_occurred_at,
)

UTC = timezone.utc

GOOD = (
    "account_id,period,amount,currency\n"
    "acct_1001,2026-08,12500.00,USD\n"
    "acct_1002,2026-08,4200.50,USD\n"
)


def _fake_hash(account_id: str) -> str:
    """Stand-in for the per-tenant HMAC. Deterministic and 64 chars, like the real digest."""
    return (account_id * 64)[:64]


class TestHappyPath:
    def test_parses_rows_and_amounts(self) -> None:
        parsed = parse_revenue_csv(GOOD)
        assert [r.account_id for r in parsed.rows] == ["acct_1001", "acct_1002"]
        assert [r.amount_micro for r in parsed.rows] == [12_500_000_000, 4_200_500_000]
        assert parsed.periods == ("2026-08",)

    def test_accepts_spreadsheet_shaped_money(self) -> None:
        # A finance export ships thousands separators, currency symbols and accounting negatives.
        # Rejecting those would send the operator back to their spreadsheet for no good reason.
        text = (
            "account_id,period,amount,currency\n"
            'acct_a,2026-08,"1,234.56",USD\n'
            "acct_b,2026-08,$99,USD\n"
            "acct_c,2026-08,(50.00),USD\n"
        )
        parsed = parse_revenue_csv(text)
        assert [r.amount_micro for r in parsed.rows] == [1_234_560_000, 99_000_000, -50_000_000]

    def test_header_order_and_extra_columns_are_tolerated(self) -> None:
        text = (
            "Currency,Notes,Period,Amount,Account_ID\n"
            "usd,renewal,2026-08,100,acct_z\n"
        )
        parsed = parse_revenue_csv(text)
        assert parsed.rows[0].account_id == "acct_z"
        assert parsed.rows[0].currency == "USD"

    def test_utf8_bom_header_is_accepted(self) -> None:
        # Excel writes a BOM onto the first cell. Without stripping it every export looks headerless.
        parsed = parse_revenue_csv("﻿" + GOOD)
        assert len(parsed.rows) == 2

    def test_blank_lines_are_not_errors(self) -> None:
        parsed = parse_revenue_csv(GOOD + "\n\n")
        assert len(parsed.rows) == 2

    def test_day_precision_period_narrows_to_its_month(self) -> None:
        assert normalize_period("2026-08-01") == "2026-08"
        assert normalize_period(" 2026-08 ") == "2026-08"
        assert normalize_period("2026-13") is None
        assert normalize_period("Aug 2026") is None

    def test_multiple_periods_in_one_file(self) -> None:
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-07,10,USD\n"
            "acct_a,2026-08,20,USD\n"
        )
        parsed = parse_revenue_csv(text)
        assert parsed.periods == ("2026-07", "2026-08")
        assert len(parsed.rows_for("2026-07")) == 1


class TestRejectionsCarryLineNumbers:
    """A malformed row is rejected with the line number, never silently skipped."""

    def _errors(self, text: str) -> list[tuple[int, str]]:
        with pytest.raises(RevenueUploadError) as exc:
            parse_revenue_csv(text)
        return [(e.line, e.message) for e in exc.value.errors]

    def test_bad_amount_names_its_line(self) -> None:
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-08,100,USD\n"
            "acct_b,2026-08,n/a,USD\n"
        )
        errors = self._errors(text)
        assert [line for line, _ in errors] == [3]
        assert "amount" in errors[0][1]

    def test_bad_period_names_its_line(self) -> None:
        errors = self._errors("account_id,period,amount,currency\nacct_a,August,100,USD\n")
        assert errors[0][0] == 2
        assert "YYYY-MM" in errors[0][1]

    def test_bad_currency_names_its_line(self) -> None:
        errors = self._errors("account_id,period,amount,currency\nacct_a,2026-08,100,dollars\n")
        assert errors[0][0] == 2
        assert "ISO 4217" in errors[0][1]

    def test_empty_account_id_names_its_line(self) -> None:
        errors = self._errors("account_id,period,amount,currency\n,2026-08,100,USD\n")
        assert errors == [(2, "account_id is empty")]

    def test_overlong_account_id_names_its_line(self) -> None:
        long_id = "a" * (MAX_ACCOUNT_ID_LEN + 1)
        errors = self._errors(f"account_id,period,amount,currency\n{long_id},2026-08,100,USD\n")
        assert errors[0][0] == 2

    def test_short_row_names_its_line(self) -> None:
        errors = self._errors("account_id,period,amount,currency\nacct_a,2026-08\n")
        assert errors[0][0] == 2
        assert "columns" in errors[0][1]

    def test_every_bad_line_is_reported_at_once(self) -> None:
        # One error per attempt would mean N round trips for an export with N typos.
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-08,oops,USD\n"
            "acct_b,nope,100,USD\n"
            "acct_c,2026-08,100,US\n"
        )
        assert [line for line, _ in self._errors(text)] == [2, 3, 4]

    def test_duplicate_account_in_one_period_is_rejected_with_both_lines(self) -> None:
        # Two rows for one account and period would collapse onto one derived id and silently lose
        # a figure, so the file is refused rather than resolved by last-write-wins.
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-08,100,USD\n"
            "acct_a,2026-08,250,USD\n"
        )
        errors = self._errors(text)
        assert errors[0][0] == 3
        assert "line 2" in errors[0][1]

    def test_mixed_currency_in_one_period_is_rejected(self) -> None:
        # Summing currencies needs an FX rate we do not have. Guessing one fabricates the number.
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-08,100,USD\n"
            "acct_b,2026-08,100,EUR\n"
        )
        errors = self._errors(text)
        assert errors[0][0] == 3
        assert "FX rate" in errors[0][1]

    def test_missing_header_column_is_fatal(self) -> None:
        with pytest.raises(RevenueUploadError) as exc:
            parse_revenue_csv("account_id,period,amount\nacct_a,2026-08,100\n")
        assert "currency" in str(exc.value)

    def test_empty_and_header_only_files_are_rejected(self) -> None:
        with pytest.raises(RevenueUploadError):
            parse_revenue_csv("   ")
        with pytest.raises(RevenueUploadError) as exc:
            parse_revenue_csv("account_id,period,amount,currency\n")
        assert "no data rows" in str(exc.value)

    def test_nothing_is_written_when_any_row_fails(self) -> None:
        # All-or-nothing. An upload REPLACES its periods, so a half-accepted file would swap a
        # complete snapshot for an incomplete one and delete the rest of the accounts' revenue.
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-08,100,USD\n"
            "acct_b,2026-08,broken,USD\n"
        )
        with pytest.raises(RevenueUploadError) as exc:
            parse_revenue_csv(text)
        assert "nothing was written" in str(exc.value)


class TestDerivedIds:
    """Replacement is structural: the id of a row is a function of what it describes."""

    def test_id_is_derived_from_period_and_account_hash(self) -> None:
        assert business_event_id("2026-08", "abc") == "csv_upload:2026-08:abc"
        assert period_id_prefix("2026-08") == "csv_upload:2026-08:"
        assert business_event_id("2026-08", "abc").startswith(period_id_prefix("2026-08"))

    def test_reupload_produces_identical_ids(self) -> None:
        first = build_period_snapshots(parse_revenue_csv(GOOD), hash_account=_fake_hash)
        second = build_period_snapshots(parse_revenue_csv(GOOD), hash_account=_fake_hash)
        assert [e.business_event_id for e in first[0].events] == [
            e.business_event_id for e in second[0].events
        ]

    def test_prefix_of_one_period_does_not_match_another(self) -> None:
        assert not business_event_id("2026-09", "abc").startswith(period_id_prefix("2026-08"))


class TestSnapshotShaping:
    def test_events_carry_both_account_and_user_hash(self) -> None:
        # The account hash goes to BOTH columns on purpose: the shipped revenue readers key off
        # UserIdHash (Stripe already hashes its CUSTOMER id into it), while AccountIdHash is the
        # account dimension CTO-180 added. Writing both puts an uploaded row in exactly the same
        # place a connector row lands.
        snaps = build_period_snapshots(parse_revenue_csv(GOOD), hash_account=_fake_hash)
        ev = snaps[0].events[0]
        assert ev.user_id_hash == ev.account_id_hash == _fake_hash("acct_1001")
        assert ev.source == UPLOAD_SOURCE
        assert ev.event_name == UPLOAD_EVENT_NAME
        assert ev.value_type == "monetary"
        assert ev.value_currency == "USD"

    def test_negative_total_becomes_a_refund_so_it_nets_off(self) -> None:
        text = "account_id,period,amount,currency\nacct_a,2026-08,(500.00),USD\n"
        snaps = build_period_snapshots(parse_revenue_csv(text), hash_account=_fake_hash)
        assert snaps[0].events[0].value_type == "refund"

    def test_snapshot_totals_are_reported_back(self) -> None:
        snaps = build_period_snapshots(parse_revenue_csv(GOOD), hash_account=_fake_hash)
        assert snaps[0].account_count == 2
        assert snaps[0].total_amount_micro == 16_700_500_000
        assert snaps[0].currency == "USD"

    def test_one_snapshot_per_period(self) -> None:
        text = (
            "account_id,period,amount,currency\n"
            "acct_a,2026-07,10,USD\n"
            "acct_b,2026-08,20,USD\n"
        )
        snaps = build_period_snapshots(parse_revenue_csv(text), hash_account=_fake_hash)
        assert [s.period for s in snaps] == ["2026-07", "2026-08"]


class TestPeriodTimestamp:
    def test_closed_month_is_stamped_at_its_end(self) -> None:
        now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        assert period_occurred_at("2026-07", now=now) == datetime(
            2026, 7, 31, 23, 59, 59, tzinfo=UTC
        )

    def test_open_month_is_clamped_to_now(self) -> None:
        # A snapshot of the month in progress must not be stamped in the future: that would read
        # as revenue we have not earned yet.
        now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        assert period_occurred_at("2026-08", now=now) == now

    def test_december_rolls_the_year(self) -> None:
        now = datetime(2027, 6, 1, tzinfo=UTC)
        assert period_occurred_at("2026-12", now=now) == datetime(
            2026, 12, 31, 23, 59, 59, tzinfo=UTC
        )


def test_parsed_upload_periods_are_sorted_and_deduped() -> None:
    parsed = ParsedUpload(rows=parse_revenue_csv(GOOD).rows)
    assert parsed.periods == ("2026-08",)
