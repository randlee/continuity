"""CLI commands for continuity. HTTP RPC client — no direct SQLite reads.

FR-38: All commands use daemon HTTP RPC.
FR-39: ci status → GET /prs
FR-40: ci log <repo> <pr#> → GET /prs/<owner>/<repo>/<num>
FR-41: ci history <repo> → GET /prs?closed=true&repo=<owner>/<repo>
FR-42: ci usage → GET /status
FR-43: ci poll → POST /poll

Public API: each command is a function that returns formatted output string.
Testable with mocked HTTP responses.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

from cli.http_client import get, post, DaemonError
from constants import (
    STATUS_QUEUED, STATUS_IN_PROGRESS, STATUS_COMPLETED,
    CONCLUSION_SUCCESS, CONCLUSION_FAILURE,
)


# ═══════════════════════════════════════════════════════════════════════════
# ci status (FR-39)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_status() -> str:
    """Show all open PRs with current job states and activity mode."""
    try:
        data = get("/prs")
    except DaemonError as e:
        return f"Error: {e}\n"

    if data.get("status") != "ok":
        return f"Error: {data.get('error', 'unknown error')}\n"

    prs = data.get("prs", [])
    mode = data.get("mode", "UNKNOWN")

    if not prs:
        return ("No repos registered. Use 'ci register <owner/repo>'.\n"
                f"Mode: {mode}\n")

    lines = []
    lines.append(f"{'repo':<30} {'PR':>5} {'branch':<25} {'mergeable':>10} {'mode':>8}  jobs")
    lines.append("-" * 100)

    for pr in prs:
        owner_repo = pr["owner_repo"]
        pr_num = pr["pr_number"]
        branch = pr.get("branch", "")
        mergeable = pr.get("mergeable", "UNKNOWN")
        jobs = pr.get("jobs", [])

        pr_mode = _pr_mode(jobs)
        job_summary = " ".join(_job_symbol(j["name"], j["status"], j.get("conclusion"))
                               for j in jobs)

        lines.append(
            f"{owner_repo:<30} {pr_num:>5} {branch:<25} {mergeable or 'UNKNOWN':>10} {pr_mode:<8}  {job_summary}"
        )

    lines.append("")
    lines.append(f"Mode: {mode}")
    if data.get("warning"):
        lines.append(f"Warning: {data['warning']}")

    return "\n".join(lines) + "\n"


def _pr_mode(jobs: list[dict]) -> str:
    """Determine PR mode from job states."""
    statuses = {j["status"] for j in jobs}
    active = {STATUS_QUEUED, STATUS_IN_PROGRESS}
    if statuses & active:
        return "ACTIVE"
    if STATUS_COMPLETED in statuses:
        conclusions = {j.get("conclusion") for j in jobs if j.get("conclusion")}
        if conclusions == {CONCLUSION_SUCCESS}:
            return "SUCCESS"
        if CONCLUSION_FAILURE in conclusions:
            return "FAILED"
    return "PENDING"


def _job_symbol(name: str, status: str, conclusion: str | None) -> str:
    """Render a job as a compact symbol: build✓, test⧗, lint✗."""
    if conclusion == CONCLUSION_SUCCESS:
        mark = "✓"
    elif conclusion == CONCLUSION_FAILURE:
        mark = "✗"
    elif status == STATUS_IN_PROGRESS:
        mark = "⧗"
    elif status in (STATUS_QUEUED, "PENDING"):
        mark = "⧗"
    else:
        mark = "?"
    return f"{name}{mark}"


# ═══════════════════════════════════════════════════════════════════════════
# ci log <repo> <pr#> (FR-40)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_log(owner_repo: str, pr_number: int) -> str:
    """Show all ci_events for a PR in chronological order."""
    try:
        data = get(f"/prs/{owner_repo}/{pr_number}")
    except DaemonError as e:
        return f"Error: {e}\n"

    if data.get("status") != "ok":
        return f"Error: {data.get('error', 'unknown error')}\n"

    # The detail endpoint returns the PR object directly with events inside
    events = data.get("events", [])
    if not events:
        return f"No CI events found for {owner_repo}#{pr_number}\n"

    lines = [f"{owner_repo}#{pr_number}"]
    lines.append("-" * 60)
    for ev in events:
        ts = _format_ts(ev.get("at", 0))
        job = ev.get("job", "?")
        status = ev.get("status", "?")
        conc = ev.get("conclusion") or "-"
        lines.append(f"{ts}  {job:<20} {status:<15} {conc}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# ci history <repo> (FR-41)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_history(owner_repo: str, limit: int = 20,
                db: sqlite3.Connection | None = None) -> str:
    """Show closed PRs with outcomes and durations."""
    if db is not None:
        return _cmd_history_from_db(db, owner_repo, limit)
    try:
        return _cmd_history_from_path(owner_repo, limit)
    except DaemonError:
        return _cmd_history_from_path(owner_repo, limit)


# ═══════════════════════════════════════════════════════════════════════════
# ci usage (FR-42)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_usage(account: str | None = None) -> str:
    """Show API point consumption per account."""
    try:
        data = get("/status")
    except DaemonError as e:
        return f"Error: {e}\n"

    if data.get("status") != "ok":
        return f"Error: {data.get('error', 'unknown error')}\n"

    lines = []
    lines.append(f"Rate limit remaining: {data.get('rate_limit_remaining', 'N/A')}")
    lines.append(f"Repos tracked: {data.get('repos_tracked', 'N/A')}")
    lines.append(f"Mode: {data.get('mode', 'N/A')}")
    if data.get("stale_seconds") is not None:
        lines.append(f"Data age: {data['stale_seconds']}s")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# ci poll (FR-43)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_poll() -> str:
    """Trigger immediate poll cycle."""
    try:
        data = post("/poll")
    except DaemonError as e:
        return f"Error: {e}\n"

    if data.get("status") != "ok":
        return f"Error: {data.get('error', 'unknown error')}\n"

    lines = [data.get("message", "poll completed")]
    lines.append(f"Mode: {data.get('mode', 'unknown')}")
    if data.get("last_synced"):
        ts = _format_ts(data["last_synced"])
        lines.append(f"Last synced: {ts}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# ci register (direct SQLite — runs before daemon is up)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_register(db: sqlite3.Connection, owner_repo: str, account: str) -> str:
    """Add repo to tracking. Idempotent."""
    existing = db.execute(
        "SELECT 1 FROM repos WHERE owner_repo = ?", (owner_repo,)
    ).fetchone()

    if existing:
        return f"Repo {owner_repo} already registered.\n"

    db.execute(
        "INSERT INTO repos (owner_repo, gh_account) VALUES (?, ?)",
        (owner_repo, account),
    )
    db.commit()

    return f"Registered {owner_repo} (account: {account})\n"


# ═══════════════════════════════════════════════════════════════════════════
# ATM commands — designated member management
# ═══════════════════════════════════════════════════════════════════════════

def cmd_atm_set_notify(db: sqlite3.Connection, owner_repo: str,
                       member: str | None) -> str:
    """Set or reset designated_member for a repo.
    member=None or '--reset' resets to team-lead default.
    """
    if member is None or member == "--reset":
        db.execute(
            "UPDATE repos SET designated_member = NULL WHERE owner_repo = ?",
            (owner_repo,),
        )
        db.commit()
        return f"Designated member for {owner_repo} reset to team-lead (default)\n"

    # Basic validation: no spaces, reasonable length
    member = member.strip()
    if not member or " " in member or len(member) > 64:
        return f"Invalid member name: '{member}'. Must be a single, non-empty ATM identity.\n"

    db.execute(
        "UPDATE repos SET designated_member = ? WHERE owner_repo = ?",
        (member, owner_repo),
    )
    db.commit()
    return f"Designated member for {owner_repo} set to {member}\n"


def cmd_atm_show_notify(db: sqlite3.Connection, owner_repo: str) -> str:
    """Show current designated member for a repo."""
    row = db.execute(
        "SELECT designated_member FROM repos WHERE owner_repo = ?",
        (owner_repo,),
    ).fetchone()

    if row is None:
        return f"Repo {owner_repo} is not registered. Use 'ci register' first.\n"

    member = row[0]
    if member:
        return f"Designated member for {owner_repo}: {member}\n"
    return f"Designated member for {owner_repo}: team-lead (default)\n"


def cmd_atm_status() -> str:
    """Check ATM configuration status."""
    from atm import atm_configured, CI_IDENTITY

    issues = []

    team = os.environ.get("ATM_TEAM")
    if not team:
        issues.append("ATM_TEAM not set")
    else:
        issues.append(f"ATM_TEAM={team} ✓")

    identity = os.environ.get("ATM_IDENTITY", CI_IDENTITY)
    issues.append(f"ATM_IDENTITY={identity} ✓")

    if atm_configured():
        issues.append("atm binary found ✓")
    else:
        issues.append("atm binary NOT found ✗")

    lines = ["ATM Configuration:"]
    lines.append("-" * 40)
    for issue in issues:
        lines.append(f"  {issue}")

    if "NOT found" in "\n".join(issues) or "not set" in "\n".join(issues):
        lines.append("")
        lines.append("Status: NOT CONFIGURED")
        return "\n".join(lines) + "\n"

    lines.append("")
    lines.append("Status: READY")
    return "\n".join(lines) + "\n"


# ── SQLite fallback for ci history (used until httpd adds closed PRs endpoint)


def _cmd_history_from_db(db: sqlite3.Connection, owner_repo: str, limit: int = 20) -> str:
    """Direct SQLite history query using provided connection."""
    prs = db.execute(
        "SELECT pr_number, branch, state FROM pull_requests "
        "WHERE owner_repo = ? AND state IN ('CLOSED', 'MERGED') "
        "ORDER BY pr_number DESC LIMIT ?",
        (owner_repo, limit),
    ).fetchall()

    if not prs:
        return f"No closed PRs for {owner_repo}\n"

    lines = [f"{owner_repo} — closed PRs"]
    lines.append("-" * 70)
    lines.append(f"{'PR':>5} {'branch':<25} {'state':>8}")

    for pr_num, branch, state in prs:
        lines.append(f"{pr_num:>5} {branch:<25} {state:>8}")

    return "\n".join(lines) + "\n"


def _cmd_history_from_path(owner_repo: str, limit: int = 20) -> str:
    """SQLite history query using filesystem DB path (fallback when daemon down)."""
    import db as _db
    continuity_home = os.environ.get("CONTINUITY_HOME", "")
    if continuity_home:
        db_path = Path(continuity_home) / "continuity.db"
    else:
        db_path = Path.home() / ".local" / "share" / "continuity" / "continuity.db"
    conn = _db.ensure_db(db_path)
    try:
        return _cmd_history_from_db(conn, owner_repo, limit)
    finally:
        conn.close()


def _format_ts(unix_ts: int) -> str:
    """Format unix timestamp for display."""
    if not unix_ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_ts))
