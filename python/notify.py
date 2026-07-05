"""Notification dispatch layer — bridges daemon state detection and ATM delivery.

Thin translation layer. Takes raw state-change events from the daemon,
formats them per the notification templates in requirements-atm.md §5.3,
groups by target, and dispatches via atm_notify() in a spawned thread.

Designed for portability: single dispatch function with typed events.
Easily replaceable by a Rust notifier trait.

Public API:
    dispatch_notifications(db, events) → None (spawns thread)

Event types:
    PrCreatedUnmergable, PrBecameUnmergable, CascadeUnmergable,
    CiCompleted, CiSlow, CiTimeout
"""

import json
import logging
import sqlite3
import threading

from atm import (
    atm_notify,
    atm_configured,
    format_conflict_files,
)
from constants import CONCLUSION_SUCCESS, CONCLUSION_FAILURE

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Event types
# ═══════════════════════════════════════════════════════════════════════════

class NotificationEvent:
    """Base class for notification events."""
    owner_repo: str
    pr_number: int

    @property
    def requested_by(self) -> str | None:
        """ATM identity of who triggered this event. None = daemon-detected."""
        return None


class PrCreatedUnmergable(NotificationEvent):
    """A newly created PR is unmergable."""
    def __init__(self, owner_repo: str, pr_number: int,
                 requested_by: str | None, conflict_files: list[str]):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self._requested_by = requested_by
        self.conflict_files = conflict_files

    @property
    def requested_by(self) -> str | None:
        return self._requested_by


class PrBecameUnmergable(NotificationEvent):
    """A push made an existing PR unmergable."""
    def __init__(self, owner_repo: str, pr_number: int,
                 requested_by: str | None,
                 commit_sha: str = "",
                 conflict_files: list[str] | None = None):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self._requested_by = requested_by
        self.commit_sha = commit_sha
        self.conflict_files = conflict_files or []

    @property
    def requested_by(self) -> str | None:
        return self._requested_by


class CascadeUnmergable(NotificationEvent):
    """A merged PR caused another PR to become unmergable."""
    def __init__(self, owner_repo: str, pr_number: int,
                 merged_pr_number: int,
                 conflict_files: list[str] | None = None):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self.merged_pr_number = merged_pr_number
        self.conflict_files = conflict_files or []

    @property
    def requested_by(self) -> str | None:
        return None  # cascade always routes to team-lead


class CiCompleted(NotificationEvent):
    """All CI jobs for a PR have completed."""
    def __init__(self, owner_repo: str, pr_number: int,
                 conclusion: str,  # "SUCCESS" or "FAILURE"
                 failed_jobs: list[str] | None = None):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self.conclusion = conclusion
        self.failed_jobs = failed_jobs or []

    @property
    def requested_by(self) -> str | None:
        return None  # CI events always route to team-lead


class CiSlow(NotificationEvent):
    """A CI job is running significantly longer than normal."""
    def __init__(self, owner_repo: str, pr_number: int,
                 job_name: str, elapsed_seconds: int, ema_seconds: float):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self.job_name = job_name
        self.elapsed_seconds = elapsed_seconds
        self.ema_seconds = ema_seconds

    @property
    def requested_by(self) -> str | None:
        return None


class CiTimeout(NotificationEvent):
    """A CI job has exceeded its maximum expected duration."""
    def __init__(self, owner_repo: str, pr_number: int,
                 job_name: str, max_duration_seconds: int):
        self.owner_repo = owner_repo
        self.pr_number = pr_number
        self.job_name = job_name
        self.max_duration_seconds = max_duration_seconds

    @property
    def requested_by(self) -> str | None:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Event formatters — map event types to (subject, body) per §5.3
# ═══════════════════════════════════════════════════════════════════════════

def _format_event(event: NotificationEvent) -> tuple[str, str]:
    """Format a single event into (subject, body)."""
    if isinstance(event, PrCreatedUnmergable):
        return _format_pr_created_unmergable(event)
    if isinstance(event, PrBecameUnmergable):
        return _format_pr_became_unmergable(event)
    if isinstance(event, CascadeUnmergable):
        return _format_cascade_unmergable(event)
    if isinstance(event, CiCompleted):
        return _format_ci_completed(event)
    if isinstance(event, CiSlow):
        return _format_ci_slow(event)
    if isinstance(event, CiTimeout):
        return _format_ci_timeout(event)
    raise ValueError(f"Unknown event type: {type(event)}")


def _format_pr_created_unmergable(e: PrCreatedUnmergable) -> tuple[str, str]:
    subject = f"PR #{e.pr_number} unmergable"
    files = format_conflict_files(e.conflict_files) if e.conflict_files else ""
    if files:
        body = (f"PR #{e.pr_number} is unmergable — merge conflict in:\n"
                f"{files}")
    else:
        body = f"PR #{e.pr_number} is unmergable — check GitHub for details"
    return (subject, body)


def _format_pr_became_unmergable(e: PrBecameUnmergable) -> tuple[str, str]:
    subject = f"PR #{e.pr_number} unmergable after push"
    sha_info = f" after push {e.commit_sha[:7]}" if e.commit_sha else ""
    files = format_conflict_files(e.conflict_files) if e.conflict_files else ""
    if files:
        body = (f"PR #{e.pr_number} became unmergable{sha_info} — "
                f"merge conflict in:\n{files}")
    else:
        body = (f"PR #{e.pr_number} became unmergable{sha_info} — "
                f"check GitHub for details")
    return (subject, body)


def _format_cascade_unmergable(e: CascadeUnmergable) -> tuple[str, str]:
    subject = f"PR #{e.pr_number} now unmergable"
    files = format_conflict_files(e.conflict_files) if e.conflict_files else ""
    if files:
        body = (f"PR #{e.merged_pr_number} merged. "
                f"PR #{e.pr_number} is now unmergable — "
                f"merge conflict in:\n{files}")
    else:
        body = (f"PR #{e.merged_pr_number} merged. "
                f"PR #{e.pr_number} is now unmergable — "
                f"check GitHub for details")
    return (subject, body)


def _format_ci_completed(e: CiCompleted) -> tuple[str, str]:
    if e.conclusion == CONCLUSION_SUCCESS:
        subject = f"PR #{e.pr_number} CI passed"
        body = f"PR #{e.pr_number} — all checks passed"
    else:
        subject = f"PR #{e.pr_number} CI failed"
        if e.failed_jobs:
            jobs = ", ".join(e.failed_jobs)
            body = f"PR #{e.pr_number} — {jobs} failed"
        else:
            body = f"PR #{e.pr_number} — CI failed"
    return (subject, body)


def _format_ci_slow(e: CiSlow) -> tuple[str, str]:
    subject = f"PR #{e.pr_number} CI slow"
    body = (f"PR #{e.pr_number} — {e.job_name} running for "
            f"{_format_duration(e.elapsed_seconds)}, "
            f"2× normal ({_format_duration(int(e.ema_seconds))})")
    return (subject, body)


def _format_ci_timeout(e: CiTimeout) -> tuple[str, str]:
    subject = f"PR #{e.pr_number} CI timeout"
    body = (f"PR #{e.pr_number} — {e.job_name} exceeded max duration "
            f"({_format_duration(e.max_duration_seconds)}), may be hung")
    return (subject, body)


def _format_duration(seconds: int) -> str:
    """Human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════════════════════════════════════

def dispatch_notifications(
    db: sqlite3.Connection,
    events: list[NotificationEvent],
) -> None:
    """Dispatch notifications for a list of events.

    Events are grouped by (owner_repo, requested_by), formatted per the
    notification templates, and dispatched via atm_notify() in a spawned
    daemon thread. Non-blocking — returns immediately.

    Args:
        db: SQLite connection (must be thread-safe, WAL mode)
        events: list of notification events from the daemon
    """
    if not events:
        return
    if not atm_configured():
        return

    # Group events by (owner_repo, requested_by)
    groups: dict[tuple[str, str | None], list[NotificationEvent]] = {}
    for event in events:
        key = (event.owner_repo, event.requested_by)
        groups.setdefault(key, []).append(event)

    # Build batched notifications for each group
    batched: list[tuple[str, str | None, list[tuple[str, str]]]] = []
    for (owner_repo, requested_by), group_events in groups.items():
        notifications = [_format_event(e) for e in group_events]
        batched.append((owner_repo, requested_by, notifications))

    # Dispatch in a daemon thread (non-blocking)
    thread = threading.Thread(
        target=_dispatch_batched,
        args=(db, batched),
        daemon=True,
        name="continuity-notify",
    )
    thread.start()


def _dispatch_batched(
    db: sqlite3.Connection,
    batched: list[tuple[str, str | None, list[tuple[str, str]]]],
) -> None:
    """Send each batched group via atm_notify. Runs in a daemon thread."""
    for owner_repo, requested_by, notifications in batched:
        try:
            atm_notify(db, owner_repo, requested_by, notifications)
        except Exception:
            logger.exception(
                "notification dispatch failed for %s (%d events)",
                owner_repo, len(notifications),
            )


# ═══════════════════════════════════════════════════════════════════════════
# Daemon helpers — identity resolution from DB
# ═══════════════════════════════════════════════════════════════════════════

def resolve_pr_identity(
    db: sqlite3.Connection,
    owner_repo: str,
    pr_number: int,
) -> str | None:
    """Look up the ATM identity that created a PR.

    Returns requested_by from the pull_requests table, or None if not set.
    """
    row = db.execute(
        "SELECT requested_by FROM pull_requests "
        "WHERE owner_repo = ? AND pr_number = ?",
        (owner_repo, pr_number),
    ).fetchone()
    if row and row[0]:
        return row[0]
    return None


def resolve_push_identity(
    db: sqlite3.Connection,
    owner_repo: str,
) -> str | None:
    """Look up the ATM identity from the most recent git-push event.

    Queries cli_events for the latest git-push to the given repo
    and extracts atm_identity from args_json.
    """
    row = db.execute(
        "SELECT args_json FROM cli_events "
        "WHERE command = 'git-push' "
        "ORDER BY recorded_at DESC LIMIT 1",
    ).fetchone()
    if not row:
        return None
    try:
        args = json.loads(row[0])
        return args.get("atm_identity")
    except (json.JSONDecodeError, TypeError):
        return None
