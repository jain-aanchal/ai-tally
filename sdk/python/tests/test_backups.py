# SPDX-License-Identifier: Apache-2.0
"""Tests for tally.backups (CTO-77): policy, schedule, restore-drill RTO/RPO, env, runbook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tally.backups import (
    DEFAULT_RPO,
    DEFAULT_RTO,
    BackupPolicy,
    BackupSchedule,
    BackupTarget,
    Environment,
    IncidentClass,
    RestoreDrill,
    assert_no_real_data,
    overdue_schedules,
    runbook_steps,
    synthetic_record,
)

UTC = timezone.utc


def _policy(**kw) -> BackupPolicy:
    base = dict(
        target=BackupTarget.CLICKHOUSE,
        source_region="us-east-1",
        destination_region="us-west-2",
    )
    base.update(kw)
    return BackupPolicy(**base)


# --------------------------------------------------------------------------- #
# BackupPolicy
# --------------------------------------------------------------------------- #
def test_policy_defaults_are_daily_encrypted_cross_region():
    p = _policy()
    assert p.interval == timedelta(days=1)
    assert p.encrypted is True
    assert p.retention_days == 35


def test_policy_requires_cross_region():
    with pytest.raises(ValueError):
        _policy(destination_region="us-east-1")  # same as source


def test_policy_rejects_unencrypted():
    with pytest.raises(ValueError):
        _policy(encrypted=False)


def test_policy_rejects_empty_regions():
    with pytest.raises(ValueError):
        _policy(source_region="")
    with pytest.raises(ValueError):
        _policy(destination_region="")


def test_policy_rejects_bad_retention():
    with pytest.raises(ValueError):
        _policy(retention_days=0)
    with pytest.raises(ValueError):
        _policy(retention_days=True)


def test_policy_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        _policy(interval=timedelta(0))


def test_policy_expiry():
    p = _policy(retention_days=10)
    taken = datetime(2026, 1, 1, tzinfo=UTC)
    assert p.expires_at(taken) == datetime(2026, 1, 11, tzinfo=UTC)
    assert p.is_expired(taken, as_of=datetime(2026, 1, 11, tzinfo=UTC)) is True
    assert p.is_expired(taken, as_of=datetime(2026, 1, 10, tzinfo=UTC)) is False


def test_policy_as_dict():
    d = _policy().as_dict()
    assert d["target"] == "clickhouse"
    assert d["destination_region"] == "us-west-2"
    assert d["encrypted"] is True


# --------------------------------------------------------------------------- #
# BackupSchedule
# --------------------------------------------------------------------------- #
def test_schedule_never_run_is_overdue():
    s = BackupSchedule(_policy())
    assert s.next_due_at() is None
    assert s.is_overdue(as_of=datetime(2026, 1, 1, tzinfo=UTC)) is True
    assert s.age(as_of=datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_schedule_within_interval_not_overdue():
    last = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    s = BackupSchedule(_policy(), last_backup_at=last)
    assert s.next_due_at() == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    assert s.is_overdue(as_of=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)) is False


def test_schedule_past_interval_is_overdue():
    last = datetime(2026, 1, 1, tzinfo=UTC)
    s = BackupSchedule(_policy(), last_backup_at=last)
    assert s.is_overdue(as_of=datetime(2026, 1, 2, 1, 0, tzinfo=UTC)) is True


def test_schedule_grace_window():
    last = datetime(2026, 1, 1, tzinfo=UTC)
    s = BackupSchedule(_policy(), last_backup_at=last)
    # 30min past due but within a 1h grace -> not overdue
    as_of = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    assert s.is_overdue(as_of=as_of, grace=timedelta(hours=1)) is False
    assert s.is_overdue(as_of=as_of) is True


def test_schedule_rejects_negative_grace():
    s = BackupSchedule(_policy(), last_backup_at=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        s.is_overdue(as_of=datetime(2026, 1, 2, tzinfo=UTC), grace=timedelta(seconds=-1))


def test_schedule_age():
    last = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    s = BackupSchedule(_policy(), last_backup_at=last)
    assert s.age(as_of=datetime(2026, 1, 1, 6, 0, tzinfo=UTC)) == timedelta(hours=6)


def test_overdue_schedules_filter():
    last_ok = datetime(2026, 1, 2, tzinfo=UTC)
    last_stale = datetime(2026, 1, 1, tzinfo=UTC)
    ok = BackupSchedule(_policy(), last_backup_at=last_ok)
    stale = BackupSchedule(_policy(target=BackupTarget.POSTGRES), last_backup_at=last_stale)
    never = BackupSchedule(_policy())
    result = overdue_schedules(
        [ok, stale, never], as_of=datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    )
    assert ok not in result
    assert stale in result
    assert never in result


# --------------------------------------------------------------------------- #
# RestoreDrill
# --------------------------------------------------------------------------- #
def test_restore_drill_passes_within_targets():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)  # RPO = 30m (<=1h)
    completed = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)  # RTO = 2h (<=4h)
    d = RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=1000)
    assert d.achieved_rpo == timedelta(minutes=30)
    assert d.achieved_rto == timedelta(hours=2)
    assert d.passed is True
    assert d.failures() == ()


def test_restore_drill_rto_breach():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 5, 10, tzinfo=UTC)  # RTO = 5h > 4h
    d = RestoreDrill(BackupTarget.POSTGRES, taken, started, completed, restored_rows=10)
    assert d.rto_met is False
    assert d.passed is False
    assert any("RTO breach" in f for f in d.failures())


def test_restore_drill_rpo_breach():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)  # RPO = 2h > 1h
    completed = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    d = RestoreDrill(BackupTarget.POSTGRES, taken, started, completed, restored_rows=10)
    assert d.rpo_met is False
    assert d.passed is False
    assert any("RPO breach" in f for f in d.failures())


def test_restore_drill_no_rows_fails():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 0, 20, tzinfo=UTC)
    d = RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=0)
    assert d.passed is False
    assert "no rows restored" in d.failures()


def test_restore_drill_uses_default_targets():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    d = RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=5)
    assert d.rto_target == DEFAULT_RTO == timedelta(hours=4)
    assert d.rpo_target == DEFAULT_RPO == timedelta(hours=1)


def test_restore_drill_rejects_impossible_timeline():
    taken = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    # restore started before backup taken
    with pytest.raises(ValueError):
        RestoreDrill(
            BackupTarget.CLICKHOUSE,
            taken,
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        )
    # completed before started
    with pytest.raises(ValueError):
        RestoreDrill(
            BackupTarget.CLICKHOUSE,
            taken,
            datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        )


def test_restore_drill_rejects_bad_rows():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 0, 20, tzinfo=UTC)
    with pytest.raises(ValueError):
        RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=-1)
    with pytest.raises(ValueError):
        RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=True)


def test_restore_drill_summary():
    taken = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    started = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    completed = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    s = RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=7).summary()
    assert s["passed"] is True
    assert s["achieved_rto_seconds"] == 2 * 3600
    assert s["achieved_rpo_seconds"] == 30 * 60
    assert s["restored_rows"] == 7


def test_restore_drill_naive_datetimes_treated_as_utc():
    taken = datetime(2026, 1, 1, 0, 0)  # naive
    started = datetime(2026, 1, 1, 0, 30)
    completed = datetime(2026, 1, 1, 1, 0)
    d = RestoreDrill(BackupTarget.CLICKHOUSE, taken, started, completed, restored_rows=1)
    assert d.achieved_rpo == timedelta(minutes=30)


# --------------------------------------------------------------------------- #
# Environment / synthetic data
# --------------------------------------------------------------------------- #
def test_only_prod_allows_real_data():
    assert Environment.PROD.allows_real_data is True
    assert Environment.DEV.allows_real_data is False
    assert Environment.STAGING.allows_real_data is False


def test_assert_no_real_data_blocks_nonprod():
    assert_no_real_data(Environment.PROD)  # no raise
    with pytest.raises(ValueError):
        assert_no_real_data(Environment.STAGING)


def test_synthetic_record_is_deterministic():
    a = synthetic_record(Environment.DEV, 1)
    b = synthetic_record(Environment.DEV, 1)
    assert a == b
    assert a["environment"] == "dev"
    assert a != synthetic_record(Environment.DEV, 2)


def test_synthetic_record_refuses_prod():
    with pytest.raises(ValueError):
        synthetic_record(Environment.PROD, 1)


def test_synthetic_record_rejects_bad_seed():
    with pytest.raises(ValueError):
        synthetic_record(Environment.DEV, -1)
    with pytest.raises(ValueError):
        synthetic_record(Environment.DEV, True)


# --------------------------------------------------------------------------- #
# Incident runbook
# --------------------------------------------------------------------------- #
def test_every_incident_class_has_runbook():
    for ic in IncidentClass:
        steps = runbook_steps(ic)
        assert len(steps) >= 3
        assert all(isinstance(s, str) and s for s in steps)


def test_data_loss_runbook_mentions_restore():
    steps = " ".join(runbook_steps(IncidentClass.DATA_LOSS)).lower()
    assert "restore" in steps and "backup" in steps
