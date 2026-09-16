# SPDX-License-Identifier: Apache-2.0
"""Per-tenant credential resolution for the cloud cost connectors (CTO-381).

Before CTO-381 every tenant's AWS connector ran as ``boto3.Session()``, ai-tally's own identity,
and the Vercel/Cloudflare token references were never read. These tests pin the replacement:

* AssumeRole carries the tenant UUID as ExternalId and a tenant-named session,
* tenant A's run never uses tenant B's role, cached credentials or secret,
* Secrets Manager token resolution (through the tenant's role, or tenant-scoped by name),
* ``aws-default-chain`` refused on a hosted gateway and honoured only when self-hosted,
* GCP fails honestly on a hosted gateway,
* an unresolvable reference records ``failed`` and emits nothing,
* no secret value reaches logs, exceptions or recorded errors.

Every AWS client is a fake handed out by the injectable ``client_factory``: no boto3, no network.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from gateway.config import Settings
from gateway.connectors import egress as egress_module
from gateway.connectors.base import ConnectorConfig
from gateway.connectors.config_admin import (
    ConfigError,
    CostConnectorAdmin,
    validate_resolvable_reference,
)
from gateway.connectors.credentials import (
    AMBIENT_AWS,
    CredentialResolutionError,
    CredentialResolver,
    TenantCredentials,
    reference_kind,
)
from gateway.connectors.egress import EgressConfig
from gateway.connectors.vercel import VercelConfig
from gateway.cost_connector_job import CostConnectorJob, CostConnectorRunError

NOW = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROLE_A = "arn:aws:iam::111111111111:role/ai-tally-connector-a"
ROLE_B = "arn:aws:iam::222222222222:role/ai-tally-connector-b"
SECRET_A = "arn:aws:secretsmanager:us-east-1:111111111111:secret:vercel-token-AbCdEf"
SECRET_B = "arn:aws:secretsmanager:us-east-1:222222222222:secret:vercel-token-GhIjKl"
TOKEN_A = "vercel-token-value-A-SUPERSECRET"
TOKEN_B = "vercel-token-value-B-SUPERSECRET"


# --- fakes ------------------------------------------------------------------------------------


class _AwsError(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.response = {"Error": {"Code": code, "Message": text}}


class FakeAws:
    """Hands out fake sts / secretsmanager / ce clients and records every call."""

    def __init__(self, *, now=lambda: NOW, trusted: dict[str, str] | None = None) -> None:
        self.now = now
        # role ARN -> the only ExternalId its trust policy accepts
        self.trusted = trusted if trusted is not None else {ROLE_A: TENANT_A, ROLE_B: TENANT_B}
        self.assume_calls: list[dict] = []
        self.secret_calls: list[tuple[str, str | None, str | None]] = []
        self.ce_calls: list[str | None] = []
        self.secrets = {SECRET_A: TOKEN_A, SECRET_B: TOKEN_B}
        self.minted = 0
        self.lifetime = timedelta(hours=1)

    def __call__(self, service, *, region, credentials):
        fake = self

        if service == "sts":

            class Sts:
                def assume_role(self, **kwargs):
                    fake.assume_calls.append(kwargs)
                    if fake.trusted.get(kwargs["RoleArn"]) != kwargs["ExternalId"]:
                        raise _AwsError(
                            "AccessDenied",
                            f"not authorized to assume {kwargs['RoleArn']} SECRETDETAIL",
                        )
                    fake.minted += 1
                    return {
                        "Credentials": {
                            "AccessKeyId": f"ASIA{fake.minted:016d}",
                            "SecretAccessKey": f"secret-access-key-{fake.minted}-SUPERSECRET",
                            "SessionToken": f"session-{kwargs['RoleArn']}-{kwargs['ExternalId']}",
                            "Expiration": fake.now() + fake.lifetime,
                        }
                    }

            return Sts()
        if service == "secretsmanager":
            who = credentials.session_token if credentials is not None else None

            class Sm:
                def get_secret_value(self, SecretId):  # noqa: N803 - boto3 spelling
                    fake.secret_calls.append((SecretId, region, who))
                    if SecretId not in fake.secrets:
                        raise _AwsError("ResourceNotFoundException", f"{SecretId} missing")
                    return {"SecretString": fake.secrets[SecretId]}

            return Sm()
        if service == "ce":
            who = credentials.session_token if credentials is not None else None
            fake.ce_calls.append(who)

            class Ce:
                def get_cost_and_usage(self, **kwargs):
                    start = kwargs["TimePeriod"]["Start"]
                    return {
                        "ResultsByTime": [
                            {
                                "TimePeriod": {"Start": start},
                                "Total": {"UnblendedCost": {"Amount": "1.25"}},
                            }
                        ]
                    }

            return Ce()
        raise AssertionError(f"unexpected service {service}")


def resolver(fake: FakeAws, *, self_hosted: bool = False, now=lambda: NOW) -> CredentialResolver:
    return CredentialResolver(self_hosted_single_tenant=self_hosted, client_factory=fake, now=now)


class FakeSink:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.ids: set[tuple[str, str]] = set()

    def span_exists(self, tenant_id, span_id):
        return (tenant_id, span_id) in self.ids

    def insert_spans(self, rows):
        self.rows.extend(rows)
        return len(rows)

    def close(self):
        pass


class FakeComputeStore:
    def __init__(self, config=None):
        self.config = config
        self.runs: list[tuple[str, str, str | None]] = []

    def load_config(self, tenant_id):
        return self.config

    def record_run(self, tenant_id, connector_id, status, *, error_message=None):
        self.runs.append((tenant_id, status, error_message))


class FakeEgressStore:
    def __init__(self, configs=None):
        self.configs = configs or []
        self.runs: list[tuple[str, str, str]] = []

    def load_configs(self, tenant_id):
        return list(self.configs)

    def recorder_for(self, provider):
        store = self

        class R:
            def record_run(self, tenant_id, connector_id, status, *, error_message=None):
                store.runs.append((tenant_id, provider, status))

        return R()


class FakeVercelStore(FakeComputeStore):
    pass


def job(
    res: CredentialResolver,
    *,
    sink: FakeSink,
    compute=None,
    egress=None,
    vercel=None,
) -> CostConnectorJob:
    return CostConnectorJob(
        None,
        compute_store=compute or FakeComputeStore(),
        egress_store=egress or FakeEgressStore(),
        vercel_store=vercel or FakeVercelStore(),
        store_factory=lambda: sink,
        now=lambda: NOW,
        resolver=res,
    )


# --- reference classification ------------------------------------------------------------------


def test_reference_kinds() -> None:
    assert reference_kind(ROLE_A) == "role_arn"
    assert reference_kind(SECRET_A) == "secret_arn"
    assert reference_kind(AMBIENT_AWS) == "ambient"
    assert reference_kind("projects/p/secrets/s") == "unsupported"
    assert reference_kind("arn:aws:iam::12345:role/short-account") == "unsupported"


# --- AssumeRole --------------------------------------------------------------------------------


def test_assume_role_uses_the_tenant_uuid_as_external_id() -> None:
    fake = FakeAws()
    creds = resolver(fake).assume_role(TENANT_A, ROLE_A)
    call = fake.assume_calls[0]
    assert call["RoleArn"] == ROLE_A
    assert call["ExternalId"] == TENANT_A
    assert TENANT_A in call["RoleSessionName"]
    assert len(call["RoleSessionName"]) <= 64
    assert creds.session_token == f"session-{ROLE_A}-{TENANT_A}"
    # The redacted repr is what reaches a log line if anyone ever formats the object.
    assert "SUPERSECRET" not in repr(creds)


def test_role_credentials_are_cached_per_tenant_and_role_until_near_expiry() -> None:
    clock = {"now": NOW}
    # STS and the resolver share one clock, as they would in production.
    fake = FakeAws(now=lambda: clock["now"])
    res = resolver(fake, now=lambda: clock["now"])
    res.assume_role(TENANT_A, ROLE_A)
    res.assume_role(TENANT_A, ROLE_A)
    assert len(fake.assume_calls) == 1
    # Inside the refresh margin: re-assume rather than hand out credentials about to expire.
    clock["now"] = NOW + timedelta(minutes=56)
    res.assume_role(TENANT_A, ROLE_A)
    assert len(fake.assume_calls) == 2


def test_already_expired_sts_credentials_fail() -> None:
    fake = FakeAws()
    fake.lifetime = timedelta(seconds=30)
    with pytest.raises(CredentialResolutionError, match="expired"):
        resolver(fake).assume_role(TENANT_A, ROLE_A)


def test_non_uuid_tenant_is_refused_rather_than_used_as_external_id() -> None:
    with pytest.raises(CredentialResolutionError, match="UUID"):
        resolver(FakeAws()).assume_role("local-dev", ROLE_A)


def test_tenant_b_pointing_at_tenant_a_role_is_refused_and_never_reuses_a_cache() -> None:
    fake = FakeAws()
    res = resolver(fake)
    res.assume_role(TENANT_A, ROLE_A)  # A's credentials are now cached
    with pytest.raises(CredentialResolutionError) as exc:
        res.assume_role(TENANT_B, ROLE_A)
    # B's attempt went to STS with B's external id, and A's trust policy refused it.
    assert fake.assume_calls[-1]["ExternalId"] == TENANT_B
    assert "AccessDenied" in str(exc.value)
    # The raw AWS error text is never echoed.
    assert "SECRETDETAIL" not in str(exc.value)


def test_credential_context_refuses_another_tenants_config() -> None:
    ctx = TenantCredentials(resolver=resolver(FakeAws()), tenant_id=TENANT_A)
    other = ConnectorConfig(tenant_id=TENANT_B, cloud_provider="aws", credentials_ref=ROLE_B)
    with pytest.raises(CredentialResolutionError, match="different tenant"):
        ctx.aws_client(other, "ce")


# --- Secrets Manager ---------------------------------------------------------------------------


def test_secret_is_read_through_the_tenants_assumed_role() -> None:
    fake = FakeAws()
    token = resolver(fake).secret_value(TENANT_A, SECRET_A, via_role=ROLE_A)
    assert token == TOKEN_A
    secret_id, region, who = fake.secret_calls[0]
    assert (secret_id, region) == (SECRET_A, "us-east-1")
    assert who == f"session-{ROLE_A}-{TENANT_A}"


def test_gateway_identity_reads_only_a_tenant_scoped_secret_name() -> None:
    fake = FakeAws()
    scoped_a = (
        f"arn:aws:secretsmanager:us-east-1:999999999999:secret:ai-tally/connectors/{TENANT_A}/vercel"
    )
    fake.secrets[scoped_a] = TOKEN_A
    res = resolver(fake)
    assert res.secret_value(TENANT_A, scoped_a) == TOKEN_A
    assert fake.secret_calls[-1][2] is None  # gateway identity, no assumed role
    # Tenant B cannot point the gateway at tenant A's secret.
    with pytest.raises(CredentialResolutionError, match="organization id"):
        res.secret_value(TENANT_B, scoped_a)
    # Nor at an unscoped secret.
    with pytest.raises(CredentialResolutionError):
        res.secret_value(TENANT_A, SECRET_A)
    assert len(fake.secret_calls) == 1


@pytest.mark.parametrize(
    "ref", ["projects/p/secrets/vercel", "vault:secret/x", AMBIENT_AWS, ROLE_A, ""]
)
def test_non_secrets_manager_token_references_fail(ref: str) -> None:
    with pytest.raises(CredentialResolutionError, match="Secrets Manager"):
        resolver(FakeAws()).secret_value(TENANT_A, ref, via_role=ROLE_A)


def test_missing_secret_fails_without_echoing_details() -> None:
    fake = FakeAws()
    missing = SECRET_A.replace("AbCdEf", "ZzZzZz")
    with pytest.raises(CredentialResolutionError) as exc:
        resolver(fake).secret_value(TENANT_A, missing, via_role=ROLE_A)
    assert "ResourceNotFoundException" in str(exc.value)
    assert "missing" not in str(exc.value)


# --- aws-default-chain -------------------------------------------------------------------------


def test_ambient_chain_is_refused_by_the_resolver_on_a_hosted_gateway() -> None:
    fake = FakeAws()
    with pytest.raises(CredentialResolutionError, match="self-hosted"):
        resolver(fake).aws_client(TENANT_A, AMBIENT_AWS, "ce")
    assert fake.ce_calls == []


def test_ambient_chain_is_honoured_only_when_self_hosted() -> None:
    fake = FakeAws()
    resolver(fake, self_hosted=True).aws_client(TENANT_A, AMBIENT_AWS, "ce")
    assert fake.ce_calls == [None]
    assert fake.assume_calls == []


def test_ambient_chain_is_rejected_at_save_time_on_a_hosted_gateway() -> None:
    for connector in ("aws_cost_explorer", "aws_egress"):
        with pytest.raises(ConfigError, match="self-hosted single-tenant"):
            validate_resolvable_reference(connector, AMBIENT_AWS, self_hosted=False)
        validate_resolvable_reference(connector, AMBIENT_AWS, self_hosted=True)
        validate_resolvable_reference(connector, ROLE_A, self_hosted=False)


def test_admin_upsert_refuses_ambient_chain_before_touching_postgres() -> None:
    # An unreachable DSN proves the refusal happens before any connection is attempted.
    hosted = CostConnectorAdmin(Settings(postgres_dsn="postgresql://nobody@127.0.0.1:1/none"))
    with pytest.raises(ConfigError, match="self-hosted single-tenant"):
        hosted.upsert(TENANT_A, "aws_cost_explorer", {"credentials_ref": AMBIENT_AWS})


def test_save_time_rules_for_token_connectors_and_gcp() -> None:
    for connector in ("vercel", "vercel_egress", "cloudflare"):
        validate_resolvable_reference(connector, SECRET_A, self_hosted=False)
        with pytest.raises(ConfigError, match="Secrets Manager"):
            validate_resolvable_reference(connector, "projects/p/secrets/t", self_hosted=False)
    with pytest.raises(ConfigError, match="not supported for hosted organizations"):
        validate_resolvable_reference("gcp_billing", "projects/p/secrets/s", self_hosted=False)
    validate_resolvable_reference("gcp_billing", "projects/p/secrets/s", self_hosted=True)
    with pytest.raises(ConfigError, match="IAM role ARN"):
        validate_resolvable_reference("aws_cost_explorer", SECRET_A, self_hosted=False)


# --- the job, end to end over fakes ------------------------------------------------------------


def test_job_bills_aws_compute_as_the_tenants_assumed_role() -> None:
    fake, sink = FakeAws(), FakeSink()
    compute = FakeComputeStore(
        ConnectorConfig(tenant_id=TENANT_A, cloud_provider="aws", credentials_ref=ROLE_A)
    )
    job(resolver(fake), sink=sink, compute=compute)(TENANT_A)
    assert fake.assume_calls[0]["ExternalId"] == TENANT_A
    assert fake.ce_calls == [f"session-{ROLE_A}-{TENANT_A}"]
    assert len(sink.rows) == 1
    assert compute.runs[-1][1] == "success"


def test_two_tenants_never_share_a_role_or_a_secret(monkeypatch) -> None:
    fake = FakeAws()
    res = resolver(fake)  # one resolver per process, as in production
    seen_tokens: list[str] = []

    def transport(method, url, *, headers, params=None, json=None, timeout=30):
        seen_tokens.append(headers["Authorization"])
        return {"items": []}

    monkeypatch.setattr(egress_module, "_requests_transport", transport)

    for tenant, role, secret in ((TENANT_A, ROLE_A, SECRET_A), (TENANT_B, ROLE_B, SECRET_B)):
        compute = FakeComputeStore(
            ConnectorConfig(tenant_id=tenant, cloud_provider="aws", credentials_ref=role)
        )
        vercel = FakeVercelStore(
            VercelConfig(tenant_id=tenant, cloud_provider="vercel", credentials_ref=secret)
        )
        job(res, sink=FakeSink(), compute=compute, vercel=vercel)(tenant)

    assert [(c["RoleArn"], c["ExternalId"]) for c in fake.assume_calls] == [
        (ROLE_A, TENANT_A),
        (ROLE_B, TENANT_B),
    ]
    assert fake.ce_calls == [f"session-{ROLE_A}-{TENANT_A}", f"session-{ROLE_B}-{TENANT_B}"]
    # Each tenant's token secret was read through its OWN role.
    assert [(s, who) for s, _, who in fake.secret_calls] == [
        (SECRET_A, f"session-{ROLE_A}-{TENANT_A}"),
        (SECRET_B, f"session-{ROLE_B}-{TENANT_B}"),
    ]
    assert seen_tokens == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"]


@pytest.mark.parametrize(
    "ref",
    [
        AMBIENT_AWS,  # hosted gateway: refused
        "projects/p/secrets/not-an-aws-role",  # malformed for AWS
        "arn:aws:iam::333333333333:role/ai-tally-connector-untrusted",  # unauthorized
    ],
)
def test_unresolvable_aws_reference_records_failed_and_emits_nothing(ref: str) -> None:
    fake, sink = FakeAws(), FakeSink()
    compute = FakeComputeStore(
        ConnectorConfig(tenant_id=TENANT_A, cloud_provider="aws", credentials_ref=ref)
    )
    with pytest.raises(CostConnectorRunError):
        job(resolver(fake), sink=sink, compute=compute)(TENANT_A)
    assert sink.rows == []
    assert fake.ce_calls == []
    assert compute.runs[-1][1] == "failed"


def test_unresolvable_token_secret_records_failed_and_emits_nothing(monkeypatch) -> None:
    def transport(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a provider was called without a resolved token")

    monkeypatch.setattr(egress_module, "_requests_transport", transport)
    sink = FakeSink()
    egress = FakeEgressStore(
        [
            EgressConfig(
                tenant_id=TENANT_A,
                cloud_provider="cloudflare",
                credentials_ref="vault:secret/cloudflare",
                resource_id="zone",
            )
        ]
    )
    with pytest.raises(CostConnectorRunError):
        job(resolver(FakeAws()), sink=sink, egress=egress)(TENANT_A)
    assert sink.rows == []
    assert egress.runs == [(TENANT_A, "cloudflare", "failed")]


def test_gcp_fails_honestly_on_a_hosted_gateway() -> None:
    sink = FakeSink()
    compute = FakeComputeStore(
        ConnectorConfig(
            tenant_id=TENANT_A,
            cloud_provider="gcp",
            credentials_ref="projects/p/secrets/s",
            bq_billing_export_table="p.d.t",
        )
    )
    with pytest.raises(CostConnectorRunError, match="compute/gcp: failed"):
        job(resolver(FakeAws()), sink=sink, compute=compute)(TENANT_A)
    assert sink.rows == []
    _, status, error = compute.runs[-1]
    assert status == "failed"
    assert "not supported for hosted organizations yet" in (error or "")


def test_no_secret_value_reaches_logs_exceptions_or_recorded_errors(monkeypatch, caplog) -> None:
    fake = FakeAws()

    def leaky_transport(method, url, *, headers, params=None, json=None, timeout=30):
        # Model an HTTP library whose error text echoes the request headers.
        raise RuntimeError(f"401 for {url} with headers {headers}")

    monkeypatch.setattr(egress_module, "_requests_transport", leaky_transport)
    recorded: list[str | None] = []

    class Recorder(FakeVercelStore):
        def record_run(self, tenant_id, connector_id, status, *, error_message=None):
            recorded.append(error_message)
            super().record_run(tenant_id, connector_id, status, error_message=error_message)

    compute = FakeComputeStore(
        ConnectorConfig(tenant_id=TENANT_A, cloud_provider="aws", credentials_ref=ROLE_A)
    )
    vercel = Recorder(
        VercelConfig(
            tenant_id=TENANT_A, cloud_provider="vercel", credentials_ref=SECRET_A, emit_egress=True
        )
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CostConnectorRunError) as exc:
        job(resolver(fake), sink=FakeSink(), compute=compute, vercel=vercel)(TENANT_A)

    haystacks = [str(exc.value), caplog.text, *[m or "" for m in recorded]]
    for text in haystacks:
        assert TOKEN_A not in text
        assert "SUPERSECRET" not in text
        assert "Bearer" not in text
    # The reason that does reach logs and the recorded error is the status-only rewrite.
    assert "Vercel usage API request failed (RuntimeError)" in caplog.text
    assert any("Vercel usage API request failed" in (m or "") for m in recorded)
    assert vercel.runs[-1][1] == "failed"


def test_live_clients_without_a_resolver_fail_rather_than_use_ambient_credentials() -> None:
    from gateway.connectors.compute import build_billing_client
    from gateway.connectors.egress import build_egress_client

    config = ConnectorConfig(tenant_id=TENANT_A, cloud_provider="aws", credentials_ref=ROLE_A)
    for client in (build_billing_client("aws"), build_egress_client("aws"), build_egress_client("vercel")):
        with pytest.raises(CredentialResolutionError, match="no credential resolver"):
            client.get_daily_costs(config, start_day=date(2026, 8, 24), end_day=date(2026, 8, 24))
