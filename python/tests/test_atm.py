"""Tests for atm.py — ATM notification module.

Tests: helpers (formatting, permanent failure detection, binary resolution),
atm_configured, atm_get_designated, atm_notify routing/retry/fallback/batching.
All pure-function helpers tested with corner cases.

Uses mocking for subprocess calls — no real atm CLI needed.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import atm as _atm
import db as _db


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conn():
    """Temp SQLite DB with repos table and designated_member column."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = _db.ensure_db(db_path)
        c.execute(
            "INSERT INTO repos (owner_repo, gh_account, designated_member) "
            "VALUES ('owner/repo', 'test', NULL)"
        )
        c.execute(
            "INSERT INTO repos (owner_repo, gh_account, designated_member) "
            "VALUES ('other/repo', 'test', 'custom-agent')"
        )
        c.commit()
        yield c
        c.close()


@pytest.fixture
def atm_env(monkeypatch):
    """Set ATM_TEAM for tests."""
    monkeypatch.setenv("ATM_TEAM", "hermes")


@pytest.fixture
def mock_atm_binary(atm_env, monkeypatch):
    """Mock atm binary on PATH."""
    monkeypatch.setattr(_atm, "_atm_binary", lambda: "/usr/local/bin/atm")


# ═══════════════════════════════════════════════════════════════════════════
# _atm_binary
# ═══════════════════════════════════════════════════════════════════════════

def test_atm_binary_on_path(monkeypatch):
    """Returns path when atm is on PATH."""
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/usr/local/bin/atm")
    assert _atm._atm_binary() == "/usr/local/bin/atm"


def test_atm_binary_not_found(monkeypatch):
    """Returns None when atm is not on PATH."""
    monkeypatch.setattr(_atm.shutil, "which", lambda _: None)
    assert _atm._atm_binary() is None


# ═══════════════════════════════════════════════════════════════════════════
# _is_permanent_failure
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stderr,expected", [
    ("rand is not a member of team hermes", True),
    ("agent 'rand' not found in team", True),
    ("unknown member: rand", True),
    ("no such member rand in hermes", True),
    ("send failed: rand is not in team roster", True),
    ("socket timeout", False),
    ("connection refused", False),
    ("", False),
    ("random error message", False),
])
def test_is_permanent_failure(stderr, expected):
    """Correctly identifies permanent roster failures vs transient errors."""
    assert _atm._is_permanent_failure(stderr) == expected


# ═══════════════════════════════════════════════════════════════════════════
# _format_identity_header
# ═══════════════════════════════════════════════════════════════════════════

def test_identity_header_with_requestor():
    """Includes 'on behalf of' when requested_by is set."""
    assert _atm._format_identity_header("rand") == "From: ci (on behalf of rand)"


def test_identity_header_none():
    """No 'on behalf of' when requested_by is None."""
    assert _atm._format_identity_header(None) == "From: ci"


def test_identity_header_empty_string():
    """Empty string treated same as None."""
    assert _atm._format_identity_header("") == "From: ci"


# ═══════════════════════════════════════════════════════════════════════════
# format_conflict_files
# ═══════════════════════════════════════════════════════════════════════════

def test_conflict_files_empty():
    """Empty list returns empty string."""
    assert _atm.format_conflict_files([]) == ""


def test_conflict_files_single():
    """Single file, no count."""
    result = _atm.format_conflict_files(["src/a.rs"])
    assert result == "  src/a.rs"


def test_conflict_files_multiple_under_max():
    """Multiple files under display cap."""
    result = _atm.format_conflict_files(["a.rs", "b.rs", "c.rs"])
    assert "a.rs" in result
    assert "b.rs" in result
    assert "c.rs" in result
    assert "more" not in result
    assert result.count("\n") == 2  # 3 lines


def test_conflict_files_exactly_max():
    """Exactly at max display, no '(N more)'."""
    result = _atm.format_conflict_files(
        ["a.rs", "b.rs", "c.rs", "d.rs", "e.rs", "f.rs"],
        max_display=6,
    )
    assert "more" not in result
    assert result.count("\n") == 5  # 6 lines


def test_conflict_files_over_max():
    """Over max display, includes '(N more)'."""
    result = _atm.format_conflict_files(
        ["a.rs", "b.rs", "c.rs", "d.rs", "e.rs", "f.rs", "g.rs", "h.rs"],
        max_display=6,
    )
    assert "(2 more)" in result
    assert result.count("\n") == 6  # 6 files + 1 "(2 more)" = 7 lines


def test_conflict_files_custom_max():
    """Custom max_display works."""
    result = _atm.format_conflict_files(
        ["a.rs", "b.rs", "c.rs", "d.rs"],
        max_display=3,
    )
    assert "(1 more)" in result


# ═══════════════════════════════════════════════════════════════════════════
# format_notification
# ═══════════════════════════════════════════════════════════════════════════

def test_format_notification_with_identity():
    """Includes identity header and body."""
    result = _atm.format_notification("rand", "PR #1 failed", "build failed")
    assert "From: ci (on behalf of rand)" in result
    assert "PR #1 failed" in result
    assert "build failed" in result


def test_format_notification_without_identity():
    """No 'on behalf of' when no identity."""
    result = _atm.format_notification(None, "PR #1 failed", "build failed")
    assert "From: ci" in result
    assert "on behalf of" not in result


# ═══════════════════════════════════════════════════════════════════════════
# format_batch_notification
# ═══════════════════════════════════════════════════════════════════════════

def test_format_batch_empty():
    """Empty list returns empty string."""
    assert _atm.format_batch_notification("rand", []) == ""


def test_format_batch_single():
    """Single notification uses simple format (no batching header)."""
    result = _atm.format_batch_notification(
        "rand", [("PR #1 failed", "build failed")],
    )
    assert "PR #1 failed" in result
    assert "events" not in result  # no batch header


def test_format_batch_multiple():
    """Multiple notifications get batching header."""
    result = _atm.format_batch_notification(
        "rand",
        [
            ("PR #1 unmergable", "conflict in src/a.rs"),
            ("PR #2 CI failed", "build: FAILURE"),
        ],
    )
    assert "2 events" in result
    assert "PR #1 unmergable" in result
    assert "PR #2 CI failed" in result
    assert "conflict in src/a.rs" in result
    assert "build: FAILURE" in result


def test_format_batch_no_identity():
    """Batching works without requesting identity."""
    result = _atm.format_batch_notification(
        None,
        [("CI slow", "build taking too long"), ("CI timeout", "test hung")],
    )
    assert "From: ci" in result
    assert "on behalf of" not in result
    assert "2 events" in result


# ═══════════════════════════════════════════════════════════════════════════
# atm_configured
# ═══════════════════════════════════════════════════════════════════════════

def test_configured_with_team_and_binary(atm_env, mock_atm_binary):
    """True when ATM_TEAM is set and atm is on PATH."""
    assert _atm.atm_configured() is True


def test_configured_without_team(monkeypatch):
    """False when ATM_TEAM is not set."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    assert _atm.atm_configured() is False


def test_configured_without_binary(atm_env, monkeypatch):
    """False when atm binary is not found."""
    monkeypatch.setattr(_atm, "_atm_binary", lambda: None)
    assert _atm.atm_configured() is False


# ═══════════════════════════════════════════════════════════════════════════
# _atm_send
# ═══════════════════════════════════════════════════════════════════════════

def _mock_run(returncode=0, stderr=""):
    """Create a mock subprocess.run result."""
    return mock.Mock(returncode=returncode, stderr=stderr)


def test_send_success(mock_atm_binary, monkeypatch):
    """Returns (True, False) on successful send."""
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: _mock_run(0))
    delivered, permanent = _atm._atm_send("rand", "test message")
    assert delivered is True
    assert permanent is False


def test_send_permanent_roster_failure(mock_atm_binary, monkeypatch):
    """Returns (False, True) when member is not in roster."""
    monkeypatch.setattr(
        _atm.subprocess, "run",
        lambda *a, **kw: _mock_run(1, "rand is not a member of team hermes"),
    )
    delivered, permanent = _atm._atm_send("rand", "test message")
    assert delivered is False
    assert permanent is True


def test_send_transient_failure(mock_atm_binary, monkeypatch):
    """Returns (False, False) on non-roster non-zero exit."""
    monkeypatch.setattr(
        _atm.subprocess, "run",
        lambda *a, **kw: _mock_run(1, "socket timeout"),
    )
    delivered, permanent = _atm._atm_send("rand", "test message")
    assert delivered is False
    assert permanent is False


def test_send_timeout(mock_atm_binary, monkeypatch):
    """Returns (False, False) on subprocess timeout."""
    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="atm", timeout=5)
    monkeypatch.setattr(_atm.subprocess, "run", _raise_timeout)
    delivered, permanent = _atm._atm_send("rand", "test message")
    assert delivered is False
    assert permanent is False


def test_send_not_configured(monkeypatch):
    """Returns (False, True) when ATM is not configured."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    delivered, permanent = _atm._atm_send("rand", "test message")
    assert delivered is False
    assert permanent is True


# ═══════════════════════════════════════════════════════════════════════════
# atm_get_designated
# ═══════════════════════════════════════════════════════════════════════════

def test_get_designated_null_returns_team_lead(conn):
    """NULL designated_member returns 'team-lead'."""
    result = _atm.atm_get_designated(conn, "owner/repo")
    assert result == "team-lead"


def test_get_designated_custom_value(conn):
    """Stored value is returned as-is."""
    result = _atm.atm_get_designated(conn, "other/repo")
    assert result == "custom-agent"


def test_get_designated_unknown_repo(conn):
    """Unknown repo returns team-lead."""
    result = _atm.atm_get_designated(conn, "nonexistent/repo")
    assert result == "team-lead"


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_target
# ═══════════════════════════════════════════════════════════════════════════

def test_resolve_target_with_identity(conn):
    """Returns the requesting identity when set."""
    result = _atm._resolve_target(conn, "owner/repo", "rand")
    assert result == "rand"


def test_resolve_target_none_uses_designated(conn):
    """Returns designated member when requested_by is None."""
    result = _atm._resolve_target(conn, "owner/repo", None)
    assert result == "team-lead"


def test_resolve_target_ci_uses_designated(conn):
    """Returns designated member when requested_by is 'ci'."""
    result = _atm._resolve_target(conn, "owner/repo", "ci")
    assert result == "team-lead"


def test_resolve_target_empty_string_uses_designated(conn):
    """Empty string treated as None → designated member."""
    result = _atm._resolve_target(conn, "owner/repo", "")
    assert result == "team-lead"


# ═══════════════════════════════════════════════════════════════════════════
# atm_notify — routing and batching
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_noop_when_not_configured(conn, monkeypatch):
    """Returns True immediately when ATM not configured."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    result = _atm.atm_notify(conn, "owner/repo", "rand", [("s", "b")])
    assert result is True


def test_notify_empty_notifications(conn, mock_atm_binary):
    """Returns True when notification list is empty."""
    result = _atm.atm_notify(conn, "owner/repo", "rand", [])
    assert result is True


def test_notify_sends_to_requested_by(conn, mock_atm_binary, monkeypatch):
    """When requested_by is set, sends to that member."""
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: _mock_run(0))
    result = _atm.atm_notify(
        conn, "owner/repo", "rand",
        [("PR #1 failed", "build failed")],
    )
    assert result is True


def test_notify_routes_none_to_designated(conn, mock_atm_binary, monkeypatch):
    """When requested_by is None, routes to designated member."""
    calls = []

    def _fake_run(*a, **kw):
        # subprocess.run([atm_path, "send", to, message], ...)
        to = a[0][2]  # third element of command list
        calls.append(to)
        return _mock_run(0)

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    _atm.atm_notify(conn, "owner/repo", None, [("s", "b")])
    assert calls == ["team-lead"]


def test_notify_batches_multiple(conn, mock_atm_binary, monkeypatch):
    """Multiple notifications are batched into one message."""
    messages_sent = []

    def _fake_run(*a, **kw):
        # subprocess.run([atm_path, "send", to, message], ...)
        message = a[0][3]  # fourth element of command list
        messages_sent.append(message)
        return _mock_run(0)

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    _atm.atm_notify(
        conn, "owner/repo", "rand",
        [
            ("PR #1 unmergable", "conflict in a.rs"),
            ("PR #2 CI failed", "build: FAILURE"),
        ],
    )
    assert len(messages_sent) == 1
    assert "2 events" in messages_sent[0]
    assert "PR #1 unmergable" in messages_sent[0]
    assert "PR #2 CI failed" in messages_sent[0]


# ═══════════════════════════════════════════════════════════════════════════
# atm_notify — fallback
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_falls_back_on_permanent_failure(conn, mock_atm_binary, monkeypatch):
    """When target is not in roster, falls back to team-lead."""
    calls = []

    def _fake_run(*a, **kw):
        to = a[0][2]
        calls.append(to)
        if to == "rand":
            return _mock_run(1, "rand is not a member")
        return _mock_run(0)

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    result = _atm.atm_notify(
        conn, "owner/repo", "rand", [("s", "b")],
    )
    assert result is True
    assert calls == ["rand", "team-lead"]


def test_notify_terminal_when_both_fail(conn, mock_atm_binary, monkeypatch):
    """Returns False when both target and team-lead fail permanently."""
    monkeypatch.setattr(
        _atm.subprocess, "run",
        lambda *a, **kw: _mock_run(1, "not a member"),
    )
    result = _atm.atm_notify(
        conn, "owner/repo", "rand", [("s", "b")],
    )
    assert result is False


def test_notify_no_double_fallback(conn, mock_atm_binary, monkeypatch):
    """When target is team-lead, don't fall back to itself."""
    calls = []

    def _fake_run(*a, **kw):
        to = a[0][2]
        calls.append(to)
        return _mock_run(1, "not a member")

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    result = _atm.atm_notify(
        conn, "owner/repo", "team-lead", [("s", "b")],
    )
    assert result is False
    assert calls == ["team-lead"]


# ═══════════════════════════════════════════════════════════════════════════
# atm_notify — retry
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_retries_transient(conn, mock_atm_binary, monkeypatch):
    """Retries up to 3x on transient failures, then succeeds."""
    calls = []

    def _fake_run(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            return _mock_run(1, "socket timeout")
        return _mock_run(0)

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    monkeypatch.setattr(_atm.time, "sleep", lambda _: None)
    result = _atm.atm_notify(
        conn, "owner/repo", "rand", [("s", "b")],
    )
    assert result is True
    assert len(calls) == 3


def test_notify_retries_exhausted_falls_back(conn, mock_atm_binary, monkeypatch):
    """After 4 attempts, falls back to team-lead."""
    targets = []

    def _fake_run(*a, **kw):
        to = a[0][2]
        targets.append(to)
        if to == "rand":
            return _mock_run(1, "socket timeout")
        return _mock_run(0)

    monkeypatch.setattr(_atm.subprocess, "run", _fake_run)
    monkeypatch.setattr(_atm.time, "sleep", lambda _: None)
    result = _atm.atm_notify(
        conn, "owner/repo", "rand", [("s", "b")],
    )
    assert result is True
    assert targets.count("rand") == 4
    assert "team-lead" in targets
