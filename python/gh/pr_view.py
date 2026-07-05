"""gh pr view parser — extracts PR metadata and CI status from --json output."""

import json
import sqlite3
import time

from gh._checks import parse_checks


def parse(db: sqlite3.Connection, args: list[str], stdout: str,
          repo_fn) -> None:
    """Parse gh pr view --json <fields> output. Extracts PR metadata and
    statusCheckRollup into pull_requests and ci_events tables."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return

    pr_num = data.get("number")
    r = repo_fn()
    if not r or not pr_num:
        return

    now = int(time.time())
    db.execute(
        "INSERT OR REPLACE INTO pull_requests "
        "(owner_repo, pr_number, branch, head_sha, mergeable, state, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (r, pr_num,
         data.get("headRefName", "unknown"),
         data.get("headRefOid"),
         data.get("mergeable", "UNKNOWN").upper(),
         (data.get("state") or "OPEN").upper(),
         now),
    )
    if "statusCheckRollup" in data:
        parse_checks(db, r, pr_num, data["statusCheckRollup"])
