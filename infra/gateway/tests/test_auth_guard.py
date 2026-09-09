# SPDX-License-Identifier: Apache-2.0
"""Boot-time guard on the gateway's auth escape hatch (CTO-268, gateway half).

The decision table is the test: `make up` and the suite must be untouched, a deployed gateway with
authentication off must refuse to boot, and the documented auth-disabled shape (the edge proxy's
EDGE_PROXY_TENANT_ID case) must remain reachable behind the same explicit opt-in the web tier
introduced in PR #345.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from gateway.auth_guard import (
    ALLOW_INSECURE_ENV,
    InsecureAuthConfigError,
    assert_auth_config,
    check_auth_config,
    is_deployed_environment,
    is_truthy,
)


def _verdict(env: str, require_api_key: bool, allow: str = "") -> str:
    return check_auth_config(
        env=env, require_api_key=require_api_key, allow_insecure=allow
    ).kind


# --- the decision table -----------------------------------------------------------------------


@pytest.mark.parametrize("env", ["", "development", "dev", "DEV", "local", "test", "ci"])
def test_local_environments_are_never_guarded(env: str) -> None:
    # `make up`, CI and pytest all run auth-off and must need no new variable.
    assert _verdict(env, require_api_key=False) == "ok"


@pytest.mark.parametrize("env", ["production", "prod", "staging", "stage", "PRODUCTION"])
def test_auth_on_is_always_fine(env: str) -> None:
    assert _verdict(env, require_api_key=True) == "ok"


@pytest.mark.parametrize("env", ["production", "staging"])
def test_deployed_with_auth_off_and_no_opt_in_refuses(env: str) -> None:
    assert _verdict(env, require_api_key=False) == "refuse"


@pytest.mark.parametrize("opt_in", ["1", "true", "yes", "on", " TRUE "])
def test_deployed_with_auth_off_and_the_opt_in_is_allowed_but_warned(opt_in: str) -> None:
    assert _verdict("production", require_api_key=False, allow=opt_in) == "insecure-allowed"


@pytest.mark.parametrize("not_consent", ["0", "false", "off", "no", "", "maybe"])
def test_a_non_truthy_opt_in_is_not_consent(not_consent: str) -> None:
    assert _verdict("production", require_api_key=False, allow=not_consent) == "refuse"


def test_an_unrecognized_environment_is_treated_as_deployed() -> None:
    # A typo in a deployment manifest is likelier than a laptop, and this is the fail-closed
    # direction: the cost of being wrong is a refused boot with a message, not an open gateway.
    assert _verdict("prod-eu-west-1", require_api_key=False) == "refuse"
    assert is_deployed_environment("prod-eu-west-1") is True
    assert is_deployed_environment("development") is False
    assert is_deployed_environment(None) is False


def test_truthiness_matches_the_web_tier_spelling() -> None:
    # Same spelling web/lib/authGuard.ts and deploy/demo/lib-tenant.sh accept, so one operator
    # sentence covers both tiers of one deployment.
    assert [is_truthy(v) for v in ("1", "true", "yes", "on")] == [True] * 4
    assert [is_truthy(v) for v in ("0", "false", "off", "", None, 3)] == [False] * 6


# --- the messages -----------------------------------------------------------------------------


def test_the_refusal_names_the_cause_the_consequence_and_both_fixes() -> None:
    message = check_auth_config(
        env="production", require_api_key=False, allow_insecure=""
    ).message
    assert "TALLY_ENV=production" in message
    assert "TALLY_REQUIRE_API_KEY" in message
    assert ALLOW_INSECURE_ENV in message
    # The consequence is the control plane too, not just ingest: that is the part an operator
    # reading "require api key" would not guess.
    assert "/v1/tenant/" in message
    assert "/v1/batches" in message
    # And it must not break the documented auth-disabled shape blind.
    assert "EDGE_PROXY_TENANT_ID" in message


def test_the_opted_in_warning_says_what_is_open() -> None:
    message = check_auth_config(
        env="production", require_api_key=False, allow_insecure="1"
    ).message
    assert "NO AUTHENTICATION" in message


# --- the boot-time assertion ------------------------------------------------------------------


def test_assert_raises_only_on_a_refusal(caplog) -> None:
    with pytest.raises(InsecureAuthConfigError):
        assert_auth_config(
            SimpleNamespace(env="production", require_api_key=False, allow_insecure_no_auth="")
        )

    with caplog.at_level("WARNING"):
        verdict = assert_auth_config(
            SimpleNamespace(env="production", require_api_key=False, allow_insecure_no_auth="1")
        )
    assert verdict.kind == "insecure-allowed"
    assert "NO AUTHENTICATION" in caplog.text

    assert assert_auth_config(
        SimpleNamespace(env="development", require_api_key=False, allow_insecure_no_auth="")
    ).kind == "ok"


def test_the_default_settings_boot_cleanly() -> None:
    # The real defaults, not a hand-built namespace: a fresh checkout must start.
    from gateway.config import Settings

    assert assert_auth_config(Settings()).kind == "ok"
