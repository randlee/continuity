"""gh pr checks parser — extracts CI job statuses from --json output."""

import json
import sqlite3

from gh._checks import parse_checks


def parse(db: sqlite3.Connection, args: list[str], stdout: str,
          repo_fn) -> None:
    """Parse gh pr checks --json output into ci_events."""
    pr_num = None
    for a in args[2:]:
        try:
            pr_num = int(a)
            break
        except ValueError:
            continue
    if not pr_num:
        return
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return
    r = repo_fn()
    if r:
        parse_checks(db, r, pr_num, data)
