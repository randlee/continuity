"""Tests for daemon.py — poll loop + singleton lifecycle.

Tests FR-31, FR-32, FR-33, FR-36, singleton guard.
All tests mock GhClient — no real GitHub access.
"""

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
from notify import CiSlow, CiTimeout, CiCompleted


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
        assert d._lock_dir.exists()
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

        # Create a stale lock directory
        lock_dir = daemon_home / "daemon.lock"
        lock_dir.mkdir(exist_ok=True)

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
    def test_inactive_by_default(self, daemon_home, db_conn, mock_client):
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.INACTIVE

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

    def test_pr_changed_when_unknown_mergeable(self, daemon_home, db_conn, mock_client):
        """PR_CHANGED when any PR has mergeable=UNKNOWN."""
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES ('x/y', 1, 'main', 'OPEN', 'UNKNOWN', 0)"
        )
        db_conn.commit()
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED

    def test_pr_changed_overrides_active(self, daemon_home, db_conn, mock_client):
        """PR_CHANGED takes priority over ACTIVE when both conditions exist."""
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES ('x/y', 2, 'feat', 'OPEN', 'UNKNOWN', 0)"
        )
        db_conn.commit()
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED

    def test_transitions_to_inactive_when_ci_completes(self, daemon_home, db_conn, mock_client):
        """Mode transitions from ACTIVE to INACTIVE when CI completes."""
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

        # CI completed → INACTIVE (no UNKNOWN, no active CI)
        db_conn.execute("DELETE FROM ci_events")
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'COMPLETED', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.INACTIVE


# ═══════════════════════════════════════════════════════════════════════════
# Rate limit backoff (FR-36)
# ═══════════════════════════════════════════════════════════════════════════

class TestRateLimitBackoff:
    def test_normal_interval(self, daemon_home, db_conn, mock_client):
        """Normal rate limit → standard interval."""
        config = DaemonConfig(active_interval=300)
        d = Daemon(daemon_home, {"test": mock_client}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() == 300

    def test_backoff_when_low(self, daemon_home, db_conn):
        """Rate limit below LOW_WATER → doubled interval."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=500)  # below 1000 LOW_WATER
        config = DaemonConfig(active_interval=300, low_water=1000)
        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() == 600  # doubled

    def test_backoff_capped(self, daemon_home, db_conn):
        """Backoff capped at max_backoff."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=10)
        config = DaemonConfig(pr_changed_interval=30, low_water=1000, max_backoff=120)
        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.PR_CHANGED
        assert d._next_interval() == 60  # 30*2 = 60, under cap of 120

    def test_min_rate_limit_across_clients(self, daemon_home, db_conn):
        """Lowest rate limit across all clients is used."""
        c1 = MagicMock(spec=GhClient)
        c1.rate_limit = ApiUsage(remaining=100)
        c2 = MagicMock(spec=GhClient)
        c2.rate_limit = ApiUsage(remaining=4000)
        config = DaemonConfig(low_water=1000)
        d = Daemon(daemon_home, {"a": c1, "b": c2}, db_conn, config)
        assert d._min_rate_limit_remaining() == 100


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR31_adaptive_modes(self, daemon_home, db_conn, mock_client):
        """FR-31: PR_CHANGED/ACTIVE/INACTIVE modes."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        assert d.mode == ActivityMode.INACTIVE

        # CI starts → ACTIVE
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE

        # Add UNKNOWN mergeable → PR_CHANGED (overrides ACTIVE)
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES ('x/y', 2, 'feat', 'OPEN', 'UNKNOWN', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED

    def test_FR32_mode_recalculated(self, daemon_home, db_conn, mock_client):
        """FR-32: Mode re-evaluated after each poll cycle."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        initial = d.mode

        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'QUEUED', 0)"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode != initial
        assert d.mode == ActivityMode.ACTIVE

    def test_FR36_rate_limit_backoff(self, daemon_home, db_conn):
        """FR-36: Interval increases when rate limit is low."""
        c = MagicMock(spec=GhClient)
        c.rate_limit = ApiUsage(remaining=100)
        config = DaemonConfig(active_interval=300, low_water=1000)
        d = Daemon(daemon_home, {"test": c}, db_conn, config)
        d.mode = ActivityMode.ACTIVE
        assert d._next_interval() > 300  # backoff applied


# ═══════════════════════════════════════════════════════════════════════════
# POST_PUSH_DELAY + mode transition edge cases (to be fixed)
# ═══════════════════════════════════════════════════════════════════════════

class TestPostPushDelay:
    """Tests for POST_PUSH_DELAY behavior and mode transitions."""

    def test_wake_sets_pr_changed_mode(self, daemon_home, db_conn, mock_client):
        """GAP: _wake should immediately set mode to PR_CHANGED."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        assert d.mode == ActivityMode.INACTIVE

        d._wake()
        assert d.mode == ActivityMode.PR_CHANGED, (
            "_wake should set mode to PR_CHANGED immediately"
        )
        assert d._wake_event is True
        assert d._scheduled_wake_at > 0

    def test_first_wins_multiple_wakes(self, daemon_home, db_conn, mock_client):
        """GAP: first SIGUSR1 wins; subsequent signals within delay window ignored."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._wake()
        first_scheduled = d._scheduled_wake_at

        # Second wake within window → ignored
        d._wake()
        assert d._scheduled_wake_at == first_scheduled, (
            "first-wins: second wake should not change scheduled time"
        )

    def test_pr_changed_persists_while_unknown(self, daemon_home, db_conn, mock_client):
        """PR_CHANGED stays active as long as mergeable remains UNKNOWN."""
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES ('x/y', 1, 'feat', 'OPEN', 'UNKNOWN', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED

        # After another poll cycle with same data, should still be PR_CHANGED
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED, (
            "PR_CHANGED should persist while mergeable is UNKNOWN"
        )

    def test_pr_changed_transitions_to_active_when_mergeable_computed(self, daemon_home, db_conn, mock_client):
        """PR_CHANGED → ACTIVE when mergeable changes from UNKNOWN to MERGEABLE."""
        db_conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES ('x/y', 1, 'feat', 'OPEN', 'UNKNOWN', 0)"
        )
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, recorded_at) "
            "VALUES ('x/y', 1, 'build', 'IN_PROGRESS', 0)"
        )
        db_conn.commit()

        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED  # UNKNOWN takes priority

        # Mergeable computed → now ACTIVE (CI still running)
        db_conn.execute(
            "UPDATE pull_requests SET mergeable = 'MERGEABLE' WHERE pr_number = 1"
        )
        db_conn.commit()
        d._recalculate_mode()
        assert d.mode == ActivityMode.ACTIVE, (
            "Should transition to ACTIVE when mergeable is no longer UNKNOWN and CI running"
        )

    def test_recalculate_mode_overridden_by_pending_wake(self, daemon_home, db_conn, mock_client):
        """GAP: If _wake_event is set, _recalculate_mode should honor it."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._wake()  # sets mode to PR_CHANGED, schedules wake

        # Simulate what happens after a poll: _recalculate_mode sees no UNKNOWN, no CI
        # and would set INACTIVE — but a wake was requested during the poll
        d._recalculate_mode()
        assert d.mode == ActivityMode.PR_CHANGED, (
            "Pending wake should keep mode at PR_CHANGED after recalculation"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Monitor integration tests — EMA, slow/timeout detection
# ═══════════════════════════════════════════════════════════════════════════

class TestMonitorIntegration:
    """Tests for _update_ema and _check_monitor."""

    def _seed_ci_events(self, conn, owner_repo: str, pr_number: int,
                        job_name: str, timestamps: list[tuple[str, str | None, int]]):
        """Insert ci_events. timestamps = [(status, conclusion, recorded_at), ...]."""
        for status, conclusion, ts in timestamps:
            conn.execute(
                "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (owner_repo, pr_number, job_name, status, conclusion, ts),
            )

    def test_update_ema_first_run(self, daemon_home, db_conn):
        """First successful run seeds EMA with execution time."""
        now = 1000
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("QUEUED", None, 500),
            ("IN_PROGRESS", None, 600),
            ("COMPLETED", "SUCCESS", 900),  # 300s execution
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._update_ema("o/r", "build", now)

        assert d._ema[("o/r", "build")] == 300.0
        assert d._ema_count[("o/r", "build")] == 1

    def test_update_ema_converges(self, daemon_home, db_conn):
        """EMA converges toward recent values."""
        now = 1000
        # First run: 100s
        self._seed_ci_events(db_conn, "o/r", 1, "test", [
            ("IN_PROGRESS", None, 100),
            ("COMPLETED", "SUCCESS", 200),
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._update_ema("o/r", "test", now)
        assert d._ema[("o/r", "test")] == 100.0

        # Second run: 200s. EMA = 0.2*200 + 0.8*100 = 120
        self._seed_ci_events(db_conn, "o/r", 1, "test", [
            ("IN_PROGRESS", None, 300),
            ("COMPLETED", "SUCCESS", 500),
        ])
        db_conn.commit()
        d._update_ema("o/r", "test", now)
        assert d._ema[("o/r", "test")] == pytest.approx(120.0)
        assert d._ema_count[("o/r", "test")] == 2

    def test_ema_requires_min_samples(self, daemon_home, db_conn):
        """check_monitor skips when fewer than MIN_SAMPLES runs completed."""
        now = 1000
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("QUEUED", None, 100),
            ("IN_PROGRESS", None, 110),
            ("IN_PROGRESS", None, 200),  # latest is IN_PROGRESS
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        # No EMA data yet — count is 0
        events = d._check_monitor(now)
        assert events == []

    def test_check_monitor_slow(self, daemon_home, db_conn):
        """Emits CiSlow when elapsed > 2× EMA."""
        now = 5000

        # Seed EMA: 3 runs at 100s each → EMA ≈ 100
        for i in range(3):
            self._seed_ci_events(db_conn, "o/r", 1, "build", [
                ("IN_PROGRESS", None, i * 500 + 100),
                ("COMPLETED", "SUCCESS", i * 500 + 200),
            ])
        # Current run IN_PROGRESS at t=4600, now=5000 → 400s elapsed
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("IN_PROGRESS", None, 4600),
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)

        # Feed EMA data
        d._ema[("o/r", "build")] = 100.0
        d._ema_count[("o/r", "build")] = 3

        events = d._check_monitor(now)
        assert len(events) == 1
        assert isinstance(events[0], CiSlow)
        assert events[0].pr_number == 1
        assert events[0].job_name == "build"
        assert events[0].elapsed_seconds == 400

    def test_check_monitor_hung(self, daemon_home, db_conn):
        """Emits CiTimeout when elapsed > 5× EMA."""
        now = 1000
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("IN_PROGRESS", None, 400),  # started at 400, now 1000 → 600s
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._ema[("o/r", "build")] = 100.0
        d._ema_count[("o/r", "build")] = 3

        events = d._check_monitor(now)
        assert len(events) == 1
        assert isinstance(events[0], CiTimeout)

    def test_check_monitor_dedup(self, daemon_home, db_conn):
        """Does not re-emit slow/timeout for same (repo, pr, job, type)."""
        now = 1000
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("IN_PROGRESS", None, 600),
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._ema[("o/r", "build")] = 100.0
        d._ema_count[("o/r", "build")] = 3

        # First call emits
        events1 = d._check_monitor(now)
        assert len(events1) == 1

        # Second call on same data does not re-emit
        events2 = d._check_monitor(now)
        assert events2 == []

    def test_check_monitor_clears_dedup_on_new_run(self, daemon_home, db_conn):
        """Dedup entries cleared when new run starts (EMA updated)."""
        now = 1000
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("IN_PROGRESS", None, 600),
        ])
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        d._ema[("o/r", "build")] = 100.0
        d._ema_count[("o/r", "build")] = 3

        # Emit slow
        d._check_monitor(now)
        assert len(d._notified_monitor) == 1

        # Update EMA (new run completed)
        self._seed_ci_events(db_conn, "o/r", 1, "build", [
            ("IN_PROGRESS", None, 700),
            ("COMPLETED", "SUCCESS", 800),
        ])
        db_conn.commit()
        d._update_ema("o/r", "build", now)

        # Dedup should be cleared
        assert len(d._notified_monitor) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Daemon internal helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestDaemonHelpers:
    def test_sleep_interruptible_wakes_at_scheduled_time(self, daemon_home, db_conn, mock_client):
        """_sleep_interruptible returns early when scheduled wake time arrives."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._wake_event = True
        d._scheduled_wake_at = time.time() + 0.05  # 50ms from now

        start = time.time()
        d._sleep_interruptible(10.0)  # 10s sleep, should wake at 50ms
        elapsed = time.time() - start
        assert elapsed < 1.0, f"Sleep took {elapsed}s, should wake at ~0.05s"

    def test_sleep_interruptible_ignores_past_wake(self, daemon_home, db_conn, mock_client):
        """_sleep_interruptible ignores stale wake time in the past."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._wake_event = True
        d._scheduled_wake_at = time.time() - 60  # 60s ago — already passed

        start = time.time()
        d._sleep_interruptible(0.1)  # short sleep, should wake on deadline
        elapsed = time.time() - start
        assert elapsed < 1.0, f"Sleep took {elapsed}s, should return quickly"

    def test_sleep_interruptible_no_wake_sleeps_full(self, daemon_home, db_conn, mock_client):
        """_sleep_interruptible sleeps full duration when no wake scheduled."""
        d = Daemon(daemon_home, {"test": mock_client}, db_conn)
        d._wake_event = False

        start = time.time()
        d._sleep_interruptible(0.1)  # 100ms
        elapsed = time.time() - start
        assert elapsed >= 0.08, f"Sleep took only {elapsed}s, should sleep ~0.1s"

    def test_min_rate_limit_remaining_no_clients(self, daemon_home, db_conn):
        """Returns 5000 when no clients are configured."""
        d = Daemon(daemon_home, {}, db_conn)
        assert d._min_rate_limit_remaining() == 5000

    def test_min_rate_limit_remaining_min_across_clients(self, daemon_home, db_conn):
        """Returns the minimum across all clients."""
        c1 = MagicMock(spec=GhClient)
        c1.rate_limit = ApiUsage(remaining=200)
        c2 = MagicMock(spec=GhClient)
        c2.rate_limit = ApiUsage(remaining=50)
        d = Daemon(daemon_home, {"a": c1, "b": c2}, db_conn)
        assert d._min_rate_limit_remaining() == 50


# ═══════════════════════════════════════════════════════════════════════════
# _add_ci_completion
# ═══════════════════════════════════════════════════════════════════════════

class TestAddCiCompletion:
    def test_emits_when_all_jobs_terminal(self, daemon_home, db_conn):
        """Emits CiCompleted when all jobs are COMPLETED with SUCCESS."""
        now = 1000
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'build', 'COMPLETED', 'SUCCESS', ?)", (now,)
        )
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'test', 'COMPLETED', 'SUCCESS', ?)", (now,)
        )
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        events: list = []
        from daemon import _add_ci_completion
        _add_ci_completion(events, "o/r", 1, db_conn, {})

        assert len(events) == 1
        assert isinstance(events[0], CiCompleted)
        assert events[0].conclusion == "SUCCESS"

    def test_no_emit_when_jobs_still_running(self, daemon_home, db_conn):
        """Does not emit when some jobs are still IN_PROGRESS."""
        now = 1000
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'build', 'COMPLETED', 'SUCCESS', ?)", (now,)
        )
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'test', 'IN_PROGRESS', NULL, ?)", (now,)
        )
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        events: list = []
        from daemon import _add_ci_completion
        _add_ci_completion(events, "o/r", 1, db_conn, {})

        assert events == []  # not all terminal

    def test_emits_failure_with_failed_jobs(self, daemon_home, db_conn):
        """Emits CiCompleted with FAILURE and lists failed jobs."""
        now = 1000
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'build', 'COMPLETED', 'FAILURE', ?)", (now,)
        )
        db_conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'test', 'COMPLETED', 'SUCCESS', ?)", (now,)
        )
        db_conn.commit()

        c = MagicMock(spec=GhClient)
        d = Daemon(daemon_home, {"test": c}, db_conn)
        events: list = []
        from daemon import _add_ci_completion
        _add_ci_completion(events, "o/r", 1, db_conn, {})

        assert len(events) == 1
        assert events[0].conclusion == "FAILURE"
        assert events[0].failed_jobs == ["build"]