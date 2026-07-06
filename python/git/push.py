"""git push handler — records push events."""

import json
import sqlite3
import time


def parse(args: list[str], db: sqlite3.Connection) -> None:
    """Record git push event in cli_events with ATM_IDENTITY."""
    import os
    now = int(time.time())
    remote, ref = "origin", ""
    for i, a in enumerate(args[1:], 1):
        if not a.startswith("-"):
            if i == 1:
                remote = a
            elif i == 2:
                ref = a
            break
    db.execute(
        "INSERT INTO cli_events "
        "(command, args_json, exit_code, duration_ms, recorded_at) "
        "VALUES ('git-push', ?, 0, 0, ?)",
        (json.dumps({
            "remote": remote,
            "ref": ref or "push",
            "atm_identity": os.environ.get("ATM_IDENTITY"),
        }), now),
    )
