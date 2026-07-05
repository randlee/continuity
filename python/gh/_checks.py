"""Shared CI check parsing — used by pr_view and pr_checks."""

import sqlite3
import time

STATUS_MAP = {
    "QUEUED": "QUEUED", "IN_PROGRESS": "IN_PROGRESS",
    "COMPLETED": "COMPLETED", "PENDING": "QUEUED",
    "REQUESTED": "QUEUED", "WAITING": "QUEUED",
}


def parse_checks(db: sqlite3.Connection, repo: str, pr_num: int,
                 data: list | dict) -> None:
    """Parse statusCheckRollup JSON into ci_events with state diffing."""
    checks: list[dict] = []
    if isinstance(data, dict):
        rollup = data.get("statusCheckRollup", [])
        if isinstance(rollup, list):
            checks = rollup
    elif isinstance(data, list):
        checks = data

    now = int(time.time())
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = check.get("name", "unknown")
        raw_status = check.get("status", "UNKNOWN").upper()
        status = STATUS_MAP.get(raw_status, raw_status)
        conclusion = check.get("conclusion")
        if conclusion:
            conclusion = conclusion.upper()

        last = db.execute(
            "SELECT status, conclusion FROM ci_events "
            "WHERE owner_repo = ? AND pr_number = ? AND job_name = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (repo, pr_num, name),
        ).fetchone()

        changed = (last is None or last[0] != status or last[1] != conclusion)
        if changed:
            db.execute(
                "INSERT INTO ci_events "
                "(owner_repo, pr_number, job_name, status, conclusion, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, pr_num, name, status, conclusion, now),
            )
