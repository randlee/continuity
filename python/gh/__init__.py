"""gh command dispatcher — routes to subcommand parsers.

Public API:
    parse(args, stdout, db, repo_fn, ensure_repo) → None
"""

import sqlite3

from gh.pr_create import parse as parse_pr_create
from gh.pr_merge import parse as parse_pr_merge
from gh.pr_view import parse as parse_pr_view
from gh.pr_checks import parse as parse_pr_checks


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
    elif cmd == "pr" and sub in ("checks", "status"):
        parse_pr_checks(db, effective, stdout, repo_fn)
