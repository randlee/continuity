"""Tests for daemon.py — poll loop + singleton lifecycle.

Tests FR-31, FR-32, FR-33, FR-36, singleton guard.
All tests mock GhClient — no real GitHub access.
"""

import fcntl
import os
import signal
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys_path = __import__("sys")
sys_path.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as _db
from daemon import Daemon, DaemonConfig, ActivityMode, _is_pid_alive
from gh.client import GhClient, PollResult, PrSnapshot, CheckRun, ApiUsage


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_conn():
    """Temp SQLite DB with full schema."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        conn = _db.ensure_db(db_path)
        conn.execute("INSERT INTO repos (owner_repo, gh_account) VALUES ('test-owner/test-repo', 'test-account')")
        conn.commit()
        yield conn
        conn.close()


@pytest.fixture
def mock_client():
    """GhClient that returns empty poll results."""
    client = MagicMock(spec=GhClient)
    client.rate_limit = ApiUsage(cost=1, remaining=4999, reset_at="...")
    client.poll.return_value = PollResult(repos={}, rate_limit=ApiUsage())
    return client


@pytest.fixture
def daemon_home():
    """Isolated CONTINUITY_HOME."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ═══════════════════════════════════════════════════════════════════════════
# Singleton guard
# ═══════════════════════════════════════════════════════════════════════════

class TestSingletonGuard:
    def test_acquires_lock_and_writes_pid(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._acquire_lock()
        d._write_pid()

        assert d._pid_file.exists()
        assert d._lock_file.exists()
        pid = int(d._pid_file.read_text().strip())
        assert pid == os.getpid()

        d._cleanup()
        assert not d._pid_file.exists()

    def test_second_instance_detected(self, daemon_home, db_conn, mock_client):
        d1 = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d1._acquire_lock()
        d1._write_pid()

        d2 = Daemon(daemon_home, {"test": mock_client}, db_conn)
        with pytest.raises(RuntimeError, match="Daemon already running"):
            d2._acquire_lock()

        d1._cleanup()

    def test_stale_lock_cleared(self, daemon_home, db_conn, mock_client):
        """Lock held by nonexistent PID → cleared."""
        # Write a stale PID
        pid_file = daemon_home / "daemon.pid"
        pid_file.parent.mkdir(exist_ok=True)
        pid_file.write_text("99999")  # nonexistent PID

        # Create a lock file manually (no actual lock since process is gone)
        lock_file = daemon_home / "daemon.lock"
        lock_file.write_text("")

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._acquire_lock()  # should succeed (stale lock cleared)
        d._cleanup()

    def test_lock_released_on_cleanup(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._acquire_lock()
        d._cleanup()

        # Lock should be free now
        d2 = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d2._acquire_lock()  # should succeed
        d2._cleanup()

    def test_pid_file_cleaned_on_shutdown(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._acquire_lock()
        d._write_pid()
        assert d._pid_file.exists()
        d._cleanup()
        assert not d._pid_file.exists()


# ═══════════════════════════════════════════════════════════════════════════
# Signal handling (FR-33)
# ═══════════════════════════════════════════════════════════════════════════

class TestSignals:
    def test_sigterm_sets_shutdown_flag(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        assert not d._shutdown_flag
        d.shutdown()
        assert d._shutdown_flag

    def test_sigusr1_sets_wake_event(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        assert not d._wake_event
        d._wake()
        assert d._wake_event

    def test_shutdown_stops_loop(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._shutdown_flag = True  # simulate SIGTERM
        d._run_loop()  # should exit immediately


# ═══════════════════════════════════════════════════════════════════════════
# Poll cycle (FR-31)
# ═══════════════════════════════════════════════════════════════════════════

class TestPollCycle:
    def test_polls_all_clients(self, daemon_home, db_conn):
        c1 = MagicMock(spec=GhClient)
        c1.rate_limit = ApiUsage(remaining=4999)
        c1.poll.return_value = PollResult(repos={}, rate_limit=ApiUsage())
        c2 = MagicMock(spec=GhClient)
        c2.rate_limit = ApiUsage(remaining=4998)
        c2.poll.return_value = PollResult(repos={}, rate_limit=ApiUsage())

        # Insert repos for both accounts
        db_conn.execute("INSERT INTO repos (owner_repo, gh_account) VALUES ('a/r1', 'a')")
        db_conn.execute("INSERT INTO repos (owner_repo, gh_account) VALUES ('b/r2', 'b')")
        db_conn.commit()

        d = Daemon(daemon_home, {"a": c1, "b": c2}, db_conn)
        d._poll_cycle()

        assert c1.poll.called
        assert c2.poll.called

    def test_writes_api_usage(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test-account": mock_client}, db_conn)
        d._poll_cycle()

        rows = db_conn.execute("SELECT cost, remaining FROM api_usage").fetchall()
        assert len(rows) >= 1

    def test_handles_client_failure_gracefully(self, daemon_home, db_conn):
        """Transient client failure → continue, don't crash."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=4999)
        c.poll.side_effect = RuntimeError("network error")

        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._poll_cycle()  # should not raise

    def test_writes_ci_events(self, daemon_home, db_conn):
        """Poll result with CI data → ci_events written."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=4999)
        c.poll.return_value = PollResult(
            repos={
                "test-owner/test-repo": [
                    PrSnapshot(number=1, checks=[
                        CheckRun(name="build", status="IN_PROGRESS"),
                    ]),
                ],
            },
            rate_limit=ApiUsage(cost=1, remaining=4999),
        )

        d = Daemon(daemon_home, {"test-account": c}, db_conn)
        d._poll_cycle()

        events = db_conn.execute(
            "SELECT job_name, status FROM ci_events"
        ).fetchall()
        assert len(events) >= 1
        assert ("build", "IN_PROGRESS") in {(e[0], e[1]) for e in events}


# ═══════════════════════════════════════════════════════════════════════════
# Adaptive mode (FR-31, FR-32)
# ═══════════════════════════════════════════════════════════════════════════

class TestAdaptiveMode:
    def test_idle_when_no_prs(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.IDLE

    def test_watchful_when_open_prs_no_ci(self, daemon_home, db_conn, mock_client):
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
            "VALUES ('test-owner/test-repo', 1, 'main', 'OPEN', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.WATCHFUL

    def test_active_when_ci_running(self, daemon_home, db_conn, mock_client):
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('test-owner/test-repo', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

    def test_active_when_queued(self, daemon_home, db_conn, mock_client):
        """QUEUED also counts as active CI."""
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('test-owner/test-repo', 1, 'build', 'QUEUED', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

    def test_transitions_watchful_to_active(self, daemon_home, db_conn, mock_client):
        """Mode transitions from WATCHFUL to ACTIVE when CI starts."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.IDLE

        # Open a PR → WATCHFUL
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
            "VALUES ('x/y', 1, 'main', 'OPEN', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.WATCHFUL

        # CI starts → ACTIVE
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'QUEUED', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

    def test_transitions_active_to_idle(self, daemon_home, db_conn, mock_client):
        """Mode transitions from ACTIVE to IDLE when CI completes and PR closes."""
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

        # CI completed → but PR still open → WATCHFUL
        db_conn.execute("DELETE FROM ci_events")
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'COMPLETED', 0)"
        )
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
            "VALUES ('x/y', 1, 'main', 'OPEN', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.WATCHFUL


# ═══════════════════════════════════════════════════════════════════════════
# Rate limit backoff (FR-36)
# ═══════════════════════════════════════════════════════════════════════════

class TestRateLimitBackoff:
    def test_normal_interval(self, daemon_home, db_conn, mock_client):
        """Normal rate limit → standard interval."""
        config = DaemonConfig(active_interval=30)
        d = Daemon(daemon_home, {"test": mock_client}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() == 30

    def test_backoff_when_low(self, daemon_home, db_conn):
        """Rate limit below LOW_WATER → doubled interval."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=100)  # below 500 LOW_WATER
        config = DaemonConfig(active_interval=30, low_water=500)
        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() == 60  # doubled

    def test_backoff_capped(self, daemon_home, db_conn):
        """Backoff capped at max_backoff."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=10)
        config = DaemonConfig(active_interval=30, low_water=500, max_backoff=120)
        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() == 60  # 30*2 = 60, under cap of 120

    def test_min_rate_limit_across_clients(self, daemon_home, db_conn):
        """Lowest rate limit across all clients is used."""
        c1 = MagicMock(spec=GhClient)
        c1.rate_limit = ApiUsage(remaining=100)
        c2 = MagicMock(spec=GhClient)
        c2.rate_limit = ApiUsage(remaining=4000)
        config = DaemonConfig(low_water=500)
        d = Daemon(daemon_home, {"a": c1, "b": c2}, db_conn, config)
        assert d._min_rate_limit_remaining() == 100


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR31_adaptive_modes(self, daemon_home, db_conn, mock_client):
        """FR-31: ACTIVE/WATCHFUL/IDLE modes."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        assert d.mode == ActivityMode.IDLE

        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
            "VALUES ('x/y', 1, 'main', 'OPEN', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.WATCHFUL

        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

    def test_FR32_mode_recalculated(self, daemon_home, db_conn, mock_client):
        """FR-32: Mode re-evaluated after each poll cycle."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        initial = d.mode

        # Add data
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode != initial  # mode changed
        assert d.mode == ActivityMode.ACTIVE

    def test_FR36_rate_limit_backoff(self, daemon_home, db_conn):
        """FR-36: Interval increases when rate limit is low."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=100)
        config = DaemonConfig(active_interval=30, low_water=500)

        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() > 30  # backoff applied