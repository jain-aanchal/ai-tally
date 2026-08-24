# SPDX-License-Identifier: Apache-2.0
"""Validation rules for the cost-connector write path (CTO-176).

Pure-logic coverage only: every test here runs without Postgres. The point is that a bad config is
rejected at the edge, before it can reach a column, and that the rejection message tells the
operator what to do instead.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gateway.connectors.config_admin import (
    ALL_CONNECTORS,
    AMBIENT_AWS,
    ConfigError,
    _clean_tag_filter,
    _clean_usd_per_gb,
    looks_like_reference,
    validate_credentials_ref,
)


class TestCredentialReferences:
    """The hard rule from 0011/0012/0016: references only, never raw keys."""

    @pytest.mark.parametrize(
        "raw",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "whsec_abcdefghijklmnopqrstuvwx",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "-----BEGIN PRIVATE KEY-----\nMIIE",
        ],
    )
    def test_raw_credentials_are_rejected(self, raw: str) -> None:
        with pytest.raises(ConfigError) as exc:
            validate_credentials_ref(raw)
        # The message has to be actionable, not just "invalid".
        assert "secret manager" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "ref",
        [
            AMBIENT_AWS,
            "arn:aws:iam::123456789012:role/tally-cost-reader",
            "projects/my-proj/secrets/tally-billing-sa",
            "vault:secret/cloudflare#analytics-token",
            "gcpkms://projects/p/locations/l/keyRings/k",
            "sm://tally/vercel-token",
        ],
    )
    def test_references_are_accepted(self, ref: str) -> None:
        assert validate_credentials_ref(ref) == ref
        assert looks_like_reference(ref)

    def test_empty_is_rejected(self) -> None:
        for value in ("", "   ", None, 42):
            with pytest.raises(ConfigError):
                validate_credentials_ref(value)

    def test_overlong_reference_is_rejected(self) -> None:
        # The column bounds this at 512; reject before the driver does.
        with pytest.raises(ConfigError):
            validate_credentials_ref("arn:" + "x" * 600)

    def test_ambient_chain_bypasses_length_and_shape_checks(self) -> None:
        assert validate_credentials_ref(AMBIENT_AWS) == AMBIENT_AWS

    def test_unknown_shape_is_allowed_but_not_flagged_as_reference(self) -> None:
        # We deliberately do not allowlist URI schemes: deployments differ. A plausible-looking
        # value passes, and `looks_like_reference` merely reports that we did not recognise it.
        ref = validate_credentials_ref("my-corp-secret-store/tally/aws")
        assert ref == "my-corp-secret-store/tally/aws"
        assert not looks_like_reference(ref)

    def test_field_name_appears_in_the_error(self) -> None:
        with pytest.raises(ConfigError) as exc:
            validate_credentials_ref("", field="access_token_ref")
        assert "access_token_ref" in str(exc.value)


class TestTagFilter:
    def test_dict_round_trips_and_is_trimmed(self) -> None:
        assert _clean_tag_filter({" tally:workload ": " ai "}, field="tag_filter") == {
            "tally:workload": "ai"
        }

    def test_json_string_is_parsed(self) -> None:
        assert _clean_tag_filter('{"tally:workload":"ai"}', field="tag_filter") == {
            "tally:workload": "ai"
        }

    def test_none_passes_through_so_the_column_default_applies(self) -> None:
        assert _clean_tag_filter(None, field="tag_filter") is None

    @pytest.mark.parametrize("bad", ["not json", "[1,2]", 7, {"k": 1}, {"": "v"}])
    def test_bad_shapes_are_rejected(self, bad: object) -> None:
        with pytest.raises(ConfigError):
            _clean_tag_filter(bad, field="tag_filter")


class TestUsdPerGb:
    def test_decimal_parsing_avoids_float(self) -> None:
        assert _clean_usd_per_gb("0.09") == Decimal("0.09")

    def test_blank_is_none_so_vercel_and_aws_stay_null(self) -> None:
        assert _clean_usd_per_gb(None) is None
        assert _clean_usd_per_gb("") is None

    def test_negative_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _clean_usd_per_gb("-1")

    def test_non_numeric_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            _clean_usd_per_gb("free")


def test_connector_ids_cover_every_configurable_source() -> None:
    # The dashboard's CONFIGURABLE list (web/lib/costConnectors.ts) must stay in step with this.
    assert ALL_CONNECTORS == {
        "aws_cost_explorer",
        "gcp_billing",
        "vercel",
        "cloudflare",
        "aws_egress",
        "vercel_egress",
    }
