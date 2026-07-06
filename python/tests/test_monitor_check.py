"""Tests for gh/monitor_check.py — slow/timeout CI job detection."""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db
from gh.monitor_check import check_slow_timeout


@pytest.fixture
def db_conn():
    """Temp SQLite DB with schema and test data."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        conn = _db.ensure_db(db_path)
        conn.execute(
            "INSERT INTO repos (owner_repo, gh_account, avg_ci_duration, max_ci_duration) "
            "VALUES ('o/r', 'test', 300, 900)"
        )
        conn.commit()
        yield conn
        conn.close()


def _seed_job(conn, job_name, status, recorded_at, conclusion=None):
    conn.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('o/r', 1, ?, ?, ?, ?)",
        (job_name, status, conclusion, recorded_at),
    )


class TestCheckSlowTimeout:
    def test_no_ema_data_skips(self, db_conn):
        """No avg_ci_duration set → skip detection."""
        db_conn.execute("UPDATE repos SET avg_ci_duration = NULL")
        db_conn.commit()
        # Should not raise
        check_slow_timeout(db_conn, "o/r", 1)

    def test_in_progress_under_threshold_no_event(self, db_conn):
        """Job running for 100s with EMA=300 → no event (under 2×)."""
        now = int(time.time())
        _seed_job(db_conn, "build", "IN_PROGRESS", now - 100)
        db_conn.commit()

        with mock.patch("gh.monitor_check.dispatch_notifications") as mock_dispatch:
            check_slow_timeout(db_conn, "o/r", 1)
            assert not mock_dispatch.called

    def test_slow_detected_above_2x_ema(self, db_conn):
        """Job running for 700s with EMA=300 → CiSlow (700 > 600)."""
        now = int(time.time())
        _seed_job(db_conn, "build", "IN_PROGRESS", now - 700)
        db_conn.commit()

        with mock.patch("gh.monitor_check.dispatch_notifications") as mock_dispatch:
            check_slow_timeout(db_conn, "o/r", 1)
            assert mock_dispatch.called
            events = mock_dispatch.call_args[0][1]
            assert len(events) == 1
            from notify import CiSlow
            assert isinstance(events[0], CiSlow)

    def test_timeout_detected_above_max(self, db_conn):
        """Job running for 1000s with max=900 → CiTimeout."""
        now = int(time.time())
        _seed_job(db_conn, "build", "IN_PROGRESS", now - 1000)
        db_conn.commit()

        with mock.patch("gh.monitor_check.dispatch_notifications") as mock_dispatch:
            check_slow_timeout(db_conn, "o/r", 1)
            assert mock_dispatch.called
            events = mock_dispatch.call_args[0][1]
            assert len(events) == 1
            from notify import CiTimeout
            assert isinstance(events[0], CiTimeout)

    def test_timeout_takes_priority_over_slow(self, db_conn):
        """When both thresholds exceeded, only CiTimeout emitted."""
        now = int(time.time())
        # Elapsed=1000 exceeds both 2×EMA(600) and max(900) → timeout only
        _seed_job(db_conn, "build", "IN_PROGRESS", now - 1000)
        db_conn.commit()

        with mock.patch("gh.monitor_check.dispatch_notifications") as mock_dispatch:
            check_slow_timeout(db_conn, "o/r", 1)
            events = mock_dispatch.call_args[0][1]
            assert len(events) == 1
            from notify import CiTimeout
            assert isinstance(events[0], CiTimeout)

    def test_exception_does_not_propagate(self, db_conn):
        """check_slow_timeout never raises — exceptions are caught."""
        # Corrupt the DB query to trigger an exception
        db_conn.execute("DROP TABLE ci_events")
        db_conn.commit()
        # Should not raise
        check_slow_timeout(db_conn, "o/r", 1)

    def test_no_jobs_no_event(self, db_conn):
        """No IN_PROGRESS jobs → no events."""
        with mock.patch("gh.monitor_check.dispatch_notifications") as mock_dispatch:
            check_slow_timeout(db_conn, "o/r", 1)
            assert not mock_dispatch.called
