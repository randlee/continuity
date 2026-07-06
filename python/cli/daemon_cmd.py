"""CLI commands for continuity daemon. Read-only SQLite queries.

FR-38: All commands read SQLite only — no gh calls.
FR-39: continuity status — open PRs + job states + activity mode
FR-40: continuity log <repo> <pr#> — chronological ci_events
FR-41: continuity history <repo> — closed PRs with outcomes
FR-42: continuity usage — API point consumption per account
FR-43: continuity register — add repo + install post-push hook

Public API: each command is a function that takes (db, args) and returns
formatted output string. Testable with temp SQLite DBs.
"""

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from constants import (
    STATUS_QUEUED, STATUS_IN_PROGRESS, STATUS_COMPLETED,
    CONCLUSION_SUCCESS, CONCLUSION_FAILURE,
    PR_STATE_OPEN, MERGEABLE_UNKNOWN,
    ACTIVE_STATUSES,
)


# ═══════════════════════════════════════════════════════════════════════════
# continuity status (FR-39)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_status(db: sqlite3.Connection) -> str:
    """Show all open PRs with current job states and activity mode."""
    repos = db.execute("SELECT owner_repo FROM repos ORDER BY owner_repo").fetchall()
    if not repos:
        return "No repos registered. Use 'continuity register <owner/repo>'.\n"

    lines = []
    lines.append(f"{'repo':<30} {'PR':>5} {'branch':<25} {'mergeable':>10} {'mode':>8}  jobs")
    lines.append("-" * 100)

    for (owner_repo,) in repos:
        prs = db.execute(
            "SELECT pr_number, branch, mergeable, state FROM pull_requests "
            "WHERE owner_repo = ? AND state = 'OPEN' ORDER BY pr_number",
            (owner_repo,),
        ).fetchall()

        for pr_num, branch, mergeable, state in prs:
            # Get latest job statuses
            jobs = db.execute(
                "SELECT job_name, status, conclusion FROM ci_events "
                "WHERE owner_repo = ? AND pr_number = ? "
                "GROUP BY job_name HAVING recorded_at = MAX(recorded_at)",
                (owner_repo, pr_num),
            ).fetchall()

            mode = _pr_mode(jobs)
            job_summary = " ".join(_job_symbol(name, status, conclusion) for name, status, conclusion in jobs)

            lines.append(
                f"{owner_repo:<30} {pr_num:>5} {branch:<25} {mergeable or 'UNKNOWN':>10} {mode:<8}  {job_summary}"
            )

    # Activity footer
    mode = _activity_mode(db)
    lines.append("")
    lines.append(f"Mode: {mode}")

    return "\n".join(lines) + "\n"


def _pr_mode(jobs: list[tuple]) -> str:
    """Determine PR mode from job states."""
    statuses = {s for _, s, _ in jobs}
    if statuses & ACTIVE_STATUSES:
        return "ACTIVE"
    if statuses & {STATUS_COMPLETED}:
        conclusions = {c for _, _, c in jobs if c}
        if conclusions == {CONCLUSION_SUCCESS}:
            return "SUCCESS"
        if CONCLUSION_FAILURE in conclusions:
            return "FAILED"
    return "PENDING"


def _job_symbol(name: str, status: str, conclusion: str | None) -> str:
    """Render a job as a compact symbol: build\u2713, test\u29d7, lint\u2717."""
    if conclusion == CONCLUSION_SUCCESS:
        mark = "\u2713"
    elif conclusion == CONCLUSION_FAILURE:
        mark = "\u2717"
    elif status == STATUS_IN_PROGRESS:
        mark = "\u29d7"
    elif status in (STATUS_QUEUED, "PENDING"):
        mark = "\u29d7"
    else:
        mark = "?"
    return f"{name}{mark}"


def _activity_mode(db: sqlite3.Connection) -> str:
    """Determine overall activity mode."""
    rows = db.execute(
        "SELECT status FROM ci_events "
        "GROUP BY owner_repo, pr_number, job_name "
        "HAVING recorded_at = MAX(recorded_at)"
    ).fetchall()

    if any(s in ACTIVE_STATUSES for (s,) in rows):
        return "ACTIVE"

    unknown_count = db.execute(
        "SELECT COUNT(*) FROM pull_requests "
        "WHERE mergeable = ? AND state = ?",
        (MERGEABLE_UNKNOWN, PR_STATE_OPEN),
    ).fetchone()[0]
    if unknown_count > 0:
        return "PR_CHANGED"
    return "INACTIVE"


# ═══════════════════════════════════════════════════════════════════════════
# continuity log <repo> <pr#> (FR-40)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_log(db: sqlite3.Connection, owner_repo: str, pr_number: int) -> str:
    """Show all ci_events for a PR in chronological order."""
    events = db.execute(
        "SELECT job_name, status, conclusion, recorded_at FROM ci_events "
        "WHERE owner_repo = ? AND pr_number = ? "
        "ORDER BY recorded_at ASC",
        (owner_repo, pr_number),
    ).fetchall()

    if not events:
        return f"No CI events found for {owner_repo}#{pr_number}\n"

    lines = [f"{owner_repo}#{pr_number}"]
    lines.append("-" * 60)
    for job_name, status, conclusion, recorded_at in events:
        ts = _format_ts(recorded_at)
        conc = conclusion or "-"
        lines.append(f"{ts}  {job_name:<20} {status:<15} {conc}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# continuity history <repo> (FR-41)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_history(db: sqlite3.Connection, owner_repo: str, limit: int = 20) -> str:
    """Show closed PRs with outcomes and durations."""
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
    lines.append(f"{'PR':>5} {'branch':<25} {'state':>8}  {'duration':>10}  {'outcome'}")

    for pr_num, branch, state in prs:
        # Derive duration from first QUEUED to last COMPLETED
        first = db.execute(
            "SELECT recorded_at FROM ci_events "
            "WHERE owner_repo = ? AND pr_number = ? AND status = 'QUEUED' "
            "ORDER BY recorded_at ASC LIMIT 1",
            (owner_repo, pr_num),
        ).fetchone()

        last = db.execute(
            "SELECT recorded_at FROM ci_events "
            "WHERE owner_repo = ? AND pr_number = ? AND status = 'COMPLETED' "
            "ORDER BY recorded_at DESC LIMIT 1",
            (owner_repo, pr_num),
        ).fetchone()

        duration = ""
        if first and last:
            secs = last[0] - first[0]
            if secs < 60:
                duration = f"{secs}s"
            elif secs < 3600:
                duration = f"{secs // 60}m"
            else:
                duration = f"{secs // 3600}h{secs % 3600 // 60}m"

        # Outcome: latest conclusion for each job
        outcomes = db.execute(
            "SELECT conclusion FROM ci_events "
            "WHERE owner_repo = ? AND pr_number = ? AND conclusion IS NOT NULL "
            "GROUP BY job_name HAVING recorded_at = MAX(recorded_at)",
            (owner_repo, pr_num),
        ).fetchall()
        outcome = "\u2713" if all(c[0] == CONCLUSION_SUCCESS for c in outcomes) else "\u2717" if any(c[0] == CONCLUSION_FAILURE for c in outcomes) else "?"

        lines.append(f"{pr_num:>5} {branch:<25} {state:>8}  {duration:>10}  {outcome}")

    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# continuity usage (FR-42)
# ═══════════════════════════════════════════════════════════════════════════

def cmd_usage(db: sqlite3.Connection, account: str | None = None) -> str:
    """Show API point consumption per account."""
    query = (
        "SELECT gh_account, COUNT(*) as queries, SUM(cost) as total_cost, "
        "AVG(cost) as avg_cost, MAX(remaining) as remaining, MAX(reset_at) as reset_at "
        "FROM api_usage GROUP BY gh_account ORDER BY gh_account"
    )
    if account:
        query = query.replace("GROUP BY", f"WHERE gh_account = '{account}' GROUP BY")

    rows = db.execute(query).fetchall()
    if not rows:
        return "No API usage data yet.\n"

    lines = []
    lines.append(f"{'account':<20} {'queries':>8} {'points':>8} {'avg':>6} {'remaining':>10} {'resets':>10}")
    lines.append("-" * 70)

    for acct, queries, total_cost, avg_cost, remaining, reset_at in rows:
        lines.append(
            f"{acct:<20} {queries:>8} {total_cost or 0:>8} {avg_cost or 0:>6.1f} {remaining or 0:>10} {reset_at or '':>10}"
        )

    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# continuity register (FR-43)
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
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _format_ts(unix_ts: int) -> str:
    """Format unix timestamp for display."""
    if not unix_ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_ts))


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
        return f"Repo {owner_repo} is not registered. Use 'continuity register' first.\n"

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