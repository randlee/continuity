"""Tests for notify.py — notification dispatch layer.

Tests: event formatting, dispatch grouping/batching, identity resolution.
All tests mock atm_notify — no real atm CLI needed.
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
import notify as _notify
from notify import (
    PrCreatedUnmergable, PrBecameUnmergable, CascadeUnmergable,
    CiCompleted, CiSlow, CiTimeout,
    dispatch_notifications, resolve_pr_identity, resolve_push_identity,
    _format_event, _format_duration,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    """Temp SQLite DB with test data."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = _db.ensure_db(db_path)
        c.execute(
            "INSERT INTO repos (owner_repo, gh_account, designated_member) "
            "VALUES ('owner/repo', 'test', NULL)"
        )
        now = int(time.time())
        c.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, "
            "requested_by, updated_at) VALUES ('owner/repo', 1, 'feat/x', "
            "'OPEN', 'rand', ?)", (now,)
        )
        c.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, "
            "requested_by, updated_at) VALUES ('owner/repo', 2, 'fix/y', "
            "'OPEN', NULL, ?)", (now,)
        )
        c.commit()
        yield c
        c.close()


@pytest.fixture
def atm_env(monkeypatch):
    """Set ATM_TEAM for tests."""
    monkeypatch.setenv("ATM_TEAM", "hermes")


# ═══════════════════════════════════════════════════════════════════════════
# Event formatting
# ═══════════════════════════════════════════════════════════════════════════

def test_format_pr_created_unmergable():
    event = PrCreatedUnmergable("o/r", 42, "rand", ["src/a.rs", "src/b.rs"])
    subject, body = _format_event(event)
    assert subject == "PR #42 unmergable"
    assert "src/a.rs" in body
    assert "src/b.rs" in body


def test_format_pr_created_unmergable_no_files():
    event = PrCreatedUnmergable("o/r", 42, "rand", [])
    subject, body = _format_event(event)
    assert "PR #42" in subject
    assert "check GitHub" in body


def test_format_pr_became_unmergable():
    event = PrBecameUnmergable("o/r", 42, "rand", "abc1234", ["src/x.rs"])
    subject, body = _format_event(event)
    assert "PR #42 unmergable after push" in subject
    assert "abc1234" in body
    assert "src/x.rs" in body


def test_format_cascade_unmergable():
    event = CascadeUnmergable("o/r", 55, 42, ["src/a.rs"])
    subject, body = _format_event(event)
    assert "PR #55 now unmergable" == subject
    assert "PR #42 merged" in body
    assert "src/a.rs" in body


def test_format_ci_completed_success():
    event = CiCompleted("o/r", 42, "SUCCESS")
    subject, body = _format_event(event)
    assert "PR #42 CI passed" == subject
    assert "all checks passed" in body


def test_format_ci_completed_failure():
    event = CiCompleted("o/r", 42, "FAILURE", ["build", "test"])
    subject, body = _format_event(event)
    assert "PR #42 CI failed" == subject
    assert "build, test failed" in body


def test_format_ci_slow():
    event = CiSlow("o/r", 42, "build", 600, 120.0)
    subject, body = _format_event(event)
    assert "PR #42 CI slow" == subject
    assert "build" in body
    assert "10m" in body   # 600s
    assert "2m" in body    # 120s


def test_format_ci_timeout():
    event = CiTimeout("o/r", 42, "test", 1800)
    subject, body = _format_event(event)
    assert "PR #42 CI timeout" == subject
    assert "test" in body
    assert "30m" in body
    assert "hung" in body


def test_format_unknown_event_type():
    """Raises ValueError for unknown event types."""
    with pytest.raises(ValueError):
        _format_event(mock.Mock(spec=[]))  # not a NotificationEvent subclass


# ═══════════════════════════════════════════════════════════════════════════
# Event requested_by property
# ═══════════════════════════════════════════════════════════════════════════

def test_pr_created_requested_by():
    e = PrCreatedUnmergable("o/r", 1, "rand", [])
    assert e.requested_by == "rand"


def test_pr_became_requested_by():
    e = PrBecameUnmergable("o/r", 1, "agent-x", "abc")
    assert e.requested_by == "agent-x"


def test_cascade_requested_by_none():
    """Cascade events always have None requested_by (route to team-lead)."""
    e = CascadeUnmergable("o/r", 1, 2)
    assert e.requested_by is None


def test_ci_completed_requested_by_none():
    e = CiCompleted("o/r", 1, "SUCCESS")
    assert e.requested_by is None


def test_ci_slow_requested_by_none():
    e = CiSlow("o/r", 1, "build", 300, 150.0)
    assert e.requested_by is None


def test_ci_timeout_requested_by_none():
    e = CiTimeout("o/r", 1, "build", 1800)
    assert e.requested_by is None


# ═══════════════════════════════════════════════════════════════════════════
# Identity resolution
# ═══════════════════════════════════════════════════════════════════════════

def test_resolve_pr_identity_found(conn):
    """Returns requested_by from pull_requests table."""
    ident = resolve_pr_identity(conn, "owner/repo", 1)
    assert ident == "rand"


def test_resolve_pr_identity_null(conn):
    """Returns None when requested_by is NULL."""
    ident = resolve_pr_identity(conn, "owner/repo", 2)
    assert ident is None


def test_resolve_pr_identity_unknown_pr(conn):
    """Returns None for unknown PR."""
    ident = resolve_pr_identity(conn, "owner/repo", 999)
    assert ident is None


def test_resolve_push_identity_found(conn):
    """Extracts atm_identity from cli_events args_json."""
    import json
    now = int(time.time())
    conn.execute(
        "INSERT INTO cli_events (command, args_json, exit_code, duration_ms, recorded_at) "
        "VALUES ('git-push', ?, 0, 0, ?)",
        (json.dumps({"remote": "origin", "ref": "feat/x", "atm_identity": "pusher"}), now),
    )
    conn.commit()
    ident = resolve_push_identity(conn, "owner/repo")
    assert ident == "pusher"


def test_resolve_push_identity_no_events(conn):
    """Returns None when no git-push events exist."""
    ident = resolve_push_identity(conn, "owner/repo")
    assert ident is None


def test_resolve_push_identity_no_atm_identity(conn):
    """Returns None when atm_identity is not in args_json."""
    import json
    now = int(time.time())
    conn.execute(
        "INSERT INTO cli_events (command, args_json, exit_code, duration_ms, recorded_at) "
        "VALUES ('git-push', ?, 0, 0, ?)",
        (json.dumps({"remote": "origin", "ref": "main"}), now),
    )
    conn.commit()
    ident = resolve_push_identity(conn, "owner/repo")
    assert ident is None


def test_resolve_push_identity_malformed_json(conn):
    """Returns None when args_json is malformed."""
    import json
    now = int(time.time())
    conn.execute(
        "INSERT INTO cli_events (command, args_json, exit_code, duration_ms, recorded_at) "
        "VALUES ('git-push', 'not-valid-json', 0, 0, ?)",
        (now,),
    )
    conn.commit()
    ident = resolve_push_identity(conn, "owner/repo")
    assert ident is None


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════════════════════════════════════

def test_dispatch_empty_events(conn, atm_env):
    """No-op when event list is empty."""
    dispatch_notifications(conn, [])  # should not raise


def test_dispatch_groups_by_repo_and_identity(conn, atm_env, monkeypatch):
    """Events with different owners/identities are dispatched separately."""
    calls = []

    def _fake_notify(db, owner_repo, requested_by, notifications):
        calls.append((owner_repo, requested_by, len(notifications)))
        return True

    monkeypatch.setattr(_notify, "atm_notify", _fake_notify)
    monkeypatch.setattr(_notify, "atm_configured", lambda: True)

    events = [
        PrCreatedUnmergable("owner/repo", 1, "rand", []),
        PrCreatedUnmergable("owner/repo", 2, "rand", []),  # same group
        CiCompleted("owner/repo", 1, "SUCCESS"),           # different requested_by (None)
        PrCreatedUnmergable("other/repo", 1, "rand", []),  # different repo
    ]

    dispatch_notifications(conn, events)
    # Wait for thread to complete
    import time as _time
    _time.sleep(0.1)

    # Should have 3 groups:
    # ("owner/repo", "rand", 2 events)
    # ("owner/repo", None, 1 event)
    # ("other/repo", "rand", 1 event)
    assert len(calls) == 3

    # Verify grouping
    groups = {(r, i): n for r, i, n in calls}
    assert groups[("owner/repo", "rand")] == 2
    assert groups[("owner/repo", None)] == 1
    assert groups[("other/repo", "rand")] == 1


def test_dispatch_not_configured(conn, monkeypatch):
    """No-op when ATM is not configured."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    monkeypatch.setattr(_notify, "atm_configured", lambda: False)
    dispatch_notifications(conn, [CiCompleted("o/r", 1, "SUCCESS")])


# ═══════════════════════════════════════════════════════════════════════════
# _format_duration
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatDuration:
    def test_zero_seconds(self):
        assert _format_duration(0) == "0s"

    def test_under_minute(self):
        assert _format_duration(30) == "30s"
        assert _format_duration(59) == "59s"

    def test_exactly_one_minute(self):
        assert _format_duration(60) == "1m"

    def test_minutes(self):
        assert _format_duration(120) == "2m"
        assert _format_duration(3599) == "59m"

    def test_exactly_one_hour(self):
        assert _format_duration(3600) == "1h0m"

    def test_hours_and_minutes(self):
        assert _format_duration(3660) == "1h1m"
        assert _format_duration(7200) == "2h0m"
        assert _format_duration(5430) == "1h30m"
