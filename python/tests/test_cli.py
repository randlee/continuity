"""Tests for cli/daemon_cmd.py — CLI commands via HTTP RPC.

Tests FR-38, FR-39, FR-40, FR-41, FR-42, FR-43.
Uses mocked HTTP responses — no real daemon, no real gh calls.
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db
from cli.daemon_cmd import (
    cmd_status, cmd_log, cmd_history, cmd_usage, cmd_poll, cmd_register,
    _pr_mode, _job_symbol,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _mock_get_response(data: dict, status_ok: bool = True):
    """Build a mock for cli.http_client.get that returns the given data."""
    response = {"status": "ok" if status_ok else "error", **data}
    return mock.patch("cli.daemon_cmd.get", return_value=response)


def _mock_post_response(data: dict, status_ok: bool = True):
    """Build a mock for cli.http_client.post that returns the given data."""
    response = {"status": "ok" if status_ok else "error", **data}
    return mock.patch("cli.daemon_cmd.post", return_value=response)


# ═══════════════════════════════════════════════════════════════════════════
# ci status (FR-39)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdStatus:
    def test_shows_open_prs(self):
        data = {
            "prs": [
                {"owner_repo": "owner/repo", "pr_number": 1, "branch": "feat/x",
                 "mergeable": "MERGEABLE", "jobs": []},
                {"owner_repo": "owner/repo", "pr_number": 2, "branch": "fix/y",
                 "mergeable": "CONFLICTING", "jobs": []},
            ],
            "mode": "ACTIVE",
        }
        with _mock_get_response(data):
            output = cmd_status()
        assert "owner/repo" in output
        assert "feat/x" in output
        assert "fix/y" in output
        assert "Mode: ACTIVE" in output

    def test_shows_job_statuses(self):
        data = {
            "prs": [
                {"owner_repo": "owner/repo", "pr_number": 1, "branch": "feat/x",
                 "mergeable": "MERGEABLE",
                 "jobs": [
                     {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                     {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                 ]},
            ],
            "mode": "INACTIVE",
        }
        with _mock_get_response(data):
            output = cmd_status()
        assert "build" in output
        assert "lint" in output

    def test_empty_repos(self):
        with _mock_get_response({"prs": [], "mode": "INACTIVE"}):
            output = cmd_status()
        assert "No repos registered" in output

    def test_shows_mergeable(self):
        data = {
            "prs": [
                {"owner_repo": "o/r", "pr_number": 1, "branch": "x",
                 "mergeable": "MERGEABLE", "jobs": []},
                {"owner_repo": "o/r", "pr_number": 2, "branch": "y",
                 "mergeable": "CONFLICTING", "jobs": []},
            ],
            "mode": "INACTIVE",
        }
        with _mock_get_response(data):
            output = cmd_status()
        assert "MERGEABLE" in output
        assert "CONFLICTING" in output

    def test_handles_daemon_error(self):
        from cli.http_client import DaemonError
        with mock.patch("cli.daemon_cmd.get", side_effect=DaemonError("daemon not running")):
            output = cmd_status()
        assert "Error: daemon not running" in output


class TestPrMode:
    def test_active_when_queued(self):
        jobs = [{"name": "build", "status": "QUEUED", "conclusion": None}]
        assert _pr_mode(jobs) == "ACTIVE"

    def test_active_when_in_progress(self):
        jobs = [{"name": "build", "status": "IN_PROGRESS", "conclusion": None}]
        assert _pr_mode(jobs) == "ACTIVE"

    def test_success_when_all_completed(self):
        jobs = [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _pr_mode(jobs) == "SUCCESS"

    def test_failed_when_any_failure(self):
        jobs = [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
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
# ci log (FR-40)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdLog:
    def test_shows_events(self):
        data = {
            "owner_repo": "owner/repo", "pr_number": 1,
            "events": [
                {"job": "build", "status": "QUEUED", "conclusion": None, "at": 1000},
                {"job": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "at": 1100},
            ],
        }
        with _mock_get_response(data):
            output = cmd_log("owner/repo", 1)
        assert "owner/repo#1" in output
        assert "build" in output
        assert "QUEUED" in output
        assert "COMPLETED" in output
        assert "SUCCESS" in output

    def test_chronological_order(self):
        data = {
            "events": [
                {"job": "build", "status": "QUEUED", "conclusion": None, "at": 1000},
                {"job": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "at": 1100},
            ],
        }
        with _mock_get_response(data):
            output = cmd_log("owner/repo", 1)
        lines = output.split("\n")
        queued_idx = next(i for i, l in enumerate(lines) if "QUEUED" in l)
        completed_idx = next(i for i, l in enumerate(lines) if "COMPLETED" in l)
        assert queued_idx < completed_idx

    def test_no_events_message(self):
        with _mock_get_response({"events": []}):
            output = cmd_log("owner/repo", 999)
        assert "No CI events found" in output

    def test_handles_daemon_error(self):
        from cli.http_client import DaemonError
        with mock.patch("cli.daemon_cmd.get", side_effect=DaemonError("daemon down")):
            output = cmd_log("owner/repo", 1)
        assert "Error: daemon down" in output


# ═══════════════════════════════════════════════════════════════════════════
# ci history (FR-41)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdHistory:
    def test_shows_merged_prs(self, conn):
        _seed_data(conn)
        output = cmd_history("owner/repo", db=conn)
        assert "owner/repo" in output
        assert "old/z" in output

    def test_no_closed_prs(self, conn):
        _seed_data(conn)
        conn.execute("DELETE FROM pull_requests WHERE state = 'MERGED'")
        conn.commit()
        output = cmd_history("owner/repo", db=conn)
        assert "No closed PRs" in output


# ═══════════════════════════════════════════════════════════════════════════
# ci usage (FR-42)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdUsage:
    def test_shows_rate_limits(self):
        data = {
            "rate_limit_remaining": 4997,
            "repos_tracked": 2,
            "mode": "ACTIVE",
            "stale_seconds": 30,
        }
        with _mock_get_response(data):
            output = cmd_usage()
        assert "4997" in output
        assert "2" in output

    def test_handles_daemon_error(self):
        from cli.http_client import DaemonError
        with mock.patch("cli.daemon_cmd.get", side_effect=DaemonError("daemon down")):
            output = cmd_usage()
        assert "Error: daemon down" in output


# ═══════════════════════════════════════════════════════════════════════════
# ci poll (FR-43)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdPoll:
    def test_poll_returns_result(self):
        data = {"message": "poll completed", "mode": "PR_CHANGED", "last_synced": 1700000000}
        with _mock_post_response(data):
            output = cmd_poll()
        assert "poll completed" in output
        assert "PR_CHANGED" in output

    def test_poll_handles_error(self):
        from cli.http_client import DaemonError
        with mock.patch("cli.daemon_cmd.post", side_effect=DaemonError("daemon down")):
            output = cmd_poll()
        assert "Error: daemon down" in output


# ═══════════════════════════════════════════════════════════════════════════
# ci register (FR-43 — direct SQLite)
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdRegister:
    def test_registers_new_repo(self, conn):
        _seed_data(conn)
        output = cmd_register(conn, "neworg/newrepo", "neworg")
        assert "Registered" in output
        assert "neworg/newrepo" in output

    def test_idempotent(self, conn):
        _seed_data(conn)
        output = cmd_register(conn, "owner/repo", "test")
        assert "already registered" in output


# ═══════════════════════════════════════════════════════════════════════════
# FR-38: All CLI commands use daemon HTTP RPC
# ═══════════════════════════════════════════════════════════════════════════

class TestFR38:
    def test_status_uses_http(self):
        """FR-38: cmd_status calls HTTP RPC, not SQLite."""
        with _mock_get_response({"prs": [], "mode": "INACTIVE"}):
            output = cmd_status()
        assert isinstance(output, str)

    def test_log_uses_http(self):
        """FR-38: cmd_log calls HTTP RPC, not SQLite."""
        with _mock_get_response({"events": []}):
            output = cmd_log("owner/repo", 1)
        assert isinstance(output, str)

    def test_usage_uses_http(self):
        """FR-38: cmd_usage calls HTTP RPC, not SQLite."""
        with _mock_get_response({"rate_limit_remaining": 5000, "repos_tracked": 0, "mode": "INACTIVE"}):
            output = cmd_usage()
        assert isinstance(output, str)

    def test_poll_uses_http(self):
        """FR-38: cmd_poll calls HTTP RPC, not SQLite."""
        with _mock_post_response({"message": "poll completed", "mode": "INACTIVE"}):
            output = cmd_poll()
        assert isinstance(output, str)


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR39_status(self):
        """FR-39: ci status renders open PRs + job states + mode via HTTP RPC."""
        data = {
            "prs": [
                {"owner_repo": "owner/repo", "pr_number": 1, "branch": "feat/x",
                 "mergeable": "MERGEABLE",
                 "jobs": [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}]},
            ],
            "mode": "INACTIVE",
        }
        with _mock_get_response(data):
            output = cmd_status()
        assert "owner/repo" in output
        assert "feat/x" in output
        assert "build" in output
        assert "Mode:" in output

    def test_FR40_log(self):
        """FR-40: ci log shows chronological ci_events via HTTP RPC."""
        data = {
            "events": [
                {"job": "build", "status": "QUEUED", "conclusion": None, "at": 1000},
                {"job": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "at": 1100},
            ],
        }
        with _mock_get_response(data):
            output = cmd_log("owner/repo", 1)
        assert "owner/repo#1" in output
        assert "build" in output
        assert "QUEUED" in output

    def test_FR41_history(self, conn):
        """FR-41: ci history shows closed PRs."""
        _seed_data(conn)
        output = cmd_history("owner/repo", db=conn)
        assert "closed PRs" in output
        assert "old/z" in output

    def test_FR43_register(self, conn):
        """FR-43: ci register adds repo."""
        _seed_data(conn)
        output = cmd_register(conn, "neworg/newrepo", "neworg")
        assert "Registered" in output


# ═══════════════════════════════════════════════════════════════════════════
# ATM CLI commands
# ═══════════════════════════════════════════════════════════════════════════

class TestAtmCli:
    """Tests for ATM CLI commands: set-notify, show-notify, status."""

    def test_set_notify_stores_member(self, conn):
        _seed_data(conn)
        from cli.daemon_cmd import cmd_atm_set_notify
        output = cmd_atm_set_notify(conn, "owner/repo", "rand")
        assert "set to rand" in output

    def test_set_notify_reset_clears_member(self, conn):
        _seed_data(conn)
        from cli.daemon_cmd import cmd_atm_set_notify
        cmd_atm_set_notify(conn, "owner/repo", "rand")
        output = cmd_atm_set_notify(conn, "owner/repo", "--reset")
        assert "reset to team-lead" in output

    def test_set_notify_rejects_invalid_name(self, conn):
        _seed_data(conn)
        from cli.daemon_cmd import cmd_atm_set_notify
        output = cmd_atm_set_notify(conn, "owner/repo", "bad name")
        assert "Invalid" in output

    def test_show_notify_reports_default(self, conn):
        _seed_data(conn)
        from cli.daemon_cmd import cmd_atm_show_notify
        output = cmd_atm_show_notify(conn, "owner/repo")
        assert "team-lead (default)" in output

    def test_show_notify_unknown_repo(self, conn):
        _seed_data(conn)
        from cli.daemon_cmd import cmd_atm_show_notify
        output = cmd_atm_show_notify(conn, "nonexistent/repo")
        assert "not registered" in output

    def test_atm_status_not_configured(self, monkeypatch):
        monkeypatch.delenv("ATM_TEAM", raising=False)
        monkeypatch.setattr("atm._atm_binary", lambda: None)
        from cli.daemon_cmd import cmd_atm_status
        output = cmd_atm_status()
        assert "NOT CONFIGURED" in output

    def test_atm_status_ready(self, monkeypatch):
        monkeypatch.setenv("ATM_TEAM", "hermes")
        monkeypatch.setenv("ATM_IDENTITY", "ci")
        monkeypatch.setattr("atm._atm_binary", lambda: "/usr/local/bin/atm")
        from cli.daemon_cmd import cmd_atm_status
        output = cmd_atm_status()
        assert "READY" in output


# ═══════════════════════════════════════════════════════════════════════════
# Fixture helpers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    """Temp SQLite DB with full schema."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = _db.ensure_db(db_path)
        yield c
        c.close()


def _seed_data(c: sqlite3.Connection):
    """Insert sample data for testing."""
    now = int(time.time())
    c.execute("INSERT OR IGNORE INTO repos (owner_repo, gh_account) VALUES ('owner/repo', 'test')")
    c.execute(
        "INSERT OR IGNORE INTO pull_requests (owner_repo, pr_number, branch, mergeable, state, updated_at) "
        "VALUES ('owner/repo', 3, 'old/z', 'MERGEABLE', 'MERGED', ?)", (now - 3600,)
    )
    c.commit()
