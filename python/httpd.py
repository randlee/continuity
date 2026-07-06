"""HTTP server for daemon RPC — cache-aware query endpoint.

Runs as a daemon thread within the daemon process. The CLI communicates
via HTTP instead of direct SQLite reads. The daemon owns all cache logic:
stale requests trigger an immediate GraphQL poll before returning.

Endpoints:
    GET /health     — daemon liveness (no DB access)
    GET /status     — daemon mode, rate limit, repo count, last_synced
    GET /prs        — all open PRs with CI status, freshness
    GET /prs/<owner>/<repo>/<num> — single PR details
    POST /poll      — trigger immediate poll, return fresh data

Design:
    - stdlib http.server only — zero dependencies
    - Daemon thread — non-blocking, own SQLite connection (WAL-safe reads)
    - Write lock: _poll_cycle serialized via daemon._poll_lock
    - Cache: last_synced > 30s triggers on-demand GraphQL poll
    - Timeouts: all handlers have 30s deadline; stale data returned on timeout
    - Discriminated union: every response has {"status": "ok"} or {"status": "error", "error": "..."}
"""

import json
import logging
import socket
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

from constants import (
    PR_STATE_OPEN,
)

logger = logging.getLogger(__name__)

# Cache freshness threshold: how old before triggering on-demand poll
STALE_THRESHOLD_SECONDS = 30
# HTTP handler timeout: max wait for on-demand poll
HANDLER_TIMEOUT = 30  # seconds


class DaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler with access to daemon state."""

    # Set by the server factory before serving
    daemon_ref = None  # Daemon instance
    # Own SQLite connection (thread-safe reads via WAL)
    db_conn: sqlite3.Connection | None = None

    def log_message(self, format, *args):
        logger.debug("httpd: %s", format % args)

    def do_GET(self):
        if self.daemon_ref is None:
            self._json_error(503, "daemon not ready")
            return

        path = self.path.rstrip("/")

        if path == "/health":
            self._json_ok({"mode": self.daemon_ref.mode.value})
        elif path == "/status":
            self._handle_status()
        elif path == "/prs":
            self._handle_prs()
        elif path.startswith("/prs/") and path.count("/") == 4:
            parts = path.split("/")
            self._handle_pr_detail(parts[2], parts[3], parts[4])
        else:
            self._json_error(404, "not found")

    def do_POST(self):
        if self.daemon_ref is None:
            self._json_error(503, "daemon not ready")
            return

        if self.path.rstrip("/") == "/poll":
            self._handle_poll()
        else:
            self._json_error(404, "not found")

    # ── Handlers ────────────────────────────────────────────────────────

    def _handle_status(self):
        d = self.daemon_ref
        last_synced = self._get_max_last_synced()
        self._json_ok({
            "mode": d.mode.value,
            "rate_limit_remaining": d._min_rate_limit_remaining(),
            "repos_tracked": self._count_repos(),
            "last_synced": last_synced,
            "stale_seconds": (int(time.time()) - last_synced) if last_synced else None,
        })

    def _handle_prs(self):
        d = self.daemon_ref
        now = int(time.time())

        refreshed = True
        if self._data_is_stale(now):
            ok, err = self._refresh_data(d, now)
            refreshed = ok

        prs = self._query_prs()
        response = {
            "prs": prs,
            "mode": d.mode.value,
            "last_synced": self._get_max_last_synced(),
            "refreshed": refreshed,
        }
        if not refreshed:
            response["warning"] = "data may be stale — poll timed out"
        self._json_ok(response)

    def _handle_pr_detail(self, owner: str, repo: str, pr_num_str: str):
        d = self.daemon_ref
        try:
            pr_num = int(pr_num_str)
        except ValueError:
            self._json_error(400, "invalid PR number")
            return

        owner_repo = f"{owner}/{repo}"
        now = int(time.time())

        if self._data_is_stale_for_repo(owner_repo, now):
            ok, err = self._refresh_data(d, now)
            if not ok:
                self._json_error(504, f"poll timed out, returning stale data: {err}")

        pr = self._query_pr_detail(owner_repo, pr_num)
        if pr is None:
            self._json_error(404, f"PR {owner_repo}#{pr_num} not found")
            return

        self._json_ok(pr)

    def _handle_poll(self):
        d = self.daemon_ref
        now = int(time.time())
        ok, err = self._refresh_data(d, now)
        if not ok:
            self._json_error(504, f"poll failed: {err}")
            return
        self._json_ok({
            "message": "poll completed",
            "last_synced": self._get_max_last_synced(),
        })

    # ── Cache logic ─────────────────────────────────────────────────────

    def _data_is_stale(self, now: int) -> bool:
        """True if any tracked repo has stale data."""
        row = self.db_conn.execute(
            "SELECT MAX(last_synced) FROM repos"
        ).fetchone()
        if not row or not row[0]:
            return True
        return (now - row[0]) > STALE_THRESHOLD_SECONDS

    def _data_is_stale_for_repo(self, owner_repo: str, now: int) -> bool:
        row = self.db_conn.execute(
            "SELECT last_synced FROM repos WHERE owner_repo = ?",
            (owner_repo,),
        ).fetchone()
        if not row or not row[0]:
            return True
        return (now - row[0]) > STALE_THRESHOLD_SECONDS

    def _get_max_last_synced(self) -> int | None:
        row = self.db_conn.execute(
            "SELECT MAX(last_synced) FROM repos"
        ).fetchone()
        return row[0] if row else None

    def _count_repos(self) -> int:
        row = self.db_conn.execute(
            "SELECT COUNT(*) FROM repos"
        ).fetchone()
        return row[0] if row else 0

    def _refresh_data(self, d, now: int) -> tuple[bool, str]:
        """Trigger an immediate poll cycle with timeout.

        Returns (ok, error_message). On timeout, returns (False, reason)
        so the caller can serve stale data.
        """
        acquired = d._poll_lock.acquire(timeout=HANDLER_TIMEOUT)
        if not acquired:
            return (False, "write lock busy — poll in progress")

        try:
            d._poll_cycle()
            return (True, "")
        except Exception as exc:
            logger.exception("httpd: on-demand poll failed")
            return (False, str(exc))
        finally:
            d._poll_lock.release()

    # ── Queries (own SQLite connection, thread-safe reads via WAL) ──────

    def _query_prs(self) -> list[dict]:
        """Return all open PRs with current CI job states."""
        prs = []
        rows = self.db_conn.execute(
            "SELECT owner_repo, pr_number, branch, mergeable, state "
            "FROM pull_requests WHERE state = ? ORDER BY owner_repo, pr_number",
            (PR_STATE_OPEN,),
        ).fetchall()

        for owner_repo, pr_num, branch, mergeable, state in rows:
            jobs = self.db_conn.execute(
                "SELECT job_name, status, conclusion FROM ci_events "
                "WHERE owner_repo = ? AND pr_number = ? "
                "GROUP BY job_name HAVING recorded_at = MAX(recorded_at)",
                (owner_repo, pr_num),
            ).fetchall()

            prs.append({
                "owner_repo": owner_repo,
                "pr_number": pr_num,
                "branch": branch,
                "mergeable": mergeable,
                "state": state,
                "jobs": [
                    {"name": j[0], "status": j[1], "conclusion": j[2]}
                    for j in jobs
                ],
            })
        return prs

    def _query_pr_detail(self, owner_repo: str,
                          pr_num: int) -> dict | None:
        row = self.db_conn.execute(
            "SELECT branch, mergeable, state FROM pull_requests "
            "WHERE owner_repo = ? AND pr_number = ?",
            (owner_repo, pr_num),
        ).fetchone()
        if not row:
            return None

        events = self.db_conn.execute(
            "SELECT job_name, status, conclusion, recorded_at "
            "FROM ci_events WHERE owner_repo = ? AND pr_number = ? "
            "ORDER BY recorded_at ASC",
            (owner_repo, pr_num),
        ).fetchall()

        return {
            "owner_repo": owner_repo,
            "pr_number": pr_num,
            "branch": row[0],
            "mergeable": row[1],
            "state": row[2],
            "events": [
                {"job": e[0], "status": e[1], "conclusion": e[2], "at": e[3]}
                for e in events
            ],
        }

    # ── Response helpers ────────────────────────────────────────────────

    def _json_ok(self, data: dict):
        """Send a successful response with status=ok wrapper."""
        data["status"] = "ok"
        self._send_json(data, 200)

    def _json_error(self, status: int, message: str):
        """Send an error response with discriminated union."""
        self._send_json({"status": "error", "error": message}, status)

    def _send_json(self, data: dict, status: int):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_httpd(daemon, db_path: Path, port: int = 9119) -> HTTPServer:
    """Start HTTP server in a daemon thread. Returns the server instance.

    Creates a dedicated SQLite connection for the HTTP handler thread
    (avoids sqlite3 cross-thread errors). The daemon's _poll_lock
    serializes writes.
    """
    # Create thread-safe SQLite connection for HTTP handler
    db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute("PRAGMA busy_timeout=2000")

    # Inject daemon reference and DB connection into the handler class
    handler = type("Handler", (DaemonHandler,), {
        "daemon_ref": daemon,
        "db_conn": db_conn,
    })

    server = HTTPServer(("127.0.0.1", port), handler)
    server.allow_reuse_address = True

    thread = Thread(
        target=server.serve_forever, daemon=True, name="continuity-httpd",
    )
    thread.start()
    logger.info("httpd listening on 127.0.0.1:%d", port)
    return server