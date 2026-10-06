"""Tests for the backup-freshness check on /readyz.

The nightly backup failed for two days with `203/EXEC` and nothing said so. Its
failure is invisible by construction: no user waits on it and no request fails
because of it, and `systemctl list-timers` kept reporting `active (waiting)`,
which reads like a healthy job.

So the check itself is the thing that needs tests - it is the only thing standing
between a broken nightly job and nobody finding out.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from app.utils.metrics import BACKUP_STALE_AFTER


@pytest.fixture()
def backup_dir(app, tmp_path):
    """A BACKUP_DIR the app under test will look at.

    Set on the fixture's own application rather than on a freshly built one: the
    `app` fixture is the application `client` requests go to, and building a second
    app here would configure a different object than the one being tested - which
    reads as "disabled" rather than as the failure being investigated.
    """
    directory = tmp_path / "backups"
    directory.mkdir()
    app.config["BACKUP_DIR"] = str(directory)
    return directory


def make_dump(directory: pathlib.Path, age_seconds: float = 0.0) -> pathlib.Path:
    dump = directory / f"harmony-{int(time.time() - age_seconds)}.dump"
    dump.write_bytes(b"dump")
    stamp = time.time() - age_seconds
    import os

    os.utime(dump, (stamp, stamp))
    return dump


def backup_field(client):
    response = client.get("/readyz")
    return response.get_json()["checks"]["backup"]


def test_readyz_reports_the_backup_age(app, backup_dir, client):
    make_dump(backup_dir, age_seconds=3600)  # an hour ago

    field = backup_field(client)

    assert field["status"] == "ok", field
    assert field["age_hours"] == pytest.approx(1.0, abs=0.1)
    assert field["dumps_on_disk"] == 1


def test_a_stale_backup_is_reported(app, backup_dir, client):
    """The case that actually happened. Two days of missed backups."""
    make_dump(backup_dir, age_seconds=2 * 86400)

    field = backup_field(client)

    assert field["status"] == "stale", field
    assert field["age_hours"] > 40


@pytest.mark.parametrize(
    ("age_hours", "expected"),
    [(1, "ok"), (23, "ok"), (25, "ok"), (27, "stale"), (48, "stale")],
)
def test_the_threshold_is_26_hours(app, backup_dir, client, age_hours, expected):
    """26 hours, not 24: the timer has `RandomizedDelaySec=600` and the job takes
    a few seconds, so a healthy run lands a little after 04:17. A check that
    cries wolf gets ignored, which is worse than no check at all."""
    make_dump(backup_dir, age_seconds=age_hours * 3600)

    assert backup_field(client)["status"] == expected


def test_a_failure_marker_is_reported_as_an_error(app, backup_dir, client):
    """The unit's OnFailure hook writes this, so a job that ran and crashed is
    distinguishable from one that never started."""
    make_dump(backup_dir, age_seconds=600)
    (backup_dir / "LAST_FAILURE_EPOCH").write_text(str(int(time.time()) - 300))

    field = backup_field(client)

    assert field["status"] == "error", field
    assert "failed" in field["reason"]
    assert "failed_at" in field


def test_no_dumps_at_all_is_its_own_state(app, backup_dir, client):
    """Distinct from stale: a fresh install has no dumps and is not in trouble."""
    field = backup_field(client)

    assert field["status"] == "no_dumps_yet", field
    assert field["dumps_on_disk"] == 0


def test_a_missing_directory_is_an_error_not_a_crash(app, tmp_path, client):
    app.config["BACKUP_DIR"] = str(tmp_path / "absent")

    field = backup_field(client)

    assert field["status"] == "error", field


def test_an_unconfigured_deployment_says_disabled(app, client):
    app.config["BACKUP_DIR"] = None

    field = backup_field(client)

    assert field["status"] == "disabled", field


def test_a_stale_backup_does_not_make_the_app_unready(app, backup_dir, client):
    """Deliberate. The site is serving fine, and refusing traffic would turn a
    housekeeping problem into an outage - the only thing that gets worse is how
    far the last good backup is from now."""
    make_dump(backup_dir, age_seconds=5 * 86400)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ready"


def test_only_real_dumps_count(app, backup_dir, client):
    """The uploads tarballs and the log live in the same directory. Counting them
    would report a fresh backup when the database dump is three weeks old."""
    make_dump(backup_dir, age_seconds=3 * 86400)
    (backup_dir / "uploads-20260101-000000.tar.gz").write_bytes(b"x")
    (backup_dir / "backup.log").write_text("log")
    (backup_dir / "LAST_FAILURE_EPOCH").write_text("0")

    field = backup_field(client)

    assert field["status"] == "stale", field
    assert field["dumps_on_disk"] == 1


def test_the_threshold_is_stated_in_hours_not_wall_clock(app):
    """Pinned so the constant cannot quietly become '1 second' in a refactor."""
    assert BACKUP_STALE_AFTER == 26 * 3600