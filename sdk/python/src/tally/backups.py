"""Backup & disaster-recovery policy logic — schedule, restore-drill, IR runbook (CTO-77).

Durability is a promise we make about *other people's data*. This module is the pure-logic layer
that encodes that promise so it can be tested without standing up real backup infra (spec §12):

* :class:`BackupPolicy` — what gets backed up, how often, how long it's retained, and that it lands
  in a *different* region (cross-region is the point — a region loss must not lose the backup).
* :class:`BackupSchedule` — given a policy and a clock, when the next backup is due and whether the
  most recent one is overdue (a silently-stalled backup is the classic DR failure).
* :class:`RestoreDrill` — the actual test of the promise: take a backup, restore it, measure the
  RTO (time-to-restore) and RPO (data-loss window) and check them against the contractual targets
  (4h RTO / 1h RPO, spec §12.2). A backup you've never restored is a hope, not a backup.
* :class:`Environment` / :func:`synthetic_record` — dev/staging/prod isolation and synthetic-data
  generation so non-prod environments never hold real customer data.
* :class:`IncidentClass` / :func:`runbook_steps` — the incident-response runbook keyed by incident
  class, so the on-call has a documented path per failure mode.

The cluster-side backup execution (ClickHouse ``BACKUP``, Postgres ``pg_dump``/WAL archiving,
cross-region object replication), the scheduler that fires these, the annual tabletop exercise, the
public subprocessor list / pentest / VDP — all infra and ops follow-ups. This module owns the
*policy and the verification logic*: the targets, the overdue math, the pass/fail of a drill.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

# Contractual disaster-recovery targets (spec §12.2).
DEFAULT_RTO = timedelta(hours=4)  # max acceptable time to restore service
DEFAULT_RPO = timedelta(hours=1)  # max acceptable data-loss window

DEFAULT_BACKUP_INTERVAL = timedelta(days=1)  # daily backups
DEFAULT_RETENTION_DAYS = 35  # keep ~5 weeks of daily backups


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC (naive is assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------------------------- #
# What we back up
# --------------------------------------------------------------------------------------------- #
class BackupTarget(str, Enum):
    """The stores that must be backed up (each has its own native backup mechanism)."""

    CLICKHOUSE = "clickhouse"  # span telemetry store
    POSTGRES = "postgres"  # control-plane / metadata


# --------------------------------------------------------------------------------------------- #
# Backup policy
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """How a single store is backed up: cadence, retention, encryption, and where it lands.

    Cross-region is mandatory: ``destination_region`` must differ from ``source_region`` so the loss
    of the source region cannot also lose its backups. Backups are always encrypted at rest.
    """

    target: BackupTarget
    source_region: str
    destination_region: str
    interval: timedelta = DEFAULT_BACKUP_INTERVAL
    retention_days: int = DEFAULT_RETENTION_DAYS
    encrypted: bool = True

    def __post_init__(self) -> None:
        if not self.source_region:
            raise ValueError("source_region must be non-empty")
        if not self.destination_region:
            raise ValueError("destination_region must be non-empty")
        if self.destination_region == self.source_region:
            raise ValueError("destination_region must differ from source_region (cross-region)")
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        if isinstance(self.retention_days, bool) or not isinstance(self.retention_days, int):
            raise ValueError("retention_days must be an int")
        if self.retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if not self.encrypted:
            raise ValueError("backups must be encrypted at rest")

    @property
    def retention(self) -> timedelta:
        return timedelta(days=self.retention_days)

    def expires_at(self, taken_at: datetime) -> datetime:
        """When a backup taken at ``taken_at`` ages out of retention."""
        return _as_utc(taken_at) + self.retention

    def is_expired(self, taken_at: datetime, *, as_of: datetime) -> bool:
        return _as_utc(as_of) >= self.expires_at(taken_at)

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "source_region": self.source_region,
            "destination_region": self.destination_region,
            "interval_seconds": int(self.interval.total_seconds()),
            "retention_days": self.retention_days,
            "encrypted": self.encrypted,
        }


# --------------------------------------------------------------------------------------------- #
# Schedule / overdue detection
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BackupSchedule:
    """Pairs a policy with the timestamp of the last successful backup to answer 'are we current?'.

    A backup that silently stops firing is the classic DR failure mode — the data looks safe right
    up until you need it. ``is_overdue`` makes the stall observable.
    """

    policy: BackupPolicy
    last_backup_at: datetime | None = None

    def next_due_at(self) -> datetime | None:
        """When the next backup should run, or ``None`` if none has ever run (run immediately)."""
        if self.last_backup_at is None:
            return None
        return _as_utc(self.last_backup_at) + self.policy.interval

    def is_overdue(self, *, as_of: datetime, grace: timedelta = timedelta(0)) -> bool:
        """True if no backup has run, or the next one is past due (beyond an optional grace)."""
        if grace < timedelta(0):
            raise ValueError("grace must be non-negative")
        due = self.next_due_at()
        if due is None:
            return True  # never backed up -> overdue by definition
        return _as_utc(as_of) > due + grace

    def age(self, *, as_of: datetime) -> timedelta | None:
        """How long since the last backup, or ``None`` if none has run."""
        if self.last_backup_at is None:
            return None
        return _as_utc(as_of) - _as_utc(self.last_backup_at)


# --------------------------------------------------------------------------------------------- #
# Restore-drill verification (the actual test of the promise)
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RestoreDrill:
    """A single restore exercise and its measured outcome against the RTO/RPO targets.

    * achieved **RTO** = ``restore_completed_at - restore_started_at`` (how long recovery took).
    * achieved **RPO** = ``restore_started_at - backup_taken_at`` (the data-loss window — everything
      written after the backup snapshot is lost on restore).

    The drill *passes* only if both achieved values are within their targets. A failed drill is a
    finding, not an error — it's exactly what the exercise exists to surface.
    """

    target: BackupTarget
    backup_taken_at: datetime
    restore_started_at: datetime
    restore_completed_at: datetime
    rto_target: timedelta = DEFAULT_RTO
    rpo_target: timedelta = DEFAULT_RPO
    restored_rows: int = 0

    def __post_init__(self) -> None:
        started = _as_utc(self.restore_started_at)
        completed = _as_utc(self.restore_completed_at)
        taken = _as_utc(self.backup_taken_at)
        if completed < started:
            raise ValueError("restore_completed_at must be >= restore_started_at")
        if started < taken:
            raise ValueError("restore_started_at must be >= backup_taken_at")
        if self.rto_target <= timedelta(0) or self.rpo_target <= timedelta(0):
            raise ValueError("rto_target and rpo_target must be positive")
        if isinstance(self.restored_rows, bool) or not isinstance(self.restored_rows, int):
            raise ValueError("restored_rows must be an int")
        if self.restored_rows < 0:
            raise ValueError("restored_rows must be non-negative")

    @property
    def achieved_rto(self) -> timedelta:
        return _as_utc(self.restore_completed_at) - _as_utc(self.restore_started_at)

    @property
    def achieved_rpo(self) -> timedelta:
        return _as_utc(self.restore_started_at) - _as_utc(self.backup_taken_at)

    @property
    def rto_met(self) -> bool:
        return self.achieved_rto <= self.rto_target

    @property
    def rpo_met(self) -> bool:
        return self.achieved_rpo <= self.rpo_target

    @property
    def passed(self) -> bool:
        """Drill passes only if it actually restored data AND met both targets."""
        return self.restored_rows > 0 and self.rto_met and self.rpo_met

    def failures(self) -> tuple[str, ...]:
        """Human-readable reasons the drill failed (empty tuple if it passed)."""
        reasons: list[str] = []
        if self.restored_rows <= 0:
            reasons.append("no rows restored")
        if not self.rto_met:
            reasons.append(
                f"RTO breach: {self.achieved_rto} > {self.rto_target}"
            )
        if not self.rpo_met:
            reasons.append(
                f"RPO breach: {self.achieved_rpo} > {self.rpo_target}"
            )
        return tuple(reasons)

    def summary(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "passed": self.passed,
            "achieved_rto_seconds": int(self.achieved_rto.total_seconds()),
            "achieved_rpo_seconds": int(self.achieved_rpo.total_seconds()),
            "rto_target_seconds": int(self.rto_target.total_seconds()),
            "rpo_target_seconds": int(self.rpo_target.total_seconds()),
            "restored_rows": self.restored_rows,
            "failures": list(self.failures()),
        }


# --------------------------------------------------------------------------------------------- #
# Environment separation + synthetic data
# --------------------------------------------------------------------------------------------- #
class Environment(str, Enum):
    """Deployment environments. Only PROD may hold real customer data."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

    @property
    def allows_real_data(self) -> bool:
        return self is Environment.PROD


def assert_no_real_data(environment: Environment) -> None:
    """Guard: raise if a caller is about to load real customer data into a non-prod environment."""
    if not environment.allows_real_data:
        raise ValueError(
            f"environment {environment.value!r} must use synthetic data, not real customer data"
        )


def synthetic_record(environment: Environment, seed: int) -> dict[str, str]:
    """Deterministic synthetic span-ish record for a non-prod environment (never real PII).

    Keyed on ``(environment, seed)`` so test fixtures are reproducible. Refuses to run for PROD —
    synthetic data has no place in production, and producing it there would be a footgun.
    """
    if environment is Environment.PROD:
        raise ValueError("synthetic data must not be generated for prod")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative int")
    digest = hashlib.sha256(f"{environment.value}:{seed}".encode()).hexdigest()
    return {
        "tenant_id": f"synthetic-{environment.value}-{digest[:8]}",
        "user_id_hash": digest,
        "feature_tag": f"synthetic-feature-{seed % 8}",
        "environment": environment.value,
    }


# --------------------------------------------------------------------------------------------- #
# Incident-response runbook
# --------------------------------------------------------------------------------------------- #
class IncidentClass(str, Enum):
    """Incident classes, each with a distinct documented response path."""

    DATA_LOSS = "data_loss"  # backup/restore territory
    DATA_BREACH = "data_breach"  # unauthorized access / exfiltration
    REGION_OUTAGE = "region_outage"  # cloud region unavailable
    INTEGRITY = "integrity"  # data corruption / bad deploy


_RUNBOOK: dict[IncidentClass, tuple[str, ...]] = {
    IncidentClass.DATA_LOSS: (
        "Declare incident; page on-call DRI.",
        "Identify last good backup for the affected store.",
        "Run restore drill against an isolated environment to confirm integrity.",
        "Cut over to restored data; verify RTO/RPO against targets.",
        "Post-incident review; file backup-gap findings.",
    ),
    IncidentClass.DATA_BREACH: (
        "Declare incident; page security DRI; preserve evidence.",
        "Revoke/rotate affected credentials and HMAC keys.",
        "Scope blast radius; identify affected tenants.",
        "Notify per contractual/regulatory SLA.",
        "Post-incident review; remediate access path.",
    ),
    IncidentClass.REGION_OUTAGE: (
        "Declare incident; confirm scope with cloud status.",
        "Fail reads over to cross-region replica/backup.",
        "Throttle writes; queue for replay.",
        "Restore service in healthy region; verify RTO.",
        "Post-incident review; validate cross-region capacity.",
    ),
    IncidentClass.INTEGRITY: (
        "Declare incident; freeze the suspect pipeline/deploy.",
        "Quarantine corrupted partitions; identify last-good snapshot.",
        "Restore affected ranges from backup; reconcile.",
        "Re-enable pipeline behind validation.",
        "Post-incident review; add integrity check.",
    ),
}


def runbook_steps(incident_class: IncidentClass) -> tuple[str, ...]:
    """The documented response steps for an incident class."""
    return _RUNBOOK[incident_class]


def overdue_schedules(
    schedules: Iterable[BackupSchedule], *, as_of: datetime, grace: timedelta = timedelta(0)
) -> tuple[BackupSchedule, ...]:
    """Filter a set of schedules down to those that are overdue — the alerting surface."""
    return tuple(s for s in schedules if s.is_overdue(as_of=as_of, grace=grace))
