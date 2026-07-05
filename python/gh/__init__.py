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
    if len(args) < 2:
        return
    cmd, sub = args[0], args[1]

    if cmd == "pr" and sub == "create":
        parse_pr_create(db, args, stdout, ensure_repo_fn)
    elif cmd == "pr" and sub == "merge":
        parse_pr_merge(db, args, repo_fn)
    elif cmd == "pr" and sub == "view" and "--json" in args:
        parse_pr_view(db, args, stdout, repo_fn)
    elif cmd == "pr" and sub in ("checks", "status"):
        parse_pr_checks(db, args, stdout, repo_fn)
