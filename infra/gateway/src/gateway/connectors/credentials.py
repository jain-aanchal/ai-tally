# SPDX-License-Identifier: Apache-2.0
"""Per-tenant credential resolution for the cloud cost connectors (CTO-381).

WHY this exists. The connector control plane (CTO-176) stored a ``credentials_ref`` for every
tenant, and nothing ever resolved it. The AWS fetchers built ``boto3.Session()`` when no session
factory was injected, and nothing injected one, so every tenant's Cost Explorer query ran as
ai-tally's own deployment identity: at best a failure, at worst ai-tally's spend reported as the
customer's. The Vercel and Cloudflare fetchers said a "prod wrapper" would resolve the token; no such
wrapper existed and the live requests carried no Authorization header at all. This module is that
missing wrapper, and every connector fetcher now goes through it.

What a reference may be
-----------------------
* **An IAM role ARN** (``arn:aws:iam::<account>:role/<name>``). The gateway calls STS ``AssumeRole``
  with its own AWS identity, ``ExternalId`` set to the tenant UUID and a session name carrying the
  tenant id. The ExternalId is the confused-deputy guard: tenant B pasting tenant A's role ARN gets
  an AssumeRole call carrying B's id, which A's trust policy refuses. Temporary credentials are
  cached per ``(tenant, role)`` and only until shortly before they expire, so a revoked trust policy
  takes effect within one session lifetime and never leaks across tenants.
* **An AWS Secrets Manager secret ARN**, for API tokens (Vercel, Cloudflare). Read through the
  tenant's assumed role when the tenant has one, which keeps the ExternalId guard in front of the
  read. Otherwise read with the gateway's own identity, but only when the secret NAME starts with
  ``<prefix><tenant uuid>/``: a resource policy cannot condition on ExternalId, so the tenant-scoped
  name is what stops tenant B from pointing the gateway at a secret that belongs to tenant A. The
  token lives in a local variable for one fetch. It is never cached, logged, stored, or put in an
  exception message.
* **``aws-default-chain``**. Honoured only when the gateway runs as a self-hosted, single-tenant
  deployment (``TALLY_CONNECTORS_SELF_HOSTED_SINGLE_TENANT``). On a hosted gateway the ambient chain
  is ai-tally's identity, not the tenant's, so it is refused at save time and fails the job if a row
  already holds it. There is no fallback to ambient credentials for a tenant, anywhere.

Failure posture
---------------
Anything unresolvable (malformed, unsupported, unauthorized, expired, empty) raises
:class:`CredentialResolutionError`. The connector base turns that into a ``failed`` run that emits
no span. Messages name the reference kind and the AWS error code only, never a credential, and
never the raw AWS error text (which can echo request details).

boto3 is imported lazily, and every AWS client comes from one injectable ``client_factory`` so the
test suite runs with fakes and no network.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: The one non-reference value the schema blesses: the ambient AWS credential chain.
AMBIENT_AWS = "aws-default-chain"

#: Default name prefix for secrets the gateway reads with its OWN identity. The tenant UUID follows
#: it (``ai-tally/connectors/<uuid>/vercel-token``) so the name itself proves which tenant owns it.
DEFAULT_SECRET_NAME_PREFIX = "ai-tally/connectors/"

_ROLE_ARN_RE = re.compile(r"^arn:aws(?:-[a-z]+)*:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")
_SECRET_ARN_RE = re.compile(
    r"^arn:aws(?:-[a-z]+)*:secretsmanager:([a-z0-9-]+):(\d{12}):secret:([\w/+=.@-]{1,512})$"
)
# STS RoleSessionName: 2-64 chars of [\w+=,.@-].
_SESSION_NAME_BAD = re.compile(r"[^\w+=,.@-]")

#: Refresh cached role credentials this long before STS says they expire, so a fetch that starts
#: just before expiry does not fail halfway through a paginated billing call.
DEFAULT_REFRESH_MARGIN = timedelta(minutes=5)
#: One hour is the default maximum session a role allows without the customer raising it.
DEFAULT_SESSION_SECONDS = 3600

GCP_HOSTED_UNSUPPORTED = (
    "GCP billing connectors are not supported for hosted organizations yet: ai-tally cannot "
    "impersonate a customer service account from a reference. Use AWS, Vercel or Cloudflare."
)


class CredentialResolutionError(RuntimeError):
    """A tenant's credential reference could not be resolved. The message is safe to store."""


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """Temporary credentials from AssumeRole. ``repr`` is redacted so they cannot leak via logs."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expiration: datetime

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"AwsCredentials(expiration={self.expiration.isoformat()}, values=***redacted***)"

    __str__ = __repr__


class AwsClientFactory(Protocol):
    """Builds one AWS service client. ``credentials=None`` means the gateway's own identity."""

    def __call__(
        self, service: str, *, region: str | None, credentials: AwsCredentials | None
    ) -> Any: ...


def _boto3_client_factory(
    service: str, *, region: str | None, credentials: AwsCredentials | None
) -> Any:  # pragma: no cover - exercised only against live AWS
    try:
        import boto3  # lazy: keep boto3 out of the base install / test path
    except ImportError as exc:
        raise CredentialResolutionError(
            "cloud cost connectors need boto3 on the gateway: install the [secrets] extra"
        ) from exc
    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if credentials is not None:
        kwargs["aws_access_key_id"] = credentials.access_key_id
        kwargs["aws_secret_access_key"] = credentials.secret_access_key
        kwargs["aws_session_token"] = credentials.session_token
    return boto3.client(service, **kwargs)


def reference_kind(ref: str) -> str:
    """Classify a stored reference: ``role_arn`` | ``secret_arn`` | ``ambient`` | ``unsupported``."""
    ref = (ref or "").strip()
    if ref == AMBIENT_AWS:
        return "ambient"
    if _ROLE_ARN_RE.match(ref):
        return "role_arn"
    if _SECRET_ARN_RE.match(ref):
        return "secret_arn"
    return "unsupported"


def aws_error_code(exc: BaseException) -> str:
    """``response['Error']['Code']`` off a botocore ClientError, without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "")
    return ""


def redact(message: str, *secrets: str | None) -> str:
    """Replace every occurrence of each non-empty secret in ``message``. Defence in depth only."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***redacted***")
    return message


def _require_tenant_uuid(tenant_id: str) -> str:
    # The ExternalId and the secret-name scope are both derived from this value, so a NAME spelling
    # (``local-dev``) would silently change the confused-deputy guard. Refuse rather than guess.
    try:
        return str(uuid.UUID(str(tenant_id)))
    except (ValueError, TypeError, AttributeError):
        raise CredentialResolutionError(
            "the tenant id is not a UUID, so no external id can be derived for its credentials"
        ) from None


class CredentialResolver:
    """Resolves one tenant's references into live, short-lived credentials. Thread-safe.

    Constructed once per gateway process; the scheduler calls job bodies on worker threads, so the
    role cache is guarded by a lock.
    """

    def __init__(
        self,
        *,
        self_hosted_single_tenant: bool = False,
        secret_name_prefix: str = DEFAULT_SECRET_NAME_PREFIX,
        client_factory: AwsClientFactory | None = None,
        now: Callable[[], datetime] | None = None,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        session_seconds: int = DEFAULT_SESSION_SECONDS,
    ) -> None:
        self._self_hosted = bool(self_hosted_single_tenant)
        self._prefix = secret_name_prefix
        self._client_factory: AwsClientFactory = client_factory or _boto3_client_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._margin = refresh_margin
        self._session_seconds = session_seconds
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], AwsCredentials] = {}

    @property
    def self_hosted_single_tenant(self) -> bool:
        return self._self_hosted

    # --- AWS role credentials -------------------------------------------------------------------

    def aws_client(self, tenant_id: str, ref: str, service: str, *, region: str | None = None):
        """An AWS client for ``service`` acting as the tenant, per its stored reference."""
        kind = reference_kind(ref)
        if kind == "ambient":
            if not self._self_hosted:
                raise CredentialResolutionError(
                    "aws-default-chain is only accepted on a self-hosted single-tenant deployment. "
                    "Connect an IAM role ARN that trusts ai-tally with your organization id as the "
                    "external id."
                )
            return self._client_factory(service, region=region, credentials=None)
        if kind != "role_arn":
            raise CredentialResolutionError(
                "the AWS credential reference must be an IAM role ARN "
                "(arn:aws:iam::<account>:role/<name>)"
            )
        credentials = self.assume_role(tenant_id, ref)
        return self._client_factory(service, region=region, credentials=credentials)

    def assume_role(self, tenant_id: str, role_arn: str) -> AwsCredentials:
        """Temporary credentials for ``role_arn`` on behalf of ``tenant_id``, cached until near expiry."""
        tenant = _require_tenant_uuid(tenant_id)
        role_arn = role_arn.strip()
        if not _ROLE_ARN_RE.match(role_arn):
            raise CredentialResolutionError("the role reference is not a valid IAM role ARN")
        key = (tenant, role_arn)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expiration - self._margin > self._now():
                return cached
            self._cache.pop(key, None)

        session_name = _SESSION_NAME_BAD.sub("-", f"ai-tally-{tenant}")[:64]
        try:
            sts = self._client_factory("sts", region=None, credentials=None)
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                ExternalId=tenant,
                DurationSeconds=self._session_seconds,
            )
        except CredentialResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify by AWS error code, never echo the text
            code = aws_error_code(exc) or type(exc).__name__
            raise CredentialResolutionError(
                f"could not assume the configured IAM role ({code}). Check that its trust policy "
                "allows ai-tally with your organization id as sts:ExternalId."
            ) from None

        credentials = _parse_sts_credentials(response)
        if credentials.expiration - self._margin <= self._now():
            raise CredentialResolutionError("STS returned credentials that are already expired")
        with self._lock:
            self._cache[key] = credentials
        return credentials

    # --- Secrets Manager tokens -----------------------------------------------------------------

    def secret_value(self, tenant_id: str, ref: str, *, via_role: str | None = None) -> str:
        """Read an API token from a Secrets Manager ARN. Never cached, never echoed.

        ``via_role`` is the tenant's IAM role ARN when it has one; the read then runs as that role,
        behind its ExternalId. Without one the gateway reads with its own identity, and only a
        secret whose name is scoped to this tenant (see the module docstring) is allowed.
        """
        tenant = _require_tenant_uuid(tenant_id)
        ref = (ref or "").strip()
        match = _SECRET_ARN_RE.match(ref)
        if match is None:
            raise CredentialResolutionError(
                "the token reference must be an AWS Secrets Manager secret ARN "
                "(arn:aws:secretsmanager:<region>:<account>:secret:<name>)"
            )
        region, _account, name = match.groups()

        credentials: AwsCredentials | None = None
        if via_role and reference_kind(via_role) == "role_arn":
            credentials = self.assume_role(tenant, via_role)
        elif not name.startswith(f"{self._prefix}{tenant}/"):
            raise CredentialResolutionError(
                "without an IAM role for this organization, ai-tally only reads secrets named "
                f"{self._prefix}<organization id>/..., so another organization's secret cannot "
                "be referenced"
            )

        value: object = None
        try:
            client = self._client_factory(
                "secretsmanager", region=region, credentials=credentials
            )
            response = client.get_secret_value(SecretId=ref)
            if isinstance(response, dict):
                value = response.get("SecretString")
        except CredentialResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify by AWS error code, never echo the text
            code = aws_error_code(exc) or type(exc).__name__
            raise CredentialResolutionError(
                f"could not read the configured secret ({code})"
            ) from None
        if not isinstance(value, str) or not value.strip():
            raise CredentialResolutionError("the configured secret has no string value")
        return value.strip()


def _parse_sts_credentials(response: object) -> AwsCredentials:
    creds = response.get("Credentials") if isinstance(response, dict) else None
    if not isinstance(creds, dict):
        raise CredentialResolutionError("STS returned no credentials")
    expiration = creds.get("Expiration")
    if isinstance(expiration, str):
        try:
            expiration = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            expiration = None
    if not isinstance(expiration, datetime):
        raise CredentialResolutionError("STS returned credentials without an expiry")
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    fields = [creds.get(k) for k in ("AccessKeyId", "SecretAccessKey", "SessionToken")]
    if not all(isinstance(v, str) and v for v in fields):
        raise CredentialResolutionError("STS returned incomplete credentials")
    return AwsCredentials(
        access_key_id=fields[0],  # type: ignore[arg-type]
        secret_access_key=fields[1],  # type: ignore[arg-type]
        session_token=fields[2],  # type: ignore[arg-type]
        expiration=expiration,
    )


@dataclass(frozen=True, slots=True)
class TenantCredentials:
    """One tenant's resolution context for one job run, handed to the live connector clients.

    Binding the tenant here, and refusing a config for any other tenant, is what makes a wiring
    mistake fail closed rather than resolve tenant B's reference under tenant A's run.
    """

    resolver: CredentialResolver
    tenant_id: str
    #: The tenant's IAM role ARN from its AWS connector config, if any. Token secrets are read
    #: through it so the ExternalId guard also covers Vercel and Cloudflare.
    role_ref: str | None = None

    def _check(self, config_tenant: str) -> None:
        if str(config_tenant) != str(self.tenant_id):
            raise CredentialResolutionError(
                "connector config belongs to a different tenant than this run"
            )

    def aws_client(self, config: Any, service: str, *, region: str | None = None):
        self._check(config.tenant_id)
        return self.resolver.aws_client(
            config.tenant_id, config.credentials_ref, service, region=region
        )

    def token_for(self, config: Any) -> str:
        self._check(config.tenant_id)
        return self.resolver.secret_value(
            config.tenant_id, config.credentials_ref, via_role=self.role_ref
        )


def build_resolver(settings: Any) -> CredentialResolver:
    """The production resolver from gateway settings."""
    return CredentialResolver(
        self_hosted_single_tenant=bool(
            getattr(settings, "connectors_self_hosted_single_tenant", False)
        ),
        secret_name_prefix=str(
            getattr(settings, "connector_secret_name_prefix", DEFAULT_SECRET_NAME_PREFIX)
        ),
    )


__all__ = [
    "AMBIENT_AWS",
    "DEFAULT_SECRET_NAME_PREFIX",
    "GCP_HOSTED_UNSUPPORTED",
    "AwsCredentials",
    "CredentialResolutionError",
    "CredentialResolver",
    "TenantCredentials",
    "aws_error_code",
    "build_resolver",
    "redact",
    "reference_kind",
]
