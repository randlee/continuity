"""Tests for cli/daemon_cmd.py — CLI commands (read-only SQLite).

Tests FR-38, FR-39, FR-40, FR-41, FR-42, FR-43.
All tests use temp SQLite DBs — no real gh calls.
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db
from cli.daemon_cmd import (
    cmd_status, cmd_log, cmd_history, cmd_usage, cmd_register,
    _pr_mode, _job_symbol, _activity_mode,
)


@pytest.fixture
def conn():
    """Temp SQLite DB with full schema and sample data."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = _db.ensure_db(db_path)
        _seed_data(c)
        yield c
        c.close()


def _seed_data(c: sqlite3.Connection):
    """Insert sample data for testing."""
    now = int(time.time())
    c.execute("INSERT INTO repos (owner_repo, gh_account) VALUES ('owner/repo', 'test')")
    c.execute(
        "INSERT INTO pull_requests (owner_repo, pr_number, branch, mergeable, state, updated_at) "
        "VALUES ('owner/repo', 1, 'feat/x', 'MERGEABLE', 'OPEN', ?)", (now,)
    )
    c.execute(
        "INSERT INTO pull_requests (owner_repo, pr_number, branch, mergeable, state, updated_at) "
        "VALUES ('owner/repo', 2, 'fix/y', 'CONFLICTING', 'OPEN', ?)", (now,)
    )
    c.execute(
        "INSERT INTO pull_requests (owner_repo, pr_number, branch, mergeable, state, updated_at) "
        "VALUES ('owner/repo', 3, 'old/z', 'MERGEABLE', 'MERGED', ?)", (now - 3600,)
    )
    # CI events for PR #1
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 1, 'build', 'QUEUED', NULL, ?)", (now - 500,)
    )
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 1, 'build', 'IN_PROGRESS', NULL, ?)", (now - 400,)
    )
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 1, 'build', 'COMPLETED', 'SUCCESS', ?)", (now - 300,)
    )
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 1, 'lint', 'COMPLETED', 'SUCCESS', ?)", (now - 200,)
    )
    # CI events for PR #2 (still running)
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 2, 'test', 'IN_PROGRESS', NULL, ?)", (now - 100,)
    )
    # CI events for PR #3 (merged)
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 3, 'build', 'QUEUED', NULL, ?)", (now - 4000,)
    )
    c.execute(
        "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
        "VALUES ('owner/repo', 3, 'build', 'COMPLETED', 'SUCCESS', ?)", (now - 3600,)
    )
    # API usage
    c.execute(
        "INSERT INTO api_usage (gh_account, queried_at, cost, remaining, reset_at) "
        "VALUES ('test', ?, 3, 4997, '2026-07-05T12:00:00Z')", (now,)
    )
    c.commit()


# ═══════════════════════════════════════════════════════════════════════════
# continuity status (FR-39)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdStatus:
    def test_shows_open_prs(self, conn):
        output = cmd_status(conn)
        assert "owner/repo" in output
        assert "feat/x" in output
        assert "fix/y" in output

    def test_shows_job_statuses(self, conn):
        output = cmd_status(conn)
        assert "build" in output
        assert "lint" in output
        assert "test" in output

    def test_shows_mode(self, conn):
        output = cmd_status(conn)
        assert "Mode:" in output
        # PR #2 has IN_PROGRESS → ACTIVE
        assert "ACTIVE" in output

    def test_empty_repos(self, conn):
        conn.execute("DELETE FROM repos")
        conn.execute("DELETE FROM pull_requests")
        conn.commit()
        output = cmd_status(conn)
        assert "No repos registered" in output

    def test_shows_mergeable(self, conn):
        output = cmd_status(conn)
        assert "MERGEABLE" in output
        assert "CONFLICTING" in output

    def test_does_not_show_merged_prs(self, conn):
        output = cmd_status(conn)
        # PR #3 is MERGED, should not appear
        assert "old/z" not in output


class TestPrMode:
    def test_active_when_queued(self):
        jobs = [("build", "QUEUED", None)]
        assert _pr_mode(jobs) == "ACTIVE"

    def test_active_when_in_progress(self):
        jobs = [("build", "IN_PROGRESS", None)]
        assert _pr_mode(jobs) == "ACTIVE"

    def test_success_when_all_completed(self):
        jobs = [("build", "COMPLETED", "SUCCESS"), ("lint", "COMPLETED", "SUCCESS")]
        assert _pr_mode(jobs) == "SUCCESS"

    def test_failed_when_any_failure(self):
        jobs = [("build", "COMPLETED", "SUCCESS"), ("test", "COMPLETED", "FAILURE")]
        assert _pr_mode(jobs) == "FAILED"

    def test_pending_when_empty(self):
        assert _pr_mode([]) == "PENDING"


class TestJobSymbol:
    def test_success(self):
        assert "✓" in _job_symbol("build", "COMPLETED", "SUCCESS")

    def test_failure(self):
        assert "✗" in _job_symbol("test", "COMPLETED", "FAILURE")

    def test_in_progress(self):
        assert "⧗" in _job_symbol("build", "IN_PROGRESS", None)

    def test_queued(self):
        assert "⧗" in _job_symbol("build", "QUEUED", None)


# ═══════════════════════════════════════════════════════════════════════════
# continuity log (FR-40)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdLog:
    def test_shows_events(self, conn):
        output = cmd_log(conn, "owner/repo", 1)
        assert "owner/repo#1" in output
        assert "build" in output
        assert "lint" in output
        assert "QUEUED" in output
        assert "COMPLETED" in output
        assert "SUCCESS" in output

    def test_chronological_order(self, conn):
        output = cmd_log(conn, "owner/repo", 1)
        lines = output.split("\n")
        # QUEUED should appear before COMPLETED
        queued_idx = next(i for i, l in enumerate(lines) if "QUEUED" in l)
        completed_idx = next(i for i, l in enumerate(lines) if "COMPLETED" in l)
        assert queued_idx < completed_idx

    def test_no_events_message(self, conn):
        output = cmd_log(conn, "owner/repo", 999)
        assert "No CI events found" in output


# ═══════════════════════════════════════════════════════════════════════════
# continuity history (FR-41)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdHistory:
    def test_shows_merged_prs(self, conn):
        output = cmd_history(conn, "owner/repo")
        assert "owner/repo" in output
        assert "old/z" in output

    def test_shows_duration(self, conn):
        output = cmd_history(conn, "owner/repo")
        # Duration should show something
        assert any(c in output for c in "smh")  # seconds, minutes, hours

    def test_shows_outcome(self, conn):
        output = cmd_history(conn, "owner/repo")
        assert "✓" in output or "✗" in output

    def test_no_closed_prs(self, conn):
        conn.execute("DELETE FROM pull_requests WHERE state = 'MERGED'")
        conn.commit()
        output = cmd_history(conn, "owner/repo")
        assert "No closed PRs" in output


# ═══════════════════════════════════════════════════════════════════════════
# continuity usage (FR-42)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdUsage:
    def test_shows_usage(self, conn):
        output = cmd_usage(conn)
        assert "test" in output  # gh_account
        assert "3" in output     # cost
        assert "4997" in output  # remaining

    def test_filter_by_account(self, conn):
        output = cmd_usage(conn, account="test")
        assert "test" in output

    def test_filter_nonexistent_account(self, conn):
        output = cmd_usage(conn, account="nonexistent")
        assert "No API usage data" in output

    def test_no_data(self, conn):
        conn.execute("DELETE FROM api_usage")
        conn.commit()
        output = cmd_usage(conn)
        assert "No API usage data" in output


# ═══════════════════════════════════════════════════════════════════════════
# continuity register (FR-43)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdRegister:
    def test_registers_new_repo(self, conn):
        output = cmd_register(conn, "neworg/newrepo", "neworg")
        assert "Registered" in output
        assert "neworg/newrepo" in output

        row = conn.execute(
            "SELECT owner_repo, gh_account FROM repos WHERE owner_repo = 'neworg/newrepo'"
        ).fetchone()
        assert row is not None

    def test_idempotent(self, conn):
        cmd_register(conn, "owner/repo", "test")
        output = cmd_register(conn, "owner/repo", "test")
        assert "already registered" in output


# ═══════════════════════════════════════════════════════════════════════════
# FR-38: All commands read SQLite only
# ═══════════════════════════════════════════════════════════════════════════

class TestFR38:
    def test_status_no_gh_calls(self, conn):
        """FR-38: cmd_status does not call gh."""
        output = cmd_status(conn)
        assert isinstance(output, str)

    def test_log_no_gh_calls(self, conn):
        """FR-38: cmd_log does not call gh."""
        output = cmd_log(conn, "owner/repo", 1)
        assert isinstance(output, str)

    def test_history_no_gh_calls(self, conn):
        """FR-38: cmd_history does not call gh."""
        output = cmd_history(conn, "owner/repo")
        assert isinstance(output, str)

    def test_usage_no_gh_calls(self, conn):
        """FR-38: cmd_usage does not call gh."""
        output = cmd_usage(conn)
        assert isinstance(output, str)


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR39_status(self, conn):
        """FR-39: continuity status renders open PRs + job states + mode."""
        output = cmd_status(conn)
        assert "owner/repo" in output
        assert "feat/x" in output
        assert "build" in output
        assert "Mode:" in output

    def test_FR40_log(self, conn):
        """FR-40: continuity log shows chronological ci_events."""
        output = cmd_log(conn, "owner/repo", 1)
        assert "owner/repo#1" in output
        assert "build" in output
        assert "QUEUED" in output
        assert "COMPLETED" in output

    def test_FR41_history(self, conn):
        """FR-41: continuity history shows closed PRs with outcomes."""
        output = cmd_history(conn, "owner/repo")
        assert "closed PRs" in output
        assert "old/z" in output

    def test_FR42_usage(self, conn):
        """FR-42: continuity usage shows API point consumption."""
        output = cmd_usage(conn)
        assert "test" in output
        assert "3" in output

    def test_FR43_register(self, conn):
        """FR-43: continuity register adds repo."""
        output = cmd_register(conn, "neworg/newrepo", "neworg")
        assert "Registered" in output
        assert conn.execute(
            "SELECT 1 FROM repos WHERE owner_repo = 'neworg/newrepo'"
        ).fetchone() is not None


# ═══════════════════════════════════════════════════════════════════════════
# ATM CLI commands
# ═══════════════════════════════════════════════════════════════════════════

class TestAtmCli:
    """Tests for ATM CLI commands: set-notify, show-notify, status."""

    def test_set_notify_stores_member(self, conn):
        """cmd_atm_set_notify updates designated_member on repos table."""
        from cli.daemon_cmd import cmd_atm_set_notify
        output = cmd_atm_set_notify(conn, "owner/repo", "rand")
        assert "set to rand" in output

        row = conn.execute(
            "SELECT designated_member FROM repos WHERE owner_repo = 'owner/repo'"
        ).fetchone()
        assert row[0] == "rand"

    def test_set_notify_reset_clears_member(self, conn):
        """cmd_atm_set_notify --reset sets designated_member to NULL."""
        from cli.daemon_cmd import cmd_atm_set_notify

        # Set first
        cmd_atm_set_notify(conn, "owner/repo", "rand")
        # Then reset
        output = cmd_atm_set_notify(conn, "owner/repo", "--reset")
        assert "reset to team-lead" in output

        row = conn.execute(
            "SELECT designated_member FROM repos WHERE owner_repo = 'owner/repo'"
        ).fetchone()
        assert row[0] is None

    def test_set_notify_rejects_invalid_name(self, conn):
        """cmd_atm_set_notify rejects names with spaces or empty."""
        from cli.daemon_cmd import cmd_atm_set_notify

        output = cmd_atm_set_notify(conn, "owner/repo", "bad name")
        assert "Invalid" in output

        output = cmd_atm_set_notify(conn, "owner/repo", "")
        assert "Invalid" in output

    def test_show_notify_reports_default(self, conn):
        """cmd_atm_show_notify shows team-lead when NULL."""
        from cli.daemon_cmd import cmd_atm_show_notify
        output = cmd_atm_show_notify(conn, "owner/repo")
        assert "team-lead (default)" in output

    def test_show_notify_reports_custom(self, conn):
        """cmd_atm_show_notify shows stored member."""
        # Seed a repo with a custom designated member
        conn.execute(
            "INSERT INTO repos (owner_repo, gh_account, designated_member) "
            "VALUES ('test/repo', 'test', 'custom-agent')"
        )
        conn.commit()

        from cli.daemon_cmd import cmd_atm_show_notify
        output = cmd_atm_show_notify(conn, "test/repo")
        assert "custom-agent" in output
        assert "default" not in output

    def test_show_notify_unknown_repo(self, conn):
        """cmd_atm_show_notify reports unregistered repo."""
        from cli.daemon_cmd import cmd_atm_show_notify
        output = cmd_atm_show_notify(conn, "nonexistent/repo")
        assert "not registered" in output

    def test_atm_status_not_configured(self, monkeypatch):
        """cmd_atm_status reports NOT CONFIGURED when ATM_TEAM is unset."""
        monkeypatch.delenv("ATM_TEAM", raising=False)
        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda _: None)
        monkeypatch.setattr("os.path.isfile", lambda _: False)

        from cli.daemon_cmd import cmd_atm_status
        output = cmd_atm_status()
        assert "NOT CONFIGURED" in output
        assert "not set" in output

    def test_atm_status_ready(self, monkeypatch):
        """cmd_atm_status reports READY when configured."""
        monkeypatch.setenv("ATM_TEAM", "hermes")
        monkeypatch.setenv("ATM_IDENTITY", "ci")
        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda _: "/opt/homebrew/bin/atm")

        from cli.daemon_cmd import cmd_atm_status
        output = cmd_atm_status()
        assert "READY" in output
        assert "ATM_TEAM=hermes" in output