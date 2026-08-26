"""Span -> row promotion of the CTO-182 account dimension.

Three contracts checked here:

1. Presence — ``gen_ai.account_id_hash`` / ``gen_ai.account_id_hash_key_version`` land in the
   ``AccountIdHash`` / ``AccountIdHashKeyVersion`` typed columns, exactly like the user hash.
2. Absence — a span with no account attribute still maps cleanly, writing ``''`` (the
   unattributed bucket the DDL documents), never a null and never a placeholder that could be
   mistaken for a real account.
3. Label passthrough — ``gen_ai.account_label`` is wire-only. It is accepted without error and is
   NOT persisted: not as a column, not in the ``SpanAttributes`` long-tail map. The Postgres label
   store it belongs in is CTO-186 (B7).
"""

from __future__ import annotations

from gateway.mapping import COLUMNS, span_to_row, wire_only_account_label
from gateway.validation import SpanValidator

_HASH = "a" * 64
_OTHER_HASH = "b" * 64


def _row_dict(row: tuple[object, ...]) -> dict[str, object]:
    assert len(row) == len(COLUMNS)
    return dict(zip(COLUMNS, row, strict=True))


def test_account_hash_promoted_to_typed_columns() -> None:
    span = {
        "gen_ai.account_id_hash": _HASH,
        "gen_ai.account_id_hash_key_version": "v2",
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    assert row["AccountIdHash"] == _HASH
    assert row["AccountIdHashKeyVersion"] == "v2"


def test_account_hash_absent_writes_empty_string_not_null() -> None:
    """The unattributed bucket is '' — never None, never a 'unknown'-style placeholder."""
    row = _row_dict(span_to_row({}, tenant_id="t1", effective_ts_ns=0))
    assert row["AccountIdHash"] == ""
    assert row["AccountIdHashKeyVersion"] == ""


def test_account_columns_sit_alongside_user_columns() -> None:
    """Account and user hashes are independent dimensions and must not overwrite each other."""
    span = {
        "gen_ai.user_id_hash": _OTHER_HASH,
        "gen_ai.user_id_hash_key_version": "v1",
        "gen_ai.account_id_hash": _HASH,
        "gen_ai.account_id_hash_key_version": "v2",
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))
    assert row["UserIdHash"] == _OTHER_HASH
    assert row["UserIdHashKeyVersion"] == "v1"
    assert row["AccountIdHash"] == _HASH
    assert row["AccountIdHashKeyVersion"] == "v2"


def test_account_attrs_not_duplicated_in_span_attributes_map() -> None:
    span = {
        "gen_ai.account_id_hash": _HASH,
        "gen_ai.account_id_hash_key_version": "v2",
    }
    extra = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))["SpanAttributes"]
    assert isinstance(extra, dict)
    assert "gen_ai.account_id_hash" not in extra
    assert "gen_ai.account_id_hash_key_version" not in extra


def test_account_label_is_accepted_but_never_persisted() -> None:
    """CTO-186 seam: the label rides the wire, ingests fine, and reaches no ClickHouse column."""
    span = {
        "gen_ai.account_id_hash": _HASH,
        "gen_ai.account_id_hash_key_version": "v2",
        "gen_ai.account_label": "Acme Robotics",
    }
    row = _row_dict(span_to_row(span, tenant_id="t1", effective_ts_ns=0))

    # The hash still lands: carrying a label must not disturb attribution.
    assert row["AccountIdHash"] == _HASH

    # There is no label column at all, and it did not leak into the long-tail map either.
    assert "AccountLabel" not in COLUMNS
    extra = row["SpanAttributes"]
    assert isinstance(extra, dict)
    assert "gen_ai.account_label" not in extra
    # Belt and braces: the label string appears nowhere in the row.
    assert "Acme Robotics" not in [v for v in row.values() if isinstance(v, str)]
    assert "Acme Robotics" not in extra.values()


def test_wire_only_account_label_seam_reads_the_label() -> None:
    """The seam CTO-186 (B7) will hang the Postgres upsert on returns the parsed label."""
    assert wire_only_account_label({"gen_ai.account_label": "Acme Robotics"}) == "Acme Robotics"
    assert wire_only_account_label({"gen_ai.account_label": ""}) is None
    assert wire_only_account_label({"gen_ai.account_label": 7}) is None
    assert wire_only_account_label({}) is None


def test_validator_accepts_a_hashed_account_and_a_label() -> None:
    v = SpanValidator()
    verdict = v.validate(
        {
            "gen_ai.account_id_hash": _HASH,
            "gen_ai.account_id_hash_key_version": "v2",
            "gen_ai.account_label": "Acme Robotics",
        }
    )
    assert verdict.accepted, verdict.message


def test_validator_rejects_a_raw_account_id() -> None:
    """Only the hash reaches storage: a raw account id is a raw identifier and is refused."""
    v = SpanValidator()
    verdict = v.validate({"gen_ai.account_id_hash": "acme-robotics-inc"})
    assert not verdict.accepted
    assert "not a hash" in (verdict.message or "")
