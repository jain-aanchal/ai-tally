# SPDX-License-Identifier: Apache-2.0
"""AWS Secrets Manager HMAC key provider (CTO-336).

These use an in-memory fake mimicking the four ``boto3`` Secrets Manager operations the provider
touches (``create_secret`` / ``update_secret_version_stage`` / ``get_secret_value`` /
``delete_secret``), following the same injected-client pattern as
``tests/test_replay_store_s3.py``. No AWS account, no credentials, no network, and ``boto3`` need
not be installed to run the suite.

The three failure shapes the ticket calls out each have their own case: success, unreachable
(a botocore-style connection error with no ``response`` dict), and permission denied
(``AccessDeniedException``). The distinction that matters is that only a genuine
``ResourceNotFoundException`` becomes a ``KeyError`` (an honest 404 on the bootstrap endpoint);
everything else becomes ``KeyProviderUnavailableError`` (a 503), because "we could not ask" is not
"there is nothing there".
"""

from __future__ import annotations

import uuid

import pytest

from gateway.tenant_hmac_key import _version_from_ref
from gateway.tenant_provisioning import (
    HMAC_KEY_BYTES,
    MAX_KEK_REF_LENGTH,
    KeyProviderUnavailableError,
    SecretManagerKeyProvider,
    assert_reference_fits,
)

ACCOUNT_ARN_PREFIX = "arn:aws:secretsmanager:us-east-1:123456789012:secret:"


class _FakeAwsError(Exception):
    """Stand-in for ``botocore.exceptions.ClientError``: carries a ``response`` dict."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeEndpointError(Exception):
    """Stand-in for ``botocore.exceptions.EndpointConnectionError``: no ``response`` at all."""


class _FakeSecretsManager:
    """Enough of the Secrets Manager surface to exercise the provider end to end."""

    def __init__(self, *, fail_on: dict[str, Exception] | None = None) -> None:
        # name -> {"arn", "versions": {version_id: bytes}, "stages": {label: version_id}}
        self.secrets: dict[str, dict] = {}
        self.by_arn: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.kms_key_ids: list[str] = []
        self.tags: list[list] = []
        self._fail_on = fail_on or {}

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        exc = self._fail_on.get(op)
        if exc is not None:
            raise exc

    def create_secret(self, **kwargs) -> dict:
        self._maybe_fail("create_secret")
        name = kwargs["Name"]
        if name in self.secrets:
            raise _FakeAwsError("ResourceExistsException")
        arn = f"{ACCOUNT_ARN_PREFIX}{name}-AbCdEf"
        version_id = str(uuid.uuid4())
        record = {
            "arn": arn,
            "name": name,
            "versions": {version_id: kwargs["SecretBinary"]},
            "stages": {"AWSCURRENT": version_id},
        }
        self.secrets[name] = record
        self.by_arn[arn] = record
        if "KmsKeyId" in kwargs:
            self.kms_key_ids.append(kwargs["KmsKeyId"])
        self.tags.append(kwargs.get("Tags", []))
        return {"ARN": arn, "Name": name, "VersionId": version_id}

    def update_secret_version_stage(self, **kwargs) -> dict:
        self._maybe_fail("update_secret_version_stage")
        record = self.by_arn.get(kwargs["SecretId"])
        if record is None:
            raise _FakeAwsError("ResourceNotFoundException")
        record["stages"][kwargs["VersionStage"]] = kwargs["MoveToVersionId"]
        return {"ARN": record["arn"]}

    def get_secret_value(self, **kwargs) -> dict:
        self._maybe_fail("get_secret_value")
        record = self.by_arn.get(kwargs["SecretId"])
        if record is None:
            raise _FakeAwsError("ResourceNotFoundException")
        version_id = record["stages"].get(kwargs.get("VersionStage", "AWSCURRENT"))
        if version_id is None:
            raise _FakeAwsError("ResourceNotFoundException")
        return {"ARN": record["arn"], "SecretBinary": record["versions"][version_id]}

    def delete_secret(self, **kwargs) -> dict:
        self._maybe_fail("delete_secret")
        record = self.by_arn.pop(kwargs["SecretId"], None)
        if record is None:
            raise _FakeAwsError("ResourceNotFoundException")
        self.secrets.pop(record["name"], None)
        self.deleted.append(record["arn"])
        return {"ARN": record["arn"]}


def _provider(client: _FakeSecretsManager, **kwargs) -> SecretManagerKeyProvider:
    return SecretManagerKeyProvider(client=client, **kwargs)


# --- success ----------------------------------------------------------------------------------


def test_mint_stores_material_in_aws_and_returns_only_a_reference() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)

    ref = provider.mint()

    # The reference is a pointer: an ARN plus a version selector, and nothing else.
    assert ref.startswith(ACCOUNT_ARN_PREFIX)
    assert ref.endswith("/v1")
    # The key bytes live in AWS, and no part of them appears in what we would persist.
    (record,) = list(client.secrets.values())
    material = record["versions"][record["stages"]["v1"]]
    assert len(material) == HMAC_KEY_BYTES
    assert material.hex() not in ref
    # The version selector is the same one the HMAC bootstrap parses back out for the SDK.
    assert _version_from_ref(ref) == "v1"


def test_minted_reference_satisfies_the_no_raw_secret_check() -> None:
    client = _FakeSecretsManager()
    ref = _provider(client).mint()
    # tenants.hash_salt_kek_ref CHECK: NOT LIKE 'sk-%' AND length < 512.
    assert not ref.startswith("sk-")
    assert len(ref) < MAX_KEK_REF_LENGTH
    assert assert_reference_fits(ref) == ref


def test_material_round_trips_through_the_labelled_version() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()

    material = provider.material(ref)
    assert len(material) == HMAC_KEY_BYTES
    # Stable across calls, and distinct per tenant (the whole point of a per-tenant key).
    assert provider.material(ref) == material
    assert provider.material(provider.mint()) != material


def test_material_pins_the_labelled_version_not_awscurrent() -> None:
    # The rotation-safety property: moving AWSCURRENT onto new bytes (AWS managed rotation, or an
    # operator) must NOT change what an existing reference resolves to, or every hash already
    # written under it silently stops matching.
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    original = provider.material(ref)

    record = client.by_arn[ref.rsplit("/", 1)[0]]
    rotated_version = str(uuid.uuid4())
    record["versions"][rotated_version] = b"\xff" * HMAC_KEY_BYTES
    record["stages"]["AWSCURRENT"] = rotated_version

    assert provider.material(ref) == original


def test_mint_uses_the_configured_prefix_and_customer_managed_key() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client, name_prefix="acme/hmac/", kms_key_id="alias/ai-tally")
    ref = provider.mint()
    assert "acme/hmac/" in ref
    assert client.kms_key_ids == ["alias/ai-tally"]
    # Tagged so an auditor can find every per-tenant key set.
    assert {"Key": "purpose", "Value": "tenant-hmac"} in client.tags[0]


def test_delete_removes_the_secret_outright() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    provider.delete(ref)
    assert client.by_arn == {}
    assert len(client.deleted) == 1


# --- unreachable ------------------------------------------------------------------------------


def test_mint_when_secrets_manager_is_unreachable_raises_and_stores_nothing() -> None:
    client = _FakeSecretsManager(
        fail_on={"create_secret": _FakeEndpointError("Could not connect to the endpoint URL")}
    )
    provider = _provider(client)

    with pytest.raises(KeyProviderUnavailableError) as excinfo:
        provider.mint()

    # No secret, and emphatically no fallback to a locally generated key.
    assert client.secrets == {}
    assert "create_secret" in str(excinfo.value)


def test_material_when_unreachable_is_unavailable_not_missing() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    client._fail_on = {"get_secret_value": _FakeEndpointError("connection timed out")}

    with pytest.raises(KeyProviderUnavailableError):
        provider.material(ref)


# --- permission denied ------------------------------------------------------------------------


def test_mint_permission_denied_raises_unavailable() -> None:
    client = _FakeSecretsManager(fail_on={"create_secret": _FakeAwsError("AccessDeniedException")})
    with pytest.raises(KeyProviderUnavailableError) as excinfo:
        _provider(client).mint()
    assert "AccessDeniedException" in str(excinfo.value)
    assert client.secrets == {}


def test_material_permission_denied_raises_unavailable_not_keyerror() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    client._fail_on = {"get_secret_value": _FakeAwsError("AccessDeniedException")}

    # Deliberately NOT a KeyError: an IAM gap must not be reported to the SDK as "this tenant has
    # no key", which is a permanent-looking answer to a fixable misconfiguration.
    with pytest.raises(KeyProviderUnavailableError):
        provider.material(ref)


def test_missing_secret_is_a_keyerror() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    provider.delete(ref)

    # Genuinely absent: the HMAC bootstrap turns this into an honest 404.
    with pytest.raises(KeyError):
        provider.material(ref)


def test_mint_cleans_up_when_labelling_the_version_fails() -> None:
    # A secret whose version carries no label can never be resolved by a reference, so it is
    # orphaned material the moment mint returns. It must be removed, and the failure surfaced.
    client = _FakeSecretsManager(
        fail_on={"update_secret_version_stage": _FakeAwsError("AccessDeniedException")}
    )
    provider = _provider(client)

    with pytest.raises(KeyProviderUnavailableError):
        provider.mint()

    assert client.by_arn == {}
    assert len(client.deleted) == 1


# --- reference handling -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "not-an-arn",
        f"{ACCOUNT_ARN_PREFIX}ai-tally/tenant-hmac/abc-AbCdEf",  # no version selector
        f"{ACCOUNT_ARN_PREFIX}ai-tally/tenant-hmac/abc-AbCdEf/latest",  # not a vN selector
    ],
)
def test_malformed_reference_is_refused_rather_than_guessed(ref: str) -> None:
    with pytest.raises(KeyProviderUnavailableError):
        _provider(_FakeSecretsManager()).material(ref)


def test_a_prefix_too_long_for_the_check_fails_at_mint() -> None:
    # The column is never widened to fit a reference. A naming choice that would not fit has to
    # fail where an operator reads the reason, not as a constraint violation mid-provision.
    client = _FakeSecretsManager()
    provider = _provider(client, name_prefix="x" * MAX_KEK_REF_LENGTH)
    with pytest.raises(KeyProviderUnavailableError) as excinfo:
        provider.mint()
    assert "hash_salt_kek_ref" in str(excinfo.value)


def test_secret_string_is_refused_rather_than_decoded() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    record = client.by_arn[ref.rsplit("/", 1)[0]]
    record["versions"][record["stages"]["v1"]] = None

    with pytest.raises(KeyProviderUnavailableError) as excinfo:
        provider.material(ref)
    assert "SecretBinary" in str(excinfo.value)


def test_a_short_key_is_refused() -> None:
    client = _FakeSecretsManager()
    provider = _provider(client)
    ref = provider.mint()
    record = client.by_arn[ref.rsplit("/", 1)[0]]
    record["versions"][record["stages"]["v1"]] = b"tooshort"

    with pytest.raises(KeyProviderUnavailableError):
        provider.material(ref)


def test_assert_reference_fits_rejects_a_raw_looking_secret() -> None:
    with pytest.raises(KeyProviderUnavailableError):
        assert_reference_fits("sk-live-abcdef")
    with pytest.raises(KeyProviderUnavailableError):
        assert_reference_fits("")
