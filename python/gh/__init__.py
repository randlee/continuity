"""gh command dispatcher — routes to subcommand parsers.

Public API:
    parse(args, stdout, db, repo_fn, ensure_repo) → None
"""

import json
import sqlite3

from gh.pr_create import parse as parse_pr_create
from gh.pr_merge import parse as parse_pr_merge
from gh.pr_view import parse as parse_pr_view
from gh.pr_checks import parse as parse_pr_checks
from gh.monitor_check import check_slow_timeout


def parse(args: list[str], stdout: str, db: sqlite3.Connection,
          repo_fn, ensure_repo_fn) -> None:
    """Route gh command output to the appropriate subcommand parser."""
    # Strip -R <repo> prefix:  gh -R owner/repo pr view 1
    idx = 0
    while idx < len(args) - 1 and args[idx] == "-R":
        idx += 2
    effective = args[idx:] if idx < len(args) else []
    if len(effective) < 2:
        return
    cmd, sub = effective[0], effective[1]

    if cmd == "pr" and sub == "create":
        parse_pr_create(db, effective, stdout, ensure_repo_fn)
    elif cmd == "pr" and sub == "merge":
        parse_pr_merge(db, effective, repo_fn)
    elif cmd == "pr" and sub == "view" and "--json" in effective:
        parse_pr_view(db, effective, stdout, repo_fn)
        _check_monitor(db, effective, stdout, repo_fn)
    elif cmd == "pr" and sub in ("checks", "status"):
        parse_pr_checks(db, effective, stdout, repo_fn)
        _check_monitor(db, effective, stdout, repo_fn)


def _check_monitor(db, args, stdout, repo_fn) -> None:
    """Check for slow/timeout CI jobs after pr view/checks."""
    try:
        r = repo_fn()
        if not r:
            return
        pr_num = _extract_pr_number(args, stdout)
        if not pr_num:
            return
        check_slow_timeout(db, r, pr_num)
    except Exception:
        pass


def _extract_pr_number(args: list[str], stdout: str) -> int | None:
    """Extract PR number from args or JSON output."""
    # Try args first: gh pr checks 42
    for a in args[2:]:
        try:
            return int(a)
        except ValueError:
            continue
    # Try JSON output: {"number": 42, ...}
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and "number" in data:
            return data["number"]
    except (json.JSONDecodeError, TypeError):
        pass
    return None
