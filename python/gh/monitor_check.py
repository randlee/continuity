"""Slow/timeout detection for CI jobs — runs during interceptor parse phase.

When an agent runs gh pr view or gh pr checks, this module checks whether
any IN_PROGRESS jobs have exceeded their expected duration and emits
CiSlow/CiTimeout notifications via the ATM adapter.

Requirements: FR-56, FR-57, FR-58, FR-59.
"""

import logging
import sqlite3
import time

from notify import CiSlow, CiTimeout, dispatch_notifications

logger = logging.getLogger(__name__)


# Thresholds — matches Phase 5 requirements
SLOW_FACTOR = 2.0   # elapsed > 2× EMA
# CiTimeout uses max_ci_duration from repos table (or 2× EMA if NULL)


def check_slow_timeout(db: sqlite3.Connection, owner_repo: str,
                       pr_number: int) -> None:
    """Check all IN_PROGRESS jobs for a PR and emit slow/timeout events.

    Reads EMA and max_ci_duration from repos table, finds IN_PROGRESS
    job start times from ci_events, computes elapsed time, and dispatches
    CiSlow/CiTimeout if thresholds are exceeded.

    Fire-and-forget — never raises. Parse failures are silently ignored.
    """
    try:
        _check(db, owner_repo, pr_number)
    except Exception:
        logger.debug("monitor_check: error checking %s#%d",
                     owner_repo, pr_number, exc_info=True)


def _check(db: sqlite3.Connection, owner_repo: str, pr_number: int) -> None:
    """Core check logic."""
    # Get EMA and max_ci_duration from repos table
    row = db.execute(
        "SELECT avg_ci_duration, max_ci_duration FROM repos "
        "WHERE owner_repo = ?", (owner_repo,)
    ).fetchone()
    if not row:
        return

    avg_ci_duration, max_ci_duration = row[0], row[1]

    # No EMA data yet — skip
    if avg_ci_duration is None:
        return

    # Find all IN_PROGRESS jobs for this PR
    now = int(time.time())
    rows = db.execute(
        "SELECT job_name, status, recorded_at FROM ci_events "
        "WHERE owner_repo = ? AND pr_number = ? "
        "GROUP BY job_name HAVING recorded_at = MAX(recorded_at)",
        (owner_repo, pr_number),
    ).fetchall()

    events = []
    for job_name, status, recorded_at in rows:
        if status != "IN_PROGRESS":
            continue

        elapsed = now - recorded_at
        if elapsed <= 0:
            continue

        # Check CiTimeout first (hard limit)
        timeout_limit = max_ci_duration if max_ci_duration is not None else int(SLOW_FACTOR * avg_ci_duration)
        if elapsed > timeout_limit:
            events.append(CiTimeout(owner_repo, pr_number, job_name, timeout_limit))
            continue  # don't also emit CiSlow for the same job

        # Check CiSlow (soft threshold)
        if elapsed > SLOW_FACTOR * avg_ci_duration:
            events.append(CiSlow(owner_repo, pr_number, job_name,
                                elapsed, avg_ci_duration))

    if events:
        dispatch_notifications(db, events)
