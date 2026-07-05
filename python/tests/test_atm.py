"""Tests for atm.py — ATM notification module.

Tests: atm_configured, atm_send, atm_get_designated, atm_notify
routing, retry/backoff, fallback chain, and edge cases.

Uses mocking for subprocess calls — no real atm CLI needed.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
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


# ═══════════════════════════════════════════════════════════════════════════
# atm_configured
# ═══════════════════════════════════════════════════════════════════════════

def test_configured_with_team_and_binary(atm_env, monkeypatch):
    """True when ATM_TEAM is set and atm is on PATH."""
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")
    assert _atm.atm_configured() is True


def test_configured_without_team(monkeypatch):
    """False when ATM_TEAM is not set."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    assert _atm.atm_configured() is False


def test_configured_without_binary(atm_env, monkeypatch):
    """False when atm binary is not found."""
    monkeypatch.setattr(_atm.shutil, "which", lambda _: None)
    monkeypatch.setattr(_atm.os.path, "isfile", lambda _: False)
    assert _atm.atm_configured() is False


# ═══════════════════════════════════════════════════════════════════════════
# atm_send
# ═══════════════════════════════════════════════════════════════════════════

def test_send_success(atm_env, monkeypatch):
    """Returns (True, False) on successful send."""
    mock_run = mock.Mock(returncode=0, stderr="")
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: mock_run)
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")

    delivered, permanent = _atm.atm_send("rand", "test subject", "test body")
    assert delivered is True
    assert permanent is False


def test_send_permanent_roster_failure(atm_env, monkeypatch):
    """Returns (False, True) when member is not in roster."""
    mock_run = mock.Mock(returncode=1, stderr="rand is not a member of team hermes")
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: mock_run)
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")

    delivered, permanent = _atm.atm_send("rand", "test", "body")
    assert delivered is False
    assert permanent is True


def test_send_transient_failure(atm_env, monkeypatch):
    """Returns (False, False) on non-roster non-zero exit."""
    mock_run = mock.Mock(returncode=1, stderr="socket timeout")
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: mock_run)
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")

    delivered, permanent = _atm.atm_send("rand", "test", "body")
    assert delivered is False
    assert permanent is False


def test_send_timeout(atm_env, monkeypatch):
    """Returns (False, False) on subprocess timeout."""
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="atm", timeout=5)

    monkeypatch.setattr(_atm.subprocess, "run", _raise_timeout)

    delivered, permanent = _atm.atm_send("rand", "test", "body")
    assert delivered is False
    assert permanent is False


def test_send_not_configured(monkeypatch):
    """Returns (False, True) when ATM is not configured."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    delivered, permanent = _atm.atm_send("rand", "test", "body")
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
# atm_notify — routing
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_noop_when_not_configured(conn, monkeypatch):
    """Returns True immediately when ATM not configured."""
    monkeypatch.delenv("ATM_TEAM", raising=False)
    result = _atm.atm_notify(conn, "owner/repo", "rand", "subj", "body")
    assert result is True  # no-op, not an error


def test_notify_sends_to_target(conn, atm_env, monkeypatch):
    """Sends to target when ATM_IDENTITY is set."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append((to, subject, body))
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    result = _atm.atm_notify(conn, "owner/repo", "rand", "PR #1 failed", "details")
    assert result is True
    assert len(calls) == 1
    assert calls[0][0] == "rand"
    assert "PR #1 failed" in calls[0][1]


def test_notify_ci_identity_treated_as_none(conn, atm_env, monkeypatch):
    """target='ci' routes to designated member (team-lead for NULL)."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    _atm.atm_notify(conn, "owner/repo", "ci", "subj", "body")
    assert calls == ["team-lead"]


def test_notify_none_target_uses_designated(conn, atm_env, monkeypatch):
    """target=None routes to designated member."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    _atm.atm_notify(conn, "owner/repo", None, "subj", "body")
    assert calls == ["team-lead"]


def test_notify_custom_designated(conn, atm_env, monkeypatch):
    """Uses custom designated member when set on repos table."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    _atm.atm_notify(conn, "other/repo", None, "subj", "body")
    assert calls == ["custom-agent"]


# ═══════════════════════════════════════════════════════════════════════════
# atm_notify — fallback
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_falls_back_to_team_lead_on_permanent_failure(conn, atm_env, monkeypatch):
    """When target is not in roster, falls back to team-lead."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        if to == "rand":
            return (False, True)  # permanent failure
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    result = _atm.atm_notify(conn, "owner/repo", "rand", "subj", "body")
    assert result is True
    assert calls == ["rand", "team-lead"]


def test_notify_terminal_when_both_fail(conn, atm_env, monkeypatch):
    """Returns False when both target and team-lead fail permanently."""
    def _fake_send(to, subject, body):
        return (False, True)  # always permanent failure

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    result = _atm.atm_notify(conn, "owner/repo", "rand", "subj", "body")
    assert result is False  # terminal


def test_notify_no_double_fallback_when_target_is_team_lead(conn, atm_env, monkeypatch):
    """When target is already team-lead, don't fall back to itself."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        return (False, True)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)

    result = _atm.atm_notify(conn, "owner/repo", "team-lead", "subj", "body")
    assert result is False
    assert calls == ["team-lead"]  # only one attempt, no fallback loop


# ═══════════════════════════════════════════════════════════════════════════
# atm_notify — retry
# ═══════════════════════════════════════════════════════════════════════════

def test_notify_retries_transient_failures(conn, atm_env, monkeypatch):
    """Retries up to 3x on transient failures, then succeeds."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        if len(calls) < 3:
            return (False, False)  # transient
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)
    monkeypatch.setattr(_atm.time, "sleep", lambda _: None)  # skip backoff

    result = _atm.atm_notify(conn, "owner/repo", "rand", "subj", "body")
    assert result is True
    assert calls == ["rand", "rand", "rand"]


def test_notify_retries_exhausted_falls_back(conn, atm_env, monkeypatch):
    """After 4 attempts (1 initial + 3 retries), falls back to team-lead."""
    calls = []

    def _fake_send(to, subject, body):
        calls.append(to)
        if to == "rand":
            return (False, False)  # always transient
        return (True, False)

    monkeypatch.setattr(_atm, "atm_send", _fake_send)
    monkeypatch.setattr(_atm.time, "sleep", lambda _: None)

    result = _atm.atm_notify(conn, "owner/repo", "rand", "subj", "body")
    assert result is True
    assert calls == ["rand", "rand", "rand", "rand", "team-lead"]


# ═══════════════════════════════════════════════════════════════════════════
# Permanent failure pattern matching
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stderr", [
    "rand is not a member of team hermes",
    "agent 'rand' not found in team",
    "unknown member: rand",
    "no such member rand in hermes",
    "send failed: rand is not in team roster",
])
def test_permanent_patterns_detected(stderr, atm_env, monkeypatch):
    """Various permanent failure messages are detected."""
    mock_run = mock.Mock(returncode=1, stderr=stderr)
    monkeypatch.setattr(_atm.subprocess, "run", lambda *a, **kw: mock_run)
    monkeypatch.setattr(_atm.shutil, "which", lambda _: "/opt/homebrew/bin/atm")

    delivered, permanent = _atm.atm_send("rand", "test", "body")
    assert delivered is False
    assert permanent is True
