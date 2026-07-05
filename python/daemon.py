"""Daemon — poll loop orchestrator + singleton lifecycle (Phase 4.3).

Orchestrates polling: queries GitHub via GhClient, diffs results against
DB, writes ci_events/pull_requests, manages adaptive mode transitions.

Singleton: PID file + lock directory (cross-platform, no fcntl).
Testable with mocked GhClient.

Public API:
    Daemon(home, gh_clients, db)
    .start()      — acquire lock, enter poll loop
    .shutdown()   — SIGTERM handler, release lock, flush
"""

import json
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from diff import diff_jobs, diff_prs, diff_conflicts, CiEvent
from gh.client import GhClient


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class ActivityMode(Enum):
    ACTIVE = "ACTIVE"       # CI running: 30s interval
    WATCHFUL = "WATCHFUL"   # Open PRs, no CI: 5min interval
    IDLE = "IDLE"           # No open PRs: 30min interval


@dataclass
class DaemonConfig:
    active_interval: int = 30
    watchful_interval: int = 300
    idle_interval: int = 1800
    low_water: int = 500          # rate limit remaining threshold
    max_backoff: int = 3600       # max interval when rate limited
    backoff_multiplier: float = 2.0


# ═══════════════════════════════════════════════════════════════════════════
# Daemon
# ═══════════════════════════════════════════════════════════════════════════

class Daemon:
    """Poll loop orchestrator. One instance per CONTINUITY_HOME."""

    def __init__(
        self,
        home: Path,
        clients: dict[str, GhClient],  # account → client
        db: sqlite3.Connection,
        config: DaemonConfig | None = None,
    ):
        self.home = Path(home)
        self.clients = clients
        self.db = db
        self.config = config or DaemonConfig()
        self.mode = ActivityMode.IDLE
        self._lock_dir = self.home / "daemon.lock"
        self._shutdown_flag = False
        self._wake_event = False
        self._pid_file = self.home / "daemon.pid"

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Acquire singleton lock, write PID, enter poll loop."""
        self.home.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self._write_pid()
        self._setup_signals()

        try:
            self._run_loop()
        finally:
            self._cleanup()

    def shutdown(self, signum=None, frame=None):
        """SIGTERM handler. Sets shutdown flag for graceful exit."""
        self._shutdown_flag = True

    def _wake(self, signum=None, frame=None):
        """SIGUSR1 handler. Wakes daemon for immediate poll."""
        self._wake_event = True

    # ── Singleton guard ──────────────────────────────────────────────────

    def _acquire_lock(self):
        """Acquire exclusive lock via lock directory (cross-platform).
        os.mkdir is atomic — only one process can create the directory."""
        try:
            self._lock_dir.mkdir()
        except FileExistsError:
            # Lock held — check if the holder is alive
            pid = self._read_pid()
            if pid and _is_pid_alive(pid):
                raise RuntimeError(
                    f"Daemon already running (PID {pid}). "
                    f"Lock dir: {self._lock_dir}"
                )
            # Stale lock — remove and retry
            self._lock_dir.rmdir()
            self._lock_dir.mkdir()

    def _write_pid(self):
        """Write current PID to pid file."""
        self._pid_file.write_text(str(os.getpid()))

    def _read_pid(self) -> int | None:
        """Read PID from pid file, if it exists."""
        try:
            return int(self._pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _cleanup(self):
        """Release lock, remove PID file."""
        try:
            self._lock_dir.rmdir()
        except (FileNotFoundError, OSError):
            pass
        self._pid_file.unlink(missing_ok=True)

    # ── Signal handling ──────────────────────────────────────────────────

    def _setup_signals(self):
        """Register SIGTERM and SIGUSR1 handlers."""
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGUSR1, self._wake)

    # ── Poll loop ────────────────────────────────────────────────────────

    def _run_loop(self):
        """Main poll loop. Exits on SIGTERM."""
        while not self._shutdown_flag:
            interval = self._next_interval()
            self._sleep_interruptible(interval)

            if self._shutdown_flag:
                break

            self._wake_event = False
            self._poll_cycle()
            self._recalculate_mode()

    def _sleep_interruptible(self, seconds: float):
        """Sleep, but wake on SIGUSR1 or shutdown."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._shutdown_flag or self._wake_event:
                return
            time.sleep(min(0.5, deadline - time.time()))

    # ── Poll cycle ───────────────────────────────────────────────────────

    def _poll_cycle(self):
        """Query all accounts, diff results, write to DB."""
        now = int(time.time())

        for account, client in self.clients.items():
            repos = self._get_repos(account)
            if not repos:
                continue

            try:
                result = client.poll(repos)
            except Exception:
                continue  # transient failure — retry next cycle

            self._apply_result(result, now)

        self.db.commit()

    def _get_repos(self, account: str) -> list[str]:
        """Get tracked repos for an account."""
        rows = self.db.execute(
            "SELECT owner_repo FROM repos WHERE gh_account = ?", (account,)
        ).fetchall()
        return [r[0] for r in rows]

    def _apply_result(self, result, now: int):
        """Diff poll result against DB, write events."""
        from gh.client import ApiUsage

        # Update API usage
        rl = result.rate_limit
        self.db.execute(
            "INSERT INTO api_usage (gh_account, queried_at, cost, remaining, reset_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("_", now, rl.cost, rl.remaining, rl.reset_at),
        )

        for owner_repo, prs in result.repos.items():
            # Build current state for diffing
            current_jobs = self._load_current_jobs(owner_repo)
            current_prs = self._load_current_prs(owner_repo)

            # Diff jobs
            incoming_jobs = [(owner_repo, pr.number, pr.checks) for pr in prs]
            events = diff_jobs(incoming_jobs, current_jobs)
            for e in events:
                self.db.execute(
                    "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (e.owner_repo, e.pr_number, e.job_name, e.status, e.conclusion, now),
                )

            # Diff PRs
            pr_diff = diff_prs(prs, current_prs)
            for pr in pr_diff.added:
                self.db.execute(
                    "INSERT OR REPLACE INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner_repo, pr.number, "", pr.state, now),
                )
            for pr in pr_diff.updated:
                self.db.execute(
                    "UPDATE pull_requests SET state=?, mergeable=?, updated_at=? "
                    "WHERE owner_repo=? AND pr_number=?",
                    (pr.state, pr.mergeable or "UNKNOWN", now, owner_repo, pr.number),
                )
            for pr in pr_diff.closed:
                self.db.execute(
                    "UPDATE pull_requests SET state='CLOSED', updated_at=? "
                    "WHERE owner_repo=? AND pr_number=?",
                    (now, owner_repo, pr.number),
                )

            # Conflict detection (FR-37)
            conflicts = diff_conflicts(owner_repo, prs, current_prs)
            for repo_name, pr_num in conflicts:
                self.db.execute(
                    "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                    "VALUES (?, ?, 'merge', 'COMPLETED', 'CONFLICT', ?)",
                    (repo_name, pr_num, now),
                )

    def _load_current_jobs(self, owner_repo: str) -> dict:
        """Load latest ci_events per (repo, pr, job) for diffing."""
        rows = self.db.execute(
            "SELECT pr_number, job_name, status, conclusion "
            "FROM ci_events WHERE owner_repo = ? "
            "GROUP BY pr_number, job_name "
            "HAVING recorded_at = MAX(recorded_at)",
            (owner_repo,),
        ).fetchall()
        result = {}
        for pr_num, job_name, status, conclusion in rows:
            key = (owner_repo, pr_num, job_name)
            result[key] = CiEvent(
                owner_repo=owner_repo, pr_number=pr_num,
                job_name=job_name, status=status, conclusion=conclusion,
            )
        return result

    def _load_current_prs(self, owner_repo: str) -> dict:
        """Load current PR state for diffing."""
        from diff import PrState
        rows = self.db.execute(
            "SELECT pr_number, state, mergeable FROM pull_requests WHERE owner_repo = ?",
            (owner_repo,),
        ).fetchall()
        return {r[0]: PrState(number=r[0], state=r[1], mergeable=r[2] or "UNKNOWN")
                for r in rows}

    # ── Adaptive mode (FR-31, FR-32) ─────────────────────────────────────

    def _next_interval(self) -> int:
        """Calculate next poll interval based on mode and rate limits."""
        base = {
            ActivityMode.ACTIVE: self.config.active_interval,
            ActivityMode.WATCHFUL: self.config.watchful_interval,
            ActivityMode.IDLE: self.config.idle_interval,
        }[self.mode]

        # FR-36: Rate limit backoff
        min_remaining = self._min_rate_limit_remaining()
        if min_remaining < self.config.low_water:
            base = int(base * self.config.backoff_multiplier)
            if base > self.config.max_backoff:
                base = self.config.max_backoff

        return base

    def _min_rate_limit_remaining(self) -> int:
        """Find the lowest rate limit remaining across all clients."""
        limits = [c.rate_limit.remaining for c in self.clients.values()]
        return min(limits) if limits else 5000

    def _recalculate_mode(self):
        """FR-32: Re-evaluate activity mode after each poll cycle."""
        has_active_ci = False
        has_open_prs = False

        rows = self.db.execute(
            "SELECT status FROM ci_events "
            "GROUP BY owner_repo, pr_number, job_name "
            "HAVING recorded_at = MAX(recorded_at)"
        ).fetchall()

        for (status,) in rows:
            if status in ("QUEUED", "IN_PROGRESS"):
                has_active_ci = True
            has_open_prs = True

        if not rows:
            # Also check pull_requests for open PRs
            open_count = self.db.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE state = 'OPEN'"
            ).fetchone()[0]
            has_open_prs = open_count > 0

        if has_active_ci:
            self.mode = ActivityMode.ACTIVE
        elif has_open_prs:
            self.mode = ActivityMode.WATCHFUL
        else:
            self.mode = ActivityMode.IDLE


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive. Cross-platform."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False