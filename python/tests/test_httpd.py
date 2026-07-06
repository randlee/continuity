"""Tests for httpd.py — HTTP RPC server.

Tests: all endpoint handlers, cache staleness, timeouts, error responses,
discriminated union format, daemon_ref null guard, routing.

Tests internal handler methods directly (bypass HTTP parsing) to avoid
BaseHTTPRequestHandler's socket dependency.
"""

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db
import httpd as _httpd
from httpd import DaemonHandler, STALE_THRESHOLD_SECONDS


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_path():
    """Temp SQLite DB with schema and test data."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = Path(td) / "test.db"
        conn = _db.ensure_db(p)
        now = int(time.time())
        conn.execute(
            "INSERT INTO repos (owner_repo, gh_account, last_synced) "
            "VALUES ('o/r', 'test', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, "
            "mergeable, state, updated_at) "
            "VALUES ('o/r', 1, 'feat/x', 'MERGEABLE', 'OPEN', ?)", (now,)
        )
        conn.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, "
            "status, conclusion, recorded_at) "
            "VALUES ('o/r', 1, 'build', 'COMPLETED', 'SUCCESS', ?)", (now,)
        )
        conn.commit()
        conn.close()
        yield p


def _make_handler(db_path, daemon=None):
    """Create a handler with mock daemon, real DB, mocked HTTP I/O."""
    if daemon is None:
        daemon = mock.MagicMock()
        daemon.mode = mock.MagicMock()
        daemon.mode.value = "INACTIVE"
        daemon._min_rate_limit_remaining.return_value = 4500
        daemon._poll_lock = mock.MagicMock()
        daemon._poll_lock.acquire.return_value = True
        daemon._poll_cycle.return_value = None

    db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    db_conn.execute("PRAGMA journal_mode=WAL")

    # Create handler with all HTTP I/O mocked
    h = DaemonHandler.__new__(DaemonHandler)
    h.daemon_ref = daemon
    h.db_conn = db_conn
    h.wfile = mock.MagicMock()
    h.send_response = mock.MagicMock()
    h.send_header = mock.MagicMock()
    h.end_headers = mock.MagicMock()
    h.path = "/"
    return h


def _get_json(h: DaemonHandler) -> dict:
    """Extract JSON body written to wfile."""
    calls = h.wfile.write.call_args_list
    if not calls:
        return {}
    body = b"".join(c[0][0] for c in calls)
    return json.loads(body)


# ═══════════════════════════════════════════════════════════════════════════
# Health + Status
# ═══════════════════════════════════════════════════════════════════════════

def test_health_endpoint(db_path):
    h = _make_handler(db_path)
    h.path = "/health"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert "mode" in data

def test_status_endpoint(db_path):
    h = _make_handler(db_path)
    h.path = "/status"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert data["repos_tracked"] == 1
    assert data["last_synced"] is not None

def test_status_counts_all_repos(db_path):
    """repos_tracked counts repos across all accounts."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO repos (owner_repo, gh_account) "
        "VALUES ('other/r', 'randlee@github.com')"
    )
    conn.commit()
    conn.close()

    h = _make_handler(db_path)
    h.path = "/status"
    h.do_GET()
    data = _get_json(h)
    assert data["repos_tracked"] == 2

def test_status_includes_stale_info(db_path):
    h = _make_handler(db_path)
    h.path = "/status"
    h.do_GET()
    data = _get_json(h)
    assert "stale_seconds" in data


# ═══════════════════════════════════════════════════════════════════════════
# PR listing
# ═══════════════════════════════════════════════════════════════════════════

def test_prs_returns_prs(db_path):
    h = _make_handler(db_path)
    h.path = "/prs"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert len(data["prs"]) == 1
    assert data["prs"][0]["pr_number"] == 1

def test_prs_includes_jobs(db_path):
    h = _make_handler(db_path)
    h.path = "/prs"
    h.do_GET()
    data = _get_json(h)
    jobs = data["prs"][0]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["name"] == "build"
    assert jobs[0]["conclusion"] == "SUCCESS"


# ═══════════════════════════════════════════════════════════════════════════
# PR detail
# ═══════════════════════════════════════════════════════════════════════════

def test_pr_detail_returns_events(db_path):
    h = _make_handler(db_path)
    h.path = "/prs/o/r/1"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert data["pr_number"] == 1
    assert len(data["events"]) == 1

def test_pr_detail_not_found(db_path):
    h = _make_handler(db_path)
    h.path = "/prs/o/r/999"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "error"
    assert "not found" in data["error"]

def test_pr_detail_invalid_number(db_path):
    h = _make_handler(db_path)
    h.path = "/prs/o/r/abc"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "error"
    assert "invalid PR number" in data["error"]


# ═══════════════════════════════════════════════════════════════════════════
# Poll
# ═══════════════════════════════════════════════════════════════════════════

def test_poll_success(db_path):
    h = _make_handler(db_path)
    h.path = "/poll"
    h.do_POST()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert "poll completed" in data["message"]


# ═══════════════════════════════════════════════════════════════════════════
# Routing
# ═══════════════════════════════════════════════════════════════════════════

def test_get_unknown_path_404(db_path):
    h = _make_handler(db_path)
    h.path = "/nonexistent"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "error"

def test_post_unknown_path_404(db_path):
    h = _make_handler(db_path)
    h.path = "/nonexistent"
    h.do_POST()
    data = _get_json(h)
    assert data["status"] == "error"


# ═══════════════════════════════════════════════════════════════════════════
# daemon_ref null guard
# ═══════════════════════════════════════════════════════════════════════════

def test_handler_rejects_when_daemon_ref_none(db_path):
    """Returns 503 when daemon is not ready."""
    db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    h = DaemonHandler.__new__(DaemonHandler)
    h.daemon_ref = None
    h.db_conn = db_conn
    h.wfile = mock.MagicMock()
    h.send_response = mock.MagicMock()
    h.send_header = mock.MagicMock()
    h.end_headers = mock.MagicMock()
    h.path = "/status"

    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "error"
    assert "not ready" in data["error"]


# ═══════════════════════════════════════════════════════════════════════════
# Cache staleness
# ═══════════════════════════════════════════════════════════════════════════

def test_data_is_stale_when_never_synced(db_path):
    """Stale when no repos have last_synced."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE repos SET last_synced = NULL")
    conn.commit()
    conn.close()

    h = _make_handler(db_path)
    assert h._data_is_stale(int(time.time())) is True

def test_data_is_fresh_when_recently_synced(db_path):
    """Not stale when last_synced is within threshold."""
    h = _make_handler(db_path)
    assert h._data_is_stale(int(time.time())) is False

def test_data_is_stale_when_beyond_threshold(db_path):
    """Stale when last_synced is older than threshold."""
    old_time = int(time.time()) - STALE_THRESHOLD_SECONDS - 10
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE repos SET last_synced = ?", (old_time,))
    conn.commit()
    conn.close()

    h = _make_handler(db_path)
    assert h._data_is_stale(int(time.time())) is True


# ═══════════════════════════════════════════════════════════════════════════
# Refresh timeout
# ═══════════════════════════════════════════════════════════════════════════

def test_refresh_data_lock_timeout(db_path):
    """Returns (False, reason) when poll lock can't be acquired."""
    h = _make_handler(db_path)
    h.daemon_ref._poll_lock.acquire.return_value = False

    ok, err = h._refresh_data(h.daemon_ref, int(time.time()))
    assert ok is False
    assert "busy" in err.lower() or "lock" in err.lower()

def test_refresh_data_poll_failure(db_path):
    """Returns (False, reason) when poll raises."""
    h = _make_handler(db_path)
    h.daemon_ref._poll_cycle.side_effect = RuntimeError("API down")

    ok, err = h._refresh_data(h.daemon_ref, int(time.time()))
    assert ok is False
    assert "API down" in err

def test_prs_serves_stale_on_refresh_timeout(db_path):
    """When poll times out, returns PRs with warning flag."""
    old_time = int(time.time()) - STALE_THRESHOLD_SECONDS - 10
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE repos SET last_synced = ?", (old_time,))
    conn.commit()
    conn.close()

    h = _make_handler(db_path)
    h.daemon_ref._poll_lock.acquire.return_value = False
    h.path = "/prs"
    h.do_GET()
    data = _get_json(h)
    assert data["status"] == "ok"
    assert data["refreshed"] is False
    assert "warning" in data
    assert "stale" in data["warning"]
    assert len(data["prs"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
# _count_repos
# ═══════════════════════════════════════════════════════════════════════════

def test_count_repos_zero_when_empty(db_path):
    """Returns 0 when no repos."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM repos")
    conn.commit()
    conn.close()

    h = _make_handler(db_path)
    assert h._count_repos() == 0