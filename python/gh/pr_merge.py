"""gh pr merge parser — marks PR as MERGED in pull_requests table."""

import sqlite3
import time


def parse(db: sqlite3.Connection, args: list[str], repo_fn) -> None:
    """Parse gh pr merge <N>. Looks up repo from existing PR, falls back
    to repo_fn() for detection."""
    for a in args[2:]:
        try:
            pr_num = int(a)
            row = db.execute(
                "SELECT owner_repo FROM pull_requests "
                "WHERE pr_number = ? LIMIT 1",
                (pr_num,),
            ).fetchone()
            r = row[0] if row else repo_fn()
            if r:
                db.execute(
                    "UPDATE pull_requests SET state='MERGED', updated_at=? "
                    "WHERE owner_repo=? AND pr_number=?",
                    (int(time.time()), r, pr_num),
                )
            break
        except ValueError:
            continue
