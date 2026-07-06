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
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from diff import diff_jobs, diff_prs, diff_conflicts, CiEvent
from gh.client import GhClient
from notify import (
    PrCreatedUnmergable, PrBecameUnmergable, CascadeUnmergable,
    CiCompleted, CiSlow, CiTimeout, NotificationEvent,
    dispatch_notifications, resolve_pr_identity, resolve_push_identity,
)
from constants import (
    STATUS_COMPLETED, STATUS_QUEUED, STATUS_IN_PROGRESS,
    MERGEABLE_CONFLICTING, MERGEABLE_UNKNOWN,
    PR_STATE_OPEN, PR_STATE_MERGED, PR_STATE_CLOSED,
    CONCLUSION_SUCCESS, CONCLUSION_FAILURE,
    TERMINAL_STATUSES, ACTIVE_STATUSES,
)


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class ActivityMode(Enum):
    PR_CHANGED = "PR_CHANGED"  # post-push inspection: 30s
    ACTIVE = "ACTIVE"          # CI running: 5 min
    INACTIVE = "INACTIVE"      # nothing happening: 20 min


@dataclass
class DaemonConfig:
    pr_changed_interval: int = 30
    active_interval: int = 300           # 5 min (ADR-21)
    inactive_interval: int = 1200         # 20 min
    post_push_delay: int = 60             # 1 min (ADR-20)
    low_water: int = 1000                 # rate limit remaining threshold
    max_backoff: int = 3600               # max interval when rate limited
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
        self.mode = ActivityMode.INACTIVE
        self._lock_dir = self.home / "daemon.lock"
        self._shutdown_flag = False
        self._wake_event = False
        self._scheduled_wake_at: float = 0.0
        self._pid_file = self.home / "daemon.pid"
        # Monitor state: EMA tracking per (repo, job_name)
        self._ema: dict[tuple[str, str], float] = {}
        self._ema_count: dict[tuple[str, str], int] = {}
        # Dedup: prevent duplicate slow/timeout notifications per CI run
        self._notified_monitor: set[tuple[str, int, str, str]] = set()
        self._poll_lock = threading.Lock()
        self._httpd = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Acquire singleton lock, write PID, enter poll loop."""
        self.home.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self._write_pid()
        self._setup_signals()

        # Start HTTP server for CLI RPC
        from httpd import start_httpd
        db_path = Path(os.environ.get(
            "CONTINUITY_DB",
            str(Path.home() / ".local" / "share" / "continuity" / "continuity.db"),
        ))
        self._httpd = start_httpd(self, db_path)

        try:
            self._run_loop()
        finally:
            self._cleanup()

    def shutdown(self, signum=None, frame=None):
        """SIGTERM handler. Sets shutdown flag for graceful exit."""
        self._shutdown_flag = True

    def _wake(self, signum=None, frame=None):
        """SIGUSR1 handler. Immediately enters PR_CHANGED mode.

        ADR-20: POST_PUSH_DELAY = 1 min. GitHub takes ~1 min to compute
        is_mergeable after a push. The first SIGUSR1 sets the wake time;
        subsequent signals within the delay window are ignored (first-wins).
        """
        now = time.time()
        if self._wake_event and self._scheduled_wake_at > now:
            return  # first-wins: already scheduled, ignore
        self._scheduled_wake_at = now + self.config.post_push_delay
        self._wake_event = True
        self.mode = ActivityMode.PR_CHANGED

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
            self._scheduled_wake_at = 0.0
            with self._poll_lock:
                self._poll_cycle()
            self._recalculate_mode()


    def _sleep_interruptible(self, seconds: float):
        """Sleep until seconds elapsed, scheduled wake, or shutdown."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._shutdown_flag:
                return
            if self._wake_event and time.time() >= self._scheduled_wake_at:
                return
            remaining = min(
                deadline - time.time(),
                self._scheduled_wake_at - time.time() if self._wake_event else 999,
                0.5,
            )
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.5))

    # ── Poll cycle ───────────────────────────────────────────────────────

    # EMA configuration
    EMA_ALPHA = 0.2
    EMA_MIN_SAMPLES = 3
    SLOW_THRESHOLD = 2.0   # 2× EMA
    HUNG_THRESHOLD = 5.0   # 5× EMA

    def _poll_cycle(self):
        """Query all accounts, diff results, write to DB, dispatch notifications."""
        now = int(time.time())
        notify_events: list[NotificationEvent] = []

        for account, client in self.clients.items():
            repos = self._get_repos(account)
            if not repos:
                continue

            try:
                result = client.poll(repos)
            except Exception:
                continue  # transient failure — retry next cycle

            events = self._apply_result(result, now)
            notify_events.extend(events)

        self.db.commit()

        # Dispatch notifications in a spawned thread (non-blocking)
        if notify_events:
            dispatch_notifications(self.db, notify_events)

    def _get_repos(self, account: str) -> list[str]:
        """Get tracked repos for an account."""
        rows = self.db.execute(
            "SELECT owner_repo FROM repos WHERE gh_account = ?", (account,)
        ).fetchall()
        return [r[0] for r in rows]

    def _apply_result(self, result, now: int) -> list[NotificationEvent]:
        """Diff poll result against DB, write events, return notification events."""
        from gh.client import ApiUsage
        notify_events: list[NotificationEvent] = []

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

            # Track merged PRs for cascade detection
            merged_prs: list[int] = []

            # Diff jobs
            incoming_jobs = [(owner_repo, pr.number, pr.checks) for pr in prs]
            events = diff_jobs(incoming_jobs, current_jobs)
            for e in events:
                self.db.execute(
                    "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (e.owner_repo, e.pr_number, e.job_name, e.status, e.conclusion, now),
                )

                # Detect CI completion
                if e.status == STATUS_COMPLETED:
                    _add_ci_completion(notify_events, owner_repo, e.pr_number,
                                       self.db, current_jobs)
                    # Update EMA on successful completion
                    if e.conclusion == CONCLUSION_SUCCESS:
                        self._update_ema(owner_repo, e.job_name, now)

            # Diff PRs
            pr_diff = diff_prs(prs, current_prs)
            for pr in pr_diff.added:
                self.db.execute(
                    "INSERT OR REPLACE INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner_repo, pr.number, "", pr.state, now),
                )
                # New PR: check if unmergable
                if pr.mergeable == MERGEABLE_CONFLICTING:
                    identity = resolve_pr_identity(self.db, owner_repo, pr.number)
                    if identity is None:
                        identity = resolve_push_identity(self.db, owner_repo)
                    notify_events.append(PrCreatedUnmergable(
                        owner_repo, pr.number, identity, [],
                    ))

            for pr in pr_diff.updated:
                self.db.execute(
                    "UPDATE pull_requests SET state=?, mergeable=?, updated_at=? "
                    "WHERE owner_repo=? AND pr_number=?",
                    (pr.state, pr.mergeable or MERGEABLE_UNKNOWN, now, owner_repo, pr.number),
                )

                # Detect PR merged (for cascade later)
                prev = current_prs.get(pr.number)
                if pr.state in (PR_STATE_MERGED, PR_STATE_CLOSED) and prev and prev.state == PR_STATE_OPEN:
                    merged_prs.append(pr.number)

                # Detect PR became unmergable after a push
                if (prev and prev.mergeable != MERGEABLE_CONFLICTING
                        and pr.mergeable == MERGEABLE_CONFLICTING
                        and prev.state == PR_STATE_OPEN):
                    identity = resolve_push_identity(self.db, owner_repo)
                    if identity is None:
                        identity = resolve_pr_identity(self.db, owner_repo, pr.number)
                    notify_events.append(PrBecameUnmergable(
                        owner_repo, pr.number, identity,
                    ))

            for pr in pr_diff.closed:
                self.db.execute(
                    "UPDATE pull_requests SET state = ? , updated_at=? "
                    "WHERE owner_repo=? AND pr_number=?",
                    (PR_STATE_CLOSED, now, owner_repo, pr.number),
                )

            # Conflict detection (FR-37): new conflicts
            conflicts = diff_conflicts(owner_repo, prs, current_prs)
            for repo_name, pr_num in conflicts:
                self.db.execute(
                    "INSERT INTO ci_events (owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                    "VALUES (?, ?, 'merge', 'COMPLETED', 'CONFLICT', ?)",
                    (repo_name, pr_num, now),
                )

            # Update last_synced timestamp for cache freshness
            self.db.execute(
                "UPDATE repos SET last_synced = ? WHERE owner_repo = ?",
                (now, owner_repo),
            )

            # Cascade detection: merged PRs may have caused other PRs to conflict
            if merged_prs:
                for repo_name, pr_num in conflicts:
                    # For each newly conflicting PR, check if it was caused by a merge
                    for merged_num in merged_prs:
                        if pr_num != merged_num:
                            notify_events.append(CascadeUnmergable(
                                repo_name, pr_num, merged_num,
                            ))
                            break  # one cascade event per PR

        return notify_events

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

    # ── Monitor: EMA tracking and slow/timeout detection ────────────────

    def _update_ema(self, owner_repo: str, job_name: str, now: int) -> None:
        """Update EMA for a job that completed successfully.

        Calculates execution time from the IN_PROGRESS→COMPLETED delta
        in ci_events, then updates the running EMA.
        Requires EMA_MIN_SAMPLES before thresholds apply.
        """
        key = (owner_repo, job_name)

        # Find the IN_PROGRESS timestamp for this job run
        started = self.db.execute(
            "SELECT recorded_at FROM ci_events "
            "WHERE owner_repo = ? AND job_name = ? AND status = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (owner_repo, job_name, STATUS_IN_PROGRESS),
        ).fetchone()

        # Find the COMPLETED timestamp (latest before now)
        completed = self.db.execute(
            "SELECT recorded_at FROM ci_events "
            "WHERE owner_repo = ? AND job_name = ? AND status = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (owner_repo, job_name, STATUS_COMPLETED),
        ).fetchone()

        if not started or not completed:
            return

        execution_s = completed[0] - started[0]
        if execution_s <= 0:
            return

        count = self._ema_count.get(key, 0) + 1
        self._ema_count[key] = count

        current = self._ema.get(key, 0.0)
        if count == 1:
            self._ema[key] = float(execution_s)  # seed with first value
        else:
            self._ema[key] = (
                self.EMA_ALPHA * execution_s +
                (1.0 - self.EMA_ALPHA) * current
            )

        # Clear dedup entries for this job (new CI run starting)
        to_remove = {
            k for k in self._notified_monitor
            if k[0] == owner_repo and k[2] == job_name
        }
        self._notified_monitor -= to_remove

    def _check_monitor(self, now: int) -> list[NotificationEvent]:
        """Check all IN_PROGRESS jobs for slow/hung conditions.

        Returns CiSlow/CiTimeout events. Deduplicated — each
        (repo, pr, job, event_type) fires at most once per CI run.
        """
        events: list[NotificationEvent] = []

        # Find all currently IN_PROGRESS jobs
        rows = self.db.execute(
            "SELECT ce.owner_repo, ce.pr_number, ce.job_name, ce.recorded_at "
            "FROM ci_events ce "
            "INNER JOIN ("
            "  SELECT owner_repo, pr_number, job_name, MAX(recorded_at) AS max_ts "
            "  FROM ci_events GROUP BY owner_repo, pr_number, job_name"
            ") latest "
            "ON ce.owner_repo = latest.owner_repo "
            "AND ce.pr_number = latest.pr_number "
            "AND ce.job_name = latest.job_name "
            "AND ce.recorded_at = latest.max_ts "
            "WHERE ce.status = ?",
            (STATUS_IN_PROGRESS,),
        ).fetchall()

        for owner_repo, pr_number, job_name, started_at in rows:
            elapsed = now - started_at
            key = (owner_repo, job_name)
            ema = self._ema.get(key)
            count = self._ema_count.get(key, 0)

            # Skip if no EMA baseline yet (need minimum samples)
            if ema is None or count < self.EMA_MIN_SAMPLES:
                continue

            # Check hung (timeout) — 5× EMA
            hung_key = (owner_repo, pr_number, job_name, "timeout")
            if elapsed > self.HUNG_THRESHOLD * ema:
                if hung_key not in self._notified_monitor:
                    self._notified_monitor.add(hung_key)
                    events.append(CiTimeout(
                        owner_repo, pr_number, job_name,
                        int(self.HUNG_THRESHOLD * ema),
                    ))
                continue  # don't also emit slow if already hung

            # Check slow — 2× EMA
            slow_key = (owner_repo, pr_number, job_name, "slow")
            if elapsed > self.SLOW_THRESHOLD * ema:
                if slow_key not in self._notified_monitor:
                    self._notified_monitor.add(slow_key)
                    events.append(CiSlow(
                        owner_repo, pr_number, job_name,
                        elapsed, ema,
                    ))

        return events

    # ── Adaptive mode (FR-31, FR-32) ─────────────────────────────────────

    def _next_interval(self) -> int:
        """Calculate next poll interval based on mode and rate limits."""
        base = {
            ActivityMode.PR_CHANGED: self.config.pr_changed_interval,
            ActivityMode.ACTIVE: self.config.active_interval,
            ActivityMode.INACTIVE: self.config.inactive_interval,
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
        """FR-32: Re-evaluate activity mode after each poll cycle.

        PR_CHANGED: any PR has mergeable=UNKNOWN (still computing after push)
        ACTIVE: any CI job QUEUED or IN_PROGRESS
        INACTIVE: neither condition

        If a SIGUSR1 wake is pending, PR_CHANGED is preserved regardless
        of other conditions (prevents race: wake scheduled during poll,
        then overwritten by stale recalculation).
        """
        # If a wake is pending, keep PR_CHANGED
        if self._wake_event:
            self.mode = ActivityMode.PR_CHANGED
            return

        # Check for active CI
        rows = self.db.execute(
            "SELECT status FROM ci_events "
            "GROUP BY owner_repo, pr_number, job_name "
            "HAVING recorded_at = MAX(recorded_at)"
        ).fetchall()

        has_active_ci = any(status in ACTIVE_STATUSES for (status,) in rows)

        # Check for UNKNOWN mergeable states (post-push, still computing)
        unknown_count = self.db.execute(
            "SELECT COUNT(*) FROM pull_requests "
            "WHERE mergeable = ? AND state = ?",
            (MERGEABLE_UNKNOWN, PR_STATE_OPEN),
        ).fetchone()[0]

        if unknown_count > 0:
            self.mode = ActivityMode.PR_CHANGED
        elif has_active_ci:
            self.mode = ActivityMode.ACTIVE
        else:
            self.mode = ActivityMode.INACTIVE


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _add_ci_completion(
    events: list[NotificationEvent],
    owner_repo: str,
    pr_number: int,
    db: sqlite3.Connection,
    current_jobs: dict,
) -> None:
    """Check if all CI jobs for a PR are complete and emit CiCompleted event.

    Called once per COMPLETED ci_event. Checks if this was the last
    non-terminal job for the PR.
    """
    # Get latest status for all jobs on this PR
    rows = db.execute(
        "SELECT job_name, status, conclusion FROM ci_events "
        "WHERE owner_repo = ? AND pr_number = ? "
        "GROUP BY job_name HAVING recorded_at = MAX(recorded_at)",
        (owner_repo, pr_number),
    ).fetchall()

    # Check if all jobs are in a terminal state
    all_done = all(status in TERMINAL_STATUSES for _, status, _ in rows)
    if not all_done:
        return

    # Determine overall conclusion
    conclusions = {c for _, _, c in rows if c}
    if CONCLUSION_FAILURE in conclusions:
        failed = [name for name, _, c in rows if c == CONCLUSION_FAILURE]
        events.append(CiCompleted(owner_repo, pr_number, CONCLUSION_FAILURE, failed))
    elif conclusions == {CONCLUSION_SUCCESS}:
        events.append(CiCompleted(owner_repo, pr_number, CONCLUSION_SUCCESS))


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive. Cross-platform.
    On Unix: os.kill(pid, 0) is a null signal check.
    On Windows: os.kill(pid, 0) sends CTRL_C_EVENT — use tasklist instead."""
    if sys.platform == "win32":
        try:
            import subprocess
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in proc.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False