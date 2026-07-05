"""git command dispatcher — routes to subcommand handlers.

Public API:
    parse(args, db) → None
"""

import sqlite3

from git.push import parse as parse_push


def parse(args: list[str], db: sqlite3.Connection) -> None:
    """Route git command to the appropriate subcommand handler."""
    if not args:
        return
    if args[0] == "push":
        parse_push(args, db)
