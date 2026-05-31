"""Zero-touch tenant bootstrap for self-serve signup (CTO-88 / spec §15).

Self-serve GTM needs a new customer to go from "click signup" to "send my first trace" with **zero
manual steps**. That means the moment a :class:`SignupRequest` arrives we must deterministically
plan every control-plane row the tenant needs — the ``tenants`` row, a scoped ``api_keys`` row, the
per-tenant HMAC key set (CTO-74), and a sane default config — onto the shared cluster, in the
region the customer picked for data residency (CTO-76).

This module is the **pure planner** for that: given a signup request it produces a
:class:`TenantBootstrapPlan` (the exact rows to INSERT, matching ``db/postgres/0001_control_plane``)
plus a one-time :class:`ApiKeyIssue`. Performing the INSERTs / KMS calls is the infra layer's job;
keeping the decision logic here makes it unit-testable with no Postgres, no KMS, and no network.

Security invariants enforced structurally (never just by convention):

* The **raw API key is returned exactly once** (in :class:`ApiKeyIssue`) and is *never* stored in
  the plan — the plan carries only its SHA-256 hash, mirroring the ``api_keys.key_hash`` column.
* The **HMAC key set is a KMS reference** (``hash_salt_kek_ref``), never raw key material, and is
  checked against the same ``no_raw_secret`` shape the DDL's CHECK constraint enforces.
* :meth:`TenantBootstrapPlan.assert_no_raw_secret` is a belt-and-suspenders guard so a refactor
  can't accidentally leak a secret into a serialized plan.

Idempotency is first-class: :class:`TenantRegistry` keys provisioning on a stable
``idempotency_key`` derived from the normalized org + admin email, so a double-clicked signup (or a
retried request) returns the *same* tenant instead of double-provisioning.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

#: Default plan a self-serve tenant lands on (matches ``tenants.plan`` default; see CTO-89).
DEFAULT_PLAN = "free"

#: Human-visible prefix on every issued key. The body is high-entropy and never reconstructable
#: from anything we persist.
API_KEY_PREFIX = "tk_live_"

#: Bytes of entropy in the random portion of an API key. 32 bytes → 256 bits.
API_KEY_ENTROPY_BYTES = 32

#: Default analytics sample rate for a fresh tenant (billing counts head traces *before* sampling,
#: so this only affects analytics fidelity, never the bill — see CTO-87).
DEFAULT_SAMPLE_RATE = 1.0

#: Default guardrail posture for a brand-new tenant: watch, never block (CTO-51/58).
DEFAULT_GUARDRAIL_MODE = "observe"


def _utc(value: datetime | None) -> datetime:
    """Coerce to an aware UTC datetime; default to now()."""
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Region / scope enums
# --------------------------------------------------------------------------------------


class Region(str, Enum):
    """A data-residency region chosen at signup (CTO-76). Value matches ``tenants.region``."""

    US_EAST = "us-east"
    EU_WEST = "eu-west"
    AP_SOUTH = "ap-south"

    @property
    def residency(self) -> str:
        """Coarse legal residency bucket, for "where does this tenant's data live" copy."""
        return {
            Region.US_EAST: "US",
            Region.EU_WEST: "EU",
            Region.AP_SOUTH: "IN",
        }[self]

    @property
    def ingest_host(self) -> str:
        """Region-pinned ingest endpoint a fresh tenant points its proxy/SDK at."""
        return f"ingest.{self.value}.ai-tally.dev"


class Scope(str, Enum):
    """API-key scope. Value matches the ``api_keys.scope`` CHECK constraint."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


# --------------------------------------------------------------------------------------
# Signup request
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignupRequest:
    """An inbound self-serve signup. Validated on construction so a bad request never provisions."""

    org_name: str
    admin_email: str
    region: Region

    def __post_init__(self) -> None:
        if not self.org_name or not self.org_name.strip():
            raise ValueError("org_name must be non-empty")
        if len(self.org_name) > 200:
            raise ValueError("org_name too long (max 200)")
        email = (self.admin_email or "").strip()
        # Deliberately permissive: one '@' with non-empty local + domain, and a dot in the domain.
        # We gate provisioning, not RFC 5322 — over-strict validation rejects real users.
        if email.count("@") != 1:
            raise ValueError("admin_email must contain exactly one '@'")
        local, _, domain = email.partition("@")
        if not local or not domain or "." not in domain:
            raise ValueError("admin_email is not a valid address")
        if not isinstance(self.region, Region):
            raise ValueError("region must be a Region")

    @property
    def normalized_email(self) -> str:
        """Lowercased, trimmed email — the identity we dedup signups on."""
        return self.admin_email.strip().lower()

    @property
    def normalized_org(self) -> str:
        return " ".join(self.org_name.split()).lower()

    @property
    def idempotency_key(self) -> str:
        """Stable key for "is this the same signup". Same org + email + region → same key."""
        material = f"{self.normalized_org}\x1f{self.normalized_email}\x1f{self.region.value}"
        return _sha256_hex(material)


# --------------------------------------------------------------------------------------
# Issued secrets (returned once) vs. stored rows (no secrets)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiKeyIssue:
    """The one-time secret handed back to the customer. NEVER persisted as-is.

    Only :attr:`key_hash` (which mirrors ``api_keys.key_hash``) is stored. :attr:`raw` exists solely
    to show the customer their key once at signup; drop it immediately after.
    """

    raw: str
    key_hash: str
    display_prefix: str
    scope: Scope

    @staticmethod
    def mint(scope: Scope = Scope.WRITE, *, token: str | None = None) -> ApiKeyIssue:
        """Mint a fresh key. ``token`` is injectable for deterministic tests; otherwise CSPRNG."""
        body = token if token is not None else secrets.token_urlsafe(API_KEY_ENTROPY_BYTES)
        raw = f"{API_KEY_PREFIX}{body}"
        return ApiKeyIssue(
            raw=raw,
            key_hash=_sha256_hex(raw),
            # Enough to recognize the key in a list UI, not enough to reconstruct it.
            display_prefix=raw[: len(API_KEY_PREFIX) + 4],
            scope=scope,
        )


@dataclass(frozen=True, slots=True)
class HmacKeyRef:
    """A KMS reference to the per-tenant HMAC key set (CTO-74). Holds NO raw key material."""

    kek_ref: str
    version: int = 1

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not self.kek_ref:
            raise ValueError("kek_ref must be non-empty")
        # Same guard as the DDL's no_raw_secret CHECK: a KMS ref is short and never an obvious key.
        if self.kek_ref.startswith("sk-") or len(self.kek_ref) >= 512:
            raise ValueError("kek_ref looks like raw secret material, not a KMS reference")

    @staticmethod
    def for_tenant(tenant_id: str, version: int = 1) -> HmacKeyRef:
        return HmacKeyRef(kek_ref=f"kms://tenant/{tenant_id}/hmac/v{version}", version=version)


@dataclass(frozen=True, slots=True)
class TenantRow:
    """The ``tenants`` row to INSERT. Mirrors db/postgres/0001_control_plane.sql."""

    id: str
    name: str
    region: str
    plan: str
    hash_salt_kek_ref: str
    created_at: datetime

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "region": self.region,
            "plan": self.plan,
            "hash_salt_kek_ref": self.hash_salt_kek_ref,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ApiKeyRow:
    """The ``api_keys`` row to INSERT — hash only, never the token."""

    tenant_id: str
    key_hash: str
    scope: str
    created_at: datetime

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "key_hash": self.key_hash,
            "scope": self.scope,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------------------
# The bootstrap plan
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TenantBootstrapPlan:
    """Everything needed to provision a tenant zero-touch. Contains NO raw secrets.

    The infra layer applies :attr:`tenant_row` + :attr:`api_key_row` as INSERTs and resolves
    :attr:`hmac_key` against KMS. The matching raw API key travels separately in
    :class:`ProvisionResult.api_key` and is shown once.
    """

    tenant_id: str
    region: Region
    tenant_row: TenantRow
    api_key_row: ApiKeyRow
    hmac_key: HmacKeyRef
    default_config: dict
    idempotency_key: str
    created_at: datetime

    def assert_no_raw_secret(self) -> None:
        """Guard: a serialized plan must never carry raw key material. Raises if it does."""
        for value in (self.tenant_row.hash_salt_kek_ref, self.hmac_key.kek_ref):
            if value.startswith(API_KEY_PREFIX) or value.startswith("sk-"):
                raise ValueError(f"plan leaks raw secret material: {value!r}")
        # The api_key_row must hold a hash, not a token. A token would carry the visible prefix.
        if self.api_key_row.key_hash.startswith(API_KEY_PREFIX):
            raise ValueError("api_key_row.key_hash holds a raw token, not a hash")

    def ingest_credentials(self) -> dict:
        """The minimal config a fresh tenant points its proxy/SDK at to send its first trace.

        Note: this returns the *key hash* and host, not the raw key — the raw key is delivered once
        via :class:`ProvisionResult`. Callers compose the env from that.
        """
        return {
            "tenant_id": self.tenant_id,
            "ingest_host": self.region.ingest_host,
            "region": self.region.value,
            "key_scope": self.api_key_row.scope,
        }

    def as_dict(self) -> dict:
        self.assert_no_raw_secret()
        return {
            "tenant_id": self.tenant_id,
            "region": self.region.value,
            "tenant_row": self.tenant_row.as_dict(),
            "api_key_row": self.api_key_row.as_dict(),
            "hmac_key": {"kek_ref": self.hmac_key.kek_ref, "version": self.hmac_key.version},
            "default_config": dict(self.default_config),
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Output of provisioning: the storable plan + the one-time raw key + whether it was new."""

    plan: TenantBootstrapPlan
    api_key: ApiKeyIssue
    reused: bool = False

    @property
    def first_trace_env(self) -> dict:
        """Ready-to-paste env so the tenant can send its first trace immediately (AC: zero steps).

        This is the only place the raw key surfaces. It is intentionally not stored anywhere.
        """
        return {
            "OPENAI_BASE_URL": f"https://{self.plan.region.ingest_host}/v1",
            "TALLY_TENANT_KEY": self.api_key.raw,
        }


# --------------------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------------------


def _default_config(region: Region) -> dict:
    return {
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "guardrail_mode": DEFAULT_GUARDRAIL_MODE,
        "region": region.value,
        "residency": region.residency,
    }


def provision_tenant(
    request: SignupRequest,
    *,
    now: datetime | None = None,
    tenant_id: str | None = None,
    api_token: str | None = None,
    scope: Scope = Scope.WRITE,
) -> ProvisionResult:
    """Plan a brand-new tenant from a validated signup request. Pure: no I/O, no global state.

    ``tenant_id`` / ``api_token`` are injectable for deterministic tests; in production both default
    to fresh high-entropy values. The result's :attr:`ProvisionResult.plan` is safe to serialize and
    hand to the infra layer; the raw key lives only on :attr:`ProvisionResult.api_key`.
    """
    created_at = _utc(now)
    tid = tenant_id if tenant_id is not None else f"t_{secrets.token_hex(8)}"

    hmac_key = HmacKeyRef.for_tenant(tid, version=1)
    issue = ApiKeyIssue.mint(scope, token=api_token)

    tenant_row = TenantRow(
        id=tid,
        name=request.org_name.strip(),
        region=request.region.value,
        plan=DEFAULT_PLAN,
        hash_salt_kek_ref=hmac_key.kek_ref,
        created_at=created_at,
    )
    api_key_row = ApiKeyRow(
        tenant_id=tid,
        key_hash=issue.key_hash,
        scope=issue.scope.value,
        created_at=created_at,
    )
    plan = TenantBootstrapPlan(
        tenant_id=tid,
        region=request.region,
        tenant_row=tenant_row,
        api_key_row=api_key_row,
        hmac_key=hmac_key,
        default_config=_default_config(request.region),
        idempotency_key=request.idempotency_key,
        created_at=created_at,
    )
    plan.assert_no_raw_secret()
    return ProvisionResult(plan=plan, api_key=issue, reused=False)


# --------------------------------------------------------------------------------------
# Idempotent registry + isolation verification
# --------------------------------------------------------------------------------------


@dataclass
class TenantRegistry:
    """In-memory, idempotent provisioning front-end.

    Stands in for the unique constraint the real control plane enforces: provisioning the *same*
    signup twice (double-click, client retry) returns the original tenant rather than creating a
    second one. The raw key is only returned on the first call — a reused result has no key to show,
    because the original was already delivered once and is not stored.
    """

    _by_idempotency: dict[str, TenantBootstrapPlan] = field(default_factory=dict)
    _ids: set[str] = field(default_factory=set)

    def provision(
        self,
        request: SignupRequest,
        *,
        now: datetime | None = None,
        tenant_id: str | None = None,
        api_token: str | None = None,
        scope: Scope = Scope.WRITE,
    ) -> ProvisionResult:
        existing = self._by_idempotency.get(request.idempotency_key)
        if existing is not None:
            # Idempotent replay: no new key minted, no second tenant created.
            return ProvisionResult(plan=existing, api_key=_REDACTED_KEY, reused=True)

        result = provision_tenant(
            request, now=now, tenant_id=tenant_id, api_token=api_token, scope=scope
        )
        if result.plan.tenant_id in self._ids:
            raise ValueError(f"tenant id collision: {result.plan.tenant_id}")
        self._by_idempotency[request.idempotency_key] = result.plan
        self._ids.add(result.plan.tenant_id)
        return result

    def __len__(self) -> int:
        return len(self._by_idempotency)


# Sentinel returned when a registry replay has no fresh key to surface (the original was delivered
# once at first provision and is never stored).
_REDACTED_KEY = ApiKeyIssue(raw="", key_hash="", display_prefix="", scope=Scope.WRITE)


def verify_isolation(plans: list[TenantBootstrapPlan]) -> list[str]:
    """Verify freshly-provisioned tenants share no isolation-critical material (AC).

    Returns a list of human-readable violations; empty means isolation holds. We check that across
    every pair of tenants the tenant id, API-key hash, and HMAC KMS reference are all distinct —
    the three things that, if shared, would let one tenant read or be billed for another's data.
    """
    violations: list[str] = []
    seen_ids: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}
    seen_keks: dict[str, int] = {}

    for plan in plans:
        if plan.tenant_id in seen_ids:
            violations.append(f"duplicate tenant_id {plan.tenant_id!r}")
        seen_ids[plan.tenant_id] = seen_ids.get(plan.tenant_id, 0) + 1

        kh = plan.api_key_row.key_hash
        if kh in seen_hashes:
            violations.append(f"shared api key hash across tenants ({plan.tenant_id!r})")
        seen_hashes[kh] = 1

        kek = plan.hmac_key.kek_ref
        if kek in seen_keks:
            violations.append(f"shared HMAC kek_ref across tenants ({plan.tenant_id!r})")
        seen_keks[kek] = 1

        # Each tenant's HMAC ref must be namespaced under its own id — a cross-namespaced ref would
        # mean two tenants could resolve to the same key.
        if f"/tenant/{plan.tenant_id}/" not in kek:
            violations.append(f"HMAC kek_ref not namespaced to tenant {plan.tenant_id!r}")

    return violations
