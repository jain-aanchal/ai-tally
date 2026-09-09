# SPDX-License-Identifier: Apache-2.0
"""Provision an ai-tally tenant from a verified Clerk ``organization.created`` event (Initiative 1).

WHY this exists. Until now ai-tally was single-tenant: the one ``local-dev`` row was created by the
seed script. Initiative 1 makes Clerk the system of record for identity, and every new Clerk
organization must become a tenant with its OWN per-org HMAC key set so user and account hashes
cannot be joined across tenants. This module is the gateway half of that: the web ``/api/webhooks/
clerk`` route verifies the svix signature (the gateway is private and never sees Clerk directly),
then forwards the verified event to ``POST /v1/tenant/provision``, which lands here.

THE TWO INVARIANTS THIS MODULE HOLDS.

* **Idempotent and race-safe.** Clerk retries webhooks, and two deliveries can race. Provision is
  safe to call any number of times for one org: a redelivery returns the existing tenant and mints
  no new key material, and two concurrent first-deliveries settle on one tenant via
  ``INSERT ... ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS NOT NULL DO NOTHING RETURNING`` (the
  arbiter repeats the partial-index predicate so Postgres infers ``uq_tenants_clerk_org_id``).

* **No raw secret, no orphaned key.** The per-org HMAC key set is stored ONLY as a reference in
  ``tenants.hash_salt_kek_ref``, honoring its ``no_raw_secret`` CHECK (not ``sk-%``, length < 512).
  A tenant is never created without a usable reference (a mint failure fails the provision), and the
  loser of a race deletes the key set it minted so no orphaned material is left behind.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import psycopg

from gateway.config import Settings

logger = logging.getLogger(__name__)

#: Data-residency region stamped on a provisioned tenant. Initiative 1 does no residency routing on
#: ``region`` (§1 non-goals); the column stays and defaults here, matching how the seed defaults it.
DEFAULT_REGION = "auto"

#: Upper bound the ``no_raw_secret`` CHECK on ``tenants.hash_salt_kek_ref`` enforces
#: (``length(hash_salt_kek_ref) < 512``, db/postgres/0001_control_plane.sql). Every provider
#: validates its own reference against this BEFORE the insert, so a provider whose naming grew too
#: long fails where the operator can read the reason instead of as a constraint violation mid-
#: provision. The column is never widened to make a reference fit: a reference that does not fit is
#: a naming bug, and the CHECK is what encodes "this column holds a pointer, not a secret".
MAX_KEK_REF_LENGTH = 512

#: Scheme for the local-dev HMAC key reference. It must satisfy the ``no_raw_secret`` CHECK on
#: ``tenants.hash_salt_kek_ref`` (not ``sk-%``, length < 512). The trailing ``/v1`` leaves room for a
#: version selector so a future rotation does not orphan historical hashes (§4.1, "Versioning").
_LOCAL_KEK_SCHEME = "local"


class ProvisionError(ValueError):
    """Caller-facing validation error on the provision request. Surfaces as HTTP 422."""


class KeyProviderUnavailableError(RuntimeError):
    """The key provider could not be reached, or refused the call (CTO-336).

    Distinct from :class:`ProvisionError` on purpose. A ProvisionError says the CALLER sent something
    wrong (422). This says our own dependency is unavailable or misconfigured: the request was fine
    and we cannot answer it, which is a 503. Honest under uncertainty, both ways: the provisioner
    writes no tenant row when a mint raises this, and the HMAC bootstrap serves an error rather than
    fabricated or locally-derived bytes. There is deliberately NO fallback to the local provider,
    because locally-derived material would produce hashes that look valid while being derived from a
    process secret rather than the tenant's own key, which is the exact property the
    identifiers-by-hash invariant promises.
    """


def assert_reference_fits(ref: str) -> str:
    """Validate a key reference against the ``no_raw_secret`` CHECK before it reaches Postgres.

    Returns the reference so call sites can ``return assert_reference_fits(...)``.
    """
    if not ref:
        raise KeyProviderUnavailableError("key provider returned an empty reference")
    if ref.startswith("sk-"):
        # The CHECK's shape test for "somebody pasted a raw secret in here".
        raise KeyProviderUnavailableError(
            "key reference looks like a raw secret (starts with 'sk-'); the column stores a "
            "pointer, never key material"
        )
    if len(ref) >= MAX_KEK_REF_LENGTH:
        raise KeyProviderUnavailableError(
            f"key reference is {len(ref)} chars, which does not fit tenants.hash_salt_kek_ref "
            f"(< {MAX_KEK_REF_LENGTH}). Shorten the secret name prefix; do not widen the column."
        )
    return ref


@runtime_checkable
class KeyMaterialProvider(Protocol):
    """Mints and deletes a per-org HMAC key set, returning only an opaque reference.

    Production backs this with Secret Manager / KMS and returns its resource reference; local dev
    uses :class:`LocalDevKeyProvider`. The application never persists raw key material to Postgres:
    only the reference is stored, in ``tenants.hash_salt_kek_ref``.

    ``material`` resolves a reference back to the active key bytes. It is the seam the Initiative 2
    HMAC bootstrap (``GET /v1/tenant/hmac-key``, spec §3.2) reads through so the SDK can hash account
    and user ids in the customer process. In prod that is a KMS/Secret Manager fetch; here it is the
    in-process map below. It returns one tenant's active symmetric key only, never a KEK and never
    another tenant's material.
    """

    def mint(self) -> str: ...

    def delete(self, ref: str) -> None: ...

    def material(self, ref: str) -> bytes: ...


@dataclass
class LocalDevKeyProvider:
    """Local-dev HMAC key provider: no cloud KMS, no cloud dependency for ``make up``.

    DURABLE ACROSS RESTARTS (Initiative 2 §3.2 review). The material is DERIVED deterministically from
    the durable reference (``tenants.hash_salt_kek_ref``) and a process root secret, so a tenant
    provisioned before a restart resolves to the SAME bytes afterwards. The previous version held the
    bytes in an in-process dict only, so ``GET /v1/tenant/hmac-key`` 404'd for a durably-provisioned
    tenant after any gateway restart. Deriving from the stored reference removes that failure without
    persisting raw key material anywhere: the reference is durable, the root secret is config, and the
    32-byte key set is HMAC-SHA256(root, ref). This is the local analog of Secret Manager / KMS; the
    raw bytes never touch Postgres or a log.

    ``mint`` still returns a fresh, unique reference, so distinct tenants derive distinct material.
    ``delete`` / ``has`` track a minted-set purely for the provisioner's orphan-cleanup bookkeeping on
    a lost race; they do NOT gate ``material``, which derives from the reference alone so it survives a
    restart that empties the set.
    """

    #: Root secret material is derived under. Deterministic across restarts by design (dev-only).
    root_secret: bytes = b"tally-local-dev-hmac-root-secret-do-not-use-in-prod"
    _minted: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(self) -> str:
        ref = f"{_LOCAL_KEK_SCHEME}://hmac/{uuid.uuid4()}/v1"
        with self._lock:
            self._minted.add(ref)
        return ref

    def delete(self, ref: str) -> None:
        with self._lock:
            self._minted.discard(ref)

    def has(self, ref: str) -> bool:
        """Whether this process minted ``ref`` and has not deleted it. For orphan-cleanup tests.

        Note this is bookkeeping, NOT material availability: ``material`` derives from the reference
        and so resolves any well-formed reference, including one minted in a prior process.
        """
        with self._lock:
            return ref in self._minted

    def material(self, ref: str) -> bytes:
        """Return the active key bytes for ``ref``, derived deterministically from the root secret.

        The bytes are the tenant's own active HMAC key set. They are handed only to the HMAC
        bootstrap endpoint under the tenant's own ingest key (spec §3.2) and are never logged. Because
        the material is HMAC-SHA256(root, ref), a durably-stored reference (the seeded ``local-dev``
        key, or any tenant provisioned in a prior process) resolves to the same 32 bytes after a
        restart, rather than 404-ing. An empty reference is still a miss (``KeyError``): there is no
        material to derive from nothing.
        """
        if not ref:
            raise KeyError("no key material for an empty reference")
        return hmac.new(self.root_secret, ref.encode("utf-8"), hashlib.sha256).digest()


#: Length of a minted HMAC key set, in bytes. 32 bytes is HMAC-SHA256's block-optimal key size and
#: matches what :class:`LocalDevKeyProvider` derives, so the SDK sees one shape from either provider.
HMAC_KEY_BYTES = 32

#: A reference's trailing version selector: ``.../v1``. Parsed here and by
#: ``tenant_hmac_key._version_from_ref``, which surfaces it to the SDK as ``key_version``.
_VERSION_SELECTOR = re.compile(r"^v\d+$")

#: The first version a minted key set carries. Rotation (see the class docstring) mints a higher one.
_FIRST_VERSION = "v1"


class SecretManagerKeyProvider:
    """Per-tenant HMAC key sets held in AWS Secrets Manager (CTO-336, Initiative 2 §3.2).

    Selected by ``TALLY_HMAC_KEY_PROVIDER=kms`` (also spelled ``secret-manager``). Until CTO-336
    every method here raised ``NotImplementedError``, so a real deployment either could not provision
    a tenant at all or had to fall back to :class:`LocalDevKeyProvider`, whose material is derived
    from a process-wide root secret sitting in config. That fallback is what the credentials-by-
    reference invariant forbids, so this class exists to make the honest path the working one.

    WHAT IS STORED WHERE, and why the reference stays a reference.
      The 32 random bytes are generated in memory, written straight to Secrets Manager, and dropped.
      What is persisted to ``tenants.hash_salt_kek_ref`` is the secret's ARN plus a version selector:

          arn:aws:secretsmanager:REGION:ACCOUNT:secret:ai-tally/tenant-hmac/<uuid>-AbCdEf/v1

      That is roughly 120 characters, well inside the ``no_raw_secret`` CHECK
      (``NOT LIKE 'sk-%' AND length < 512``), and :func:`assert_reference_fits` proves it before the
      insert rather than letting Postgres discover it. The reference is a pointer in the strict
      sense: it is useless to anyone without IAM permission on that ARN, it contains no key material
      and no entropy from the key, and it is safe to log (we still do not log it needlessly).

    ROTATION IS OUT OF SCOPE FOR CTO-336, and this design does not preclude it.
      Rotating a tenant's HMAC key changes every hash computed after it, so it is not a key-store
      operation but a product decision about historical joins: the SDK stamps ``key_version`` on
      spans (``GET /v1/tenant/hmac-key``) precisely so old and new hashes can coexist, and nothing
      yet reads that column at query time. Shipping a rotation button before that read path exists
      would silently break account attribution, so this PR ships none.

      What it does ship is a design a rotation can land on without a schema change or a migration of
      existing references:

      * The reference names a STAGING LABEL (``v1``), not an immutable version id, and ``mint``
        attaches that label to the version it creates. So ``material`` asks for the version labelled
        ``v1`` forever, and an operator (or AWS managed rotation) moving ``AWSCURRENT`` onto a new
        version cannot silently swap the bytes under hashes that already exist. Pinning
        ``AWSCURRENT`` instead would have made rotation a data-corruption event; pinning an immutable
        ``VersionId`` would have worked too but would have put an opaque 36-character id in the
        column and made the human-readable ``vN`` selector a lie.
      * A future rotation is then: ``put_secret_value`` on the SAME secret, label the new version
        ``v2``, and update that tenant's ``hash_salt_kek_ref`` to ``.../v2``. That is one UPDATE of
        an existing column, still inside the CHECK, and ``v1`` keeps resolving for historical hashes.

    FAILURE IS HONEST, NEVER A FALLBACK.
      Every AWS failure becomes :class:`KeyProviderUnavailableError`, which the provisioner lets
      propagate WITHOUT writing a tenant row: a tenant that cannot hash is never created, and a
      failed provision emits nothing rather than a locally-derived key that would look valid.
      ``material`` distinguishes the two honest outcomes: a secret (or label) that genuinely does not
      exist raises ``KeyError``, which the HMAC bootstrap turns into a 404 and the SDK degrades on;
      anything else (unreachable endpoint, ``AccessDeniedException``, a throttle) raises
      ``KeyProviderUnavailableError``, which is a 503, because "we could not ask" is not "there is
      nothing there".

    TESTABILITY WITHOUT AWS.
      The ``client`` seam follows :class:`gateway.replay_store.S3ReplayBlobStore` exactly: pass a
      client and it is used as-is; pass none and ``boto3`` is lazily imported and constructed with no
      explicit credentials, so it resolves the AWS default chain (ECS task role / IRSA / instance
      profile / env), and no raw credential is ever handled here. The tests inject a fake covering
      the four calls this class makes, so the suite needs neither ``boto3`` nor an AWS account.
    """

    __slots__ = ("_client", "_name_prefix", "_kms_key_id", "_region")

    def __init__(
        self,
        client: object | None = None,
        *,
        name_prefix: str = "ai-tally/tenant-hmac/",
        kms_key_id: str = "",
        region: str = "",
    ) -> None:
        if client is None:
            client = self._build_client(region)
        self._client = client
        self._name_prefix = name_prefix
        self._kms_key_id = kms_key_id
        self._region = region

    @staticmethod
    def _build_client(region: str) -> object:
        """Construct a real Secrets Manager client, or fail with a message an operator can act on."""
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the install's extras
            raise ProvisionError(
                "TALLY_HMAC_KEY_PROVIDER=kms needs boto3: install the gateway's [secrets] extra, "
                "or inject a client. Falling back to the local dev provider is not an option: its "
                "material is derived from a config root secret, not from the tenant's own key."
            ) from exc
        try:
            # No explicit credentials: boto3 resolves the ECS task role / IRSA / instance profile /
            # environment, in its normal order. A missing region is a real misconfiguration and
            # boto3 says so, at boot, which is where we want it.
            return boto3.client("secretsmanager", region_name=region or None)
        except Exception as exc:  # noqa: BLE001 - normalise botocore's NoRegionError and friends
            raise ProvisionError(
                f"could not construct a Secrets Manager client: {exc}. Set AWS_REGION (or "
                "TALLY_HMAC_SECRETS_REGION) on the gateway task."
            ) from exc

    # --- reference parsing ---------------------------------------------------------------------

    @staticmethod
    def _split_ref(ref: str) -> tuple[str, str]:
        """Split ``<secret-arn>/<vN>`` into its two halves, rejecting anything else.

        The ARN itself contains slashes (the secret name does), so this splits on the LAST one and
        insists the tail is a ``vN`` selector. A reference we cannot parse is never guessed at.
        """
        secret_id, _, version = ref.rpartition("/")
        if not secret_id or not _VERSION_SELECTOR.match(version):
            raise KeyProviderUnavailableError(
                f"malformed Secrets Manager key reference {ref!r}: expected "
                "'<secret-arn>/v<n>'"
            )
        return secret_id, version

    # --- KeyMaterialProvider -------------------------------------------------------------------

    def mint(self) -> str:
        """Create a per-tenant secret holding 32 random bytes and return its versioned reference."""
        name = f"{self._name_prefix}{uuid.uuid4()}"
        material = secrets.token_bytes(HMAC_KEY_BYTES)
        create_kwargs: dict[str, object] = {
            "Name": name,
            # SecretBinary, not SecretString: the key is bytes, and a base64 SecretString would make
            # "is this value encoded or raw" a guess on the read path. Guessing is what we do not do.
            "SecretBinary": material,
            "Description": "ai-tally per-tenant HMAC key set (identifiers-by-hash). Do not rotate "
            "with AWS managed rotation: see gateway/tenant_provisioning.py.",
            "Tags": [
                {"Key": "app", "Value": "ai-tally"},
                {"Key": "purpose", "Value": "tenant-hmac"},
            ],
        }
        if self._kms_key_id:
            # Optional customer-managed key. Absent, Secrets Manager uses the AWS-managed
            # aws/secretsmanager key, which still encrypts at rest.
            create_kwargs["KmsKeyId"] = self._kms_key_id

        created = self._call("create_secret", **create_kwargs)
        arn = str(created.get("ARN") or "")
        version_id = str(created.get("VersionId") or "")
        if not arn or not version_id:
            raise KeyProviderUnavailableError(
                "Secrets Manager create_secret returned no ARN/VersionId; refusing to store a "
                "reference we cannot resolve"
            )
        try:
            # Label the version we just wrote so the reference pins THIS material rather than
            # whatever AWSCURRENT happens to be later. See the rotation note in the class docstring.
            self._call(
                "update_secret_version_stage",
                SecretId=arn,
                VersionStage=_FIRST_VERSION,
                MoveToVersionId=version_id,
            )
            return assert_reference_fits(f"{arn}/{_FIRST_VERSION}")
        except Exception:
            # A secret whose version carries no label can never be resolved, so it is orphaned key
            # material the moment we return. Remove it before re-raising rather than leaving it to
            # bill and to confuse an auditor.
            self._delete_secret_quietly(arn)
            raise

    def delete(self, ref: str) -> None:
        """Delete the secret behind ``ref``.

        Called only for orphan cleanup: the loser of a provisioning race, or a failure after the
        mint. In both cases no tenant row references this material, so it is deleted without a
        recovery window; leaving it in Secrets Manager's 7-to-30-day window would bill for a secret
        nothing can ever use and would leave key material behind, which the module docstring's
        no-orphaned-key invariant exists to prevent.
        """
        secret_id, _ = self._split_ref(ref)
        self._call(
            "delete_secret", SecretId=secret_id, ForceDeleteWithoutRecovery=True
        )

    def material(self, ref: str) -> bytes:
        """Fetch the key bytes the reference's version selector names.

        ``KeyError`` when the secret or that labelled version genuinely does not exist (an honest
        404 on the bootstrap endpoint); :class:`KeyProviderUnavailableError` for every other failure.
        """
        secret_id, version = self._split_ref(ref)
        response = self._call("get_secret_value", SecretId=secret_id, VersionStage=version)
        blob = response.get("SecretBinary")
        if blob is None:
            # A SecretString means somebody created this secret by hand in a shape we did not write.
            # We do not try to interpret it (base64? utf-8? raw?), because a wrong guess yields
            # hashes that look fine and are wrong for every account in that tenant.
            raise KeyProviderUnavailableError(
                f"secret {secret_id} holds a SecretString; ai-tally writes SecretBinary. Recreate "
                "it with `aws secretsmanager put-secret-value --secret-binary fileb://key.bin`."
            )
        material = bytes(blob)
        if len(material) < HMAC_KEY_BYTES:
            raise KeyProviderUnavailableError(
                f"secret {secret_id} holds {len(material)} bytes; an ai-tally HMAC key set is "
                f"{HMAC_KEY_BYTES}. Refusing to hash under a short key."
            )
        return material

    # --- AWS error translation -----------------------------------------------------------------

    def _call(self, operation: str, **kwargs: object) -> dict:
        """Invoke one boto3 operation, translating every failure into our own two error kinds."""
        method = getattr(self._client, operation, None)
        if method is None:
            raise KeyProviderUnavailableError(
                f"the wired Secrets Manager client has no {operation!r} operation"
            )
        try:
            result = method(**kwargs)
        except Exception as exc:  # noqa: BLE001 - botocore is optional; classify by shape
            if _is_aws_not_found(exc):
                # Genuinely absent, which is a different answer from "we could not ask".
                raise KeyError(f"{operation}: {_aws_error_code(exc) or exc}") from exc
            raise KeyProviderUnavailableError(
                f"Secrets Manager {operation} failed ({_aws_error_code(exc) or type(exc).__name__}): "
                f"{exc}"
            ) from exc
        return result if isinstance(result, dict) else {}

    def _delete_secret_quietly(self, secret_id: str) -> None:
        """Best-effort cleanup on a partially-created secret. Never masks the original failure."""
        try:
            self._call("delete_secret", SecretId=secret_id, ForceDeleteWithoutRecovery=True)
        except Exception as exc:  # noqa: BLE001 - the caller is already raising something better
            logger.error(
                "orphaned HMAC secret %s could not be deleted (%s): delete it by hand",
                secret_id,
                exc,
            )


def _aws_error_code(exc: BaseException) -> str:
    """Pull ``response['Error']['Code']`` off a botocore ClientError without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "")
    return ""


def _is_aws_not_found(exc: BaseException) -> bool:
    """Whether an AWS failure means "no such secret / no such version", rather than "we failed"."""
    return _aws_error_code(exc) == "ResourceNotFoundException"


def build_key_provider(settings: Settings) -> KeyMaterialProvider:
    """Select the HMAC key-material provider from settings (Initiative 2 §3.2 review).

    ``local`` (default) returns the restart-durable dev provider; ``kms`` / ``secret-manager``
    returns the AWS Secrets Manager provider (CTO-336). An unknown value fails fast rather than
    guessing, so a typo in a prod deployment cannot silently drop to dev-derived material.
    """
    choice = (getattr(settings, "hmac_key_provider", "local") or "local").strip().lower()
    if choice == "local":
        root = getattr(settings, "hmac_local_root_secret", "") or ""
        return LocalDevKeyProvider(root_secret=root.encode("utf-8"))
    if choice in ("kms", "secret-manager", "secretmanager"):
        return SecretManagerKeyProvider(
            name_prefix=getattr(settings, "hmac_secrets_name_prefix", "")
            or "ai-tally/tenant-hmac/",
            kms_key_id=getattr(settings, "hmac_secrets_kms_key_id", "") or "",
            region=getattr(settings, "hmac_secrets_region", "") or "",
        )
    raise ProvisionError(f"unknown hmac_key_provider {choice!r} (expected 'local' or 'kms')")


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """The outcome of a provision call."""

    tenant_id: str
    plan: str
    #: True when this call inserted the tenant; False on a redelivery or a lost race (idempotent).
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {"tenant_id": self.tenant_id, "plan": self.plan, "created": self.created}


def _clean(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProvisionError(f"{field_name} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ProvisionError(f"{field_name} must be non-empty")
    return trimmed


class TenantProvisioner:
    """Turns a Clerk org into a tenant, idempotently, with its own HMAC key reference.

    See the module docstring for the two invariants. The store owns the DSN and the key provider so
    a test can inject a fake provider and assert orphan cleanup without a real Secret Manager.
    """

    def __init__(
        self, settings: Settings, key_provider: KeyMaterialProvider | None = None
    ) -> None:
        self._dsn = settings.postgres_dsn
        self._keys: KeyMaterialProvider = key_provider or LocalDevKeyProvider()

    def _delete_key_quietly(self, ref: str) -> None:
        """Orphan-cleanup delete that logs instead of raising (CTO-336).

        Cleanup runs on paths where the outcome is already decided: the race is lost, or the insert
        already failed. A cloud provider's delete can itself fail (a throttle, a permission gap), and
        turning that into the caller's exception would replace a correct answer with a 500, or mask
        the real error. The orphan is recorded at ERROR with its reference so it can be deleted by
        hand, which is the honest handling: we say what we could not clean up rather than pretend.
        """
        try:
            self._keys.delete(ref)
        except Exception as exc:  # noqa: BLE001 - cleanup must not change the caller's outcome
            logger.error(
                "orphaned HMAC key reference %s could not be deleted (%s): delete it by hand", ref, exc
            )

    def provision(
        self, *, clerk_org_id: object, name: object, region: str = DEFAULT_REGION
    ) -> ProvisionResult:
        clerk_org_id = _clean(clerk_org_id, field_name="clerk_org_id")
        name = _clean(name, field_name="name")

        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            # Fast path: an existing mapping means a redelivery. Return it and mint NOTHING, so a
            # retried webhook never rolls the tenant's HMAC key set.
            cur.execute(
                "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
            )
            row = cur.fetchone()
            if row is not None:
                return ProvisionResult(str(row[0]), str(row[1]), created=False)

            # Miss: mint the per-org HMAC key set BEFORE inserting. A mint failure raises and no
            # tenant row is written, so a tenant that cannot hash is never created (§4.1).
            kek_ref = self._keys.mint()
            try:
                # Ensure the free plan tier exists before usage_limits references it. The seed also
                # inserts it, but provision can run first on a fresh volume.
                cur.execute(
                    """
                    INSERT INTO plan_tiers (name, max_traces_per_month, max_features, price_micro_usd)
                    VALUES ('free', 1000000, 10, 0)
                    ON CONFLICT (name) DO NOTHING
                    """
                )
                # Race-safe insert: two concurrent first-deliveries both miss the SELECT above, and
                # the partial-unique arbiter lets exactly one win. The predicate is repeated so
                # Postgres infers uq_tenants_clerk_org_id.
                cur.execute(
                    """
                    INSERT INTO tenants (name, region, plan, hash_salt_kek_ref, clerk_org_id)
                    VALUES (%s, %s, 'free', %s, %s)
                    ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS NOT NULL
                    DO NOTHING
                    RETURNING id
                    """,
                    (name, region, kek_ref, clerk_org_id),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    tenant_id = str(inserted[0])
                    cur.execute(
                        """
                        INSERT INTO usage_limits (tenant_id, plan) VALUES (%s, 'free')
                        ON CONFLICT (tenant_id) DO NOTHING
                        """,
                        (tenant_id,),
                    )
                    conn.commit()
                    return ProvisionResult(tenant_id, "free", created=True)

                # Lost the race: a concurrent delivery won. Adopt its tenant and delete the key set
                # we just minted, so no orphaned material survives.
                conn.rollback()
                cur.execute(
                    "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
                )
                winner = cur.fetchone()
                self._delete_key_quietly(kek_ref)
                if winner is None:
                    # Extremely unlikely: the conflicting row vanished between the failed insert and
                    # this read. Surface it rather than fabricate a tenant id.
                    raise ProvisionError(
                        "provision lost a race but the winning tenant could not be read"
                    )
                return ProvisionResult(str(winner[0]), str(winner[1]), created=False)
            except Exception:
                # Any failure after minting must not leak the key set.
                conn.rollback()
                self._delete_key_quietly(kek_ref)
                raise

    def tenant_for_clerk_org(self, clerk_org_id: object) -> ProvisionResult | None:
        """Resolve a Clerk org id to ``{tenant_id, plan}`` without provisioning. ``None`` if absent."""
        clerk_org_id = _clean(clerk_org_id, field_name="org_id")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, plan FROM tenants WHERE clerk_org_id = %s", (clerk_org_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ProvisionResult(str(row[0]), str(row[1]), created=False)
