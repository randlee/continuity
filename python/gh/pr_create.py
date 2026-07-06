"""gh pr create parser — extracts PR number, branch, repo from output URL."""

import os
import re
import sqlite3
import time


def parse(db: sqlite3.Connection, args: list[str], stdout: str,
          ensure_repo) -> None:
    """Parse gh pr create output. stdout is the URL like:
    https://github.com/owner/repo/pull/42"""
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", stdout)
    if not m:
        return
    owner_repo, pr_num = m.group(1), int(m.group(2))
    ensure_repo(owner_repo)
    branch = ""
    for i, a in enumerate(args):
        if a in ("--head", "-H") and i + 1 < len(args):
            branch = args[i + 1]
            break
    now = int(time.time())
    requested_by = os.environ.get("ATM_IDENTITY")
    db.execute(
        "INSERT OR REPLACE INTO pull_requests "
        "(owner_repo, pr_number, branch, state, requested_by, updated_at) "
        "VALUES (?, ?, ?, 'OPEN', ?, ?)",
        (owner_repo, pr_num, branch or "unknown", requested_by, now),
    )
