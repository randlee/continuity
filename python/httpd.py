"""HTTP server for daemon RPC — cache-aware query endpoint.

Runs as a daemon thread within the daemon process. The CLI communicates
via HTTP instead of direct SQLite reads. The daemon owns all cache logic:
stale requests trigger an immediate GraphQL poll before returning.

Endpoints:
    GET /status     — daemon health, mode, rate limit
    GET /prs        — all open PRs with CI status, freshness
    GET /prs/<owner>/<repo>/<num> — single PR details
    POST /poll      — trigger immediate poll, return fresh data

Design:
    - stdlib http.server only — zero dependencies
    - Daemon thread — non-blocking, shares db connection (WAL mode)
    - Cache: last_synced > 30s triggers on-demand GraphQL poll
    - Never exposes GraphQL to CLI — daemon is sole API consumer
"""

import json
import logging
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from constants import (
    STATUS_QUEUED, STATUS_IN_PROGRESS, STATUS_COMPLETED,
    PR_STATE_OPEN,
)

logger = logging.getLogger(__name__)

# Cache freshness threshold: how old before triggering on-demand poll
STALE_THRESHOLD_SECONDS = 30


class DaemonHandler(BaseHTTPRequestHandler):
    """HTTP request handler with access to daemon state."""

    # Set by the server factory before serving
    daemon_ref = None  # Daemon instance

    def log_message(self, format, *args):
        """Route to logging instead of stderr."""
        logger.debug("httpd: %s", format % args)

    def do_GET(self):
        path = self.path.rstrip("/")

        if path == "/status":
            self._handle_status()
        elif path == "/prs":
            self._handle_prs()
        elif path.startswith("/prs/") and path.count("/") == 3:
            parts = path.split("/")
            self._handle_pr_detail(parts[2], parts[3], parts[4])
        else:
            self._json_error(404, "not found")

    def do_POST(self):
        if self.path.rstrip("/") == "/poll":
            self._handle_poll()
        else:
            self._json_error(404, "not found")

    # ── Handlers ────────────────────────────────────────────────────────

    def _handle_status(self):
        d = self.daemon_ref
        self._json_response({
            "mode": d.mode.value,
            "rate_limit_remaining": d._min_rate_limit_remaining(),
            "repos_tracked": len(d._get_repos("_")),
        })

    def _handle_prs(self):
        d = self.daemon_ref
        now = int(time.time())

        # Check cache freshness
        if self._data_is_stale(d.db, now):
            self._refresh_data(d, now)

        prs = self._query_prs(d.db)
        self._json_response({
            "prs": prs,
            "mode": d.mode.value,
            "last_synced": self._get_max_last_synced(d.db),
        })

    def _handle_pr_detail(self, owner: str, repo: str, pr_num: str):
        d = self.daemon_ref
        try:
            pr_num = int(pr_num)
        except ValueError:
            self._json_error(400, "invalid PR number")
            return

        owner_repo = f"{owner}/{repo}"
        pr_num_int = int(pr_num)
        now = int(time.time())

        if self._data_is_stale_for_repo(d.db, owner_repo, now):
            self._refresh_data(d, now)

        pr = self._query_pr_detail(d.db, owner_repo, pr_num)
        if pr is None:
            self._json_error(404, f"PR {owner_repo}#{pr_num} not found")
            return

        self._json_response(pr)

    def _handle_poll(self):
        d = self.daemon_ref
        now = int(time.time())
        self._refresh_data(d, now)
        self._json_response({
            "status": "ok",
            "last_synced": self._get_max_last_synced(d.db),
        })

    # ── Cache logic ─────────────────────────────────────────────────────

    def _data_is_stale(self, db: sqlite3.Connection, now: int) -> bool:
        """True if any tracked repo has stale data."""
        row = db.execute(
            "SELECT MAX(last_synced) FROM repos"
        ).fetchone()
        if not row or not row[0]:
            return True  # never synced
        return (now - row[0]) > STALE_THRESHOLD_SECONDS

    def _data_is_stale_for_repo(self, db: sqlite3.Connection,
                                 owner_repo: str, now: int) -> bool:
        row = db.execute(
            "SELECT last_synced FROM repos WHERE owner_repo = ?",
            (owner_repo,),
        ).fetchone()
        if not row or not row[0]:
            return True
        return (now - row[0]) > STALE_THRESHOLD_SECONDS

    def _get_max_last_synced(self, db: sqlite3.Connection) -> int | None:
        row = db.execute("SELECT MAX(last_synced) FROM repos").fetchone()
        return row[0] if row else None

    def _refresh_data(self, d, now: int):
        """Trigger an immediate poll cycle. Blocks until complete."""
        try:
            d._poll_cycle()
        except Exception:
            logger.exception("httpd: on-demand poll failed")

    # ── Queries ─────────────────────────────────────────────────────────

    def _query_prs(self, db: sqlite3.Connection) -> list[dict]:
        """Return all open PRs with current CI job states."""
        prs = []
        rows = db.execute(
            "SELECT owner_repo, pr_number, branch, mergeable, state "
            "FROM pull_requests WHERE state = ? ORDER BY owner_repo, pr_number",
            (PR_STATE_OPEN,),
        ).fetchall()

        for owner_repo, pr_num, branch, mergeable, state in rows:
            jobs = db.execute(
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

    def _query_pr_detail(self, db: sqlite3.Connection,
                          owner_repo: str, pr_num: int) -> dict | None:
        row = db.execute(
            "SELECT branch, mergeable, state FROM pull_requests "
            "WHERE owner_repo = ? AND pr_number = ?",
            (owner_repo, pr_num),
        ).fetchone()
        if not row:
            return None

        events = db.execute(
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

    # ── Helpers ─────────────────────────────────────────────────────────

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: int, message: str):
        self._json_response({"error": message}, status)


def start_httpd(daemon, port: int = 9119) -> HTTPServer:
    """Start HTTP server in a daemon thread. Returns the server instance."""
    # Inject daemon reference into the handler class
    handler = type("Handler", (DaemonHandler,), {"daemon_ref": daemon})

    server = HTTPServer(("127.0.0.1", port), handler)
    thread = Thread(target=server.serve_forever, daemon=True, name="continuity-httpd")
    thread.start()
    logger.info("httpd listening on 127.0.0.1:%d", port)
    return server