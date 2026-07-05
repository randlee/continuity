"""ATM adapter module for continuity notifications.

Self-contained module. All ATM CLI invocation, notification routing,
retry/backoff, and fallback logic lives here. Callers never branch on
ATM availability — call atm_notify() and the module handles everything
internally.

Designed for portability: the module exposes a minimal public API suitable
for a Rust notifier trait. No daemon coupling, no shared state.

Public API:
    atm_configured()       → bool
    atm_get_designated(db, owner_repo) → str
    atm_notify(db, owner_repo, requested_by, notifications) → bool
    format_conflict_files(files, max_display=6) → str
"""

import logging
import os
import shutil
import sqlite3
import subprocess
import time

from constants import ATM_TEAM_LEAD, ATM_CI_IDENTITY

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

TEAM_LEAD = ATM_TEAM_LEAD
CI_IDENTITY = ATM_CI_IDENTITY

# Patterns in atm send stderr that indicate permanent roster failure
_PERMANENT_FAILURE_PATTERNS = [
    "not a member",
    "not found in team",
    "unknown member",
    "no such member",
    "is not in",
]

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # seconds
SEND_TIMEOUT = 5  # seconds per attempt


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _atm_binary() -> str | None:
    """Resolve atm binary from PATH. Returns None if not found."""
    return shutil.which("atm")


def _is_permanent_failure(stderr: str) -> bool:
    """Check if atm send stderr indicates a permanent roster failure."""
    stderr_lower = stderr.lower()
    return any(p in stderr_lower for p in _PERMANENT_FAILURE_PATTERNS)


# ═══════════════════════════════════════════════════════════════════════════
# Message formatting helpers — pure functions, testable without ATM
# ═══════════════════════════════════════════════════════════════════════════

def format_conflict_files(files: list[str], max_display: int = 6) -> str:
    """Format a list of conflicting files for notification display.

    Always includes the total count. Caps display at max_display files.
    When there are more, appends '(N more)'.

    Args:
        files: list of file paths
        max_display: maximum files to display (default 6)

    Returns:
        Formatted string like:
          src/core/pipeline.rs
          src/core/config.rs
        Or when > max_display:
          src/a.rs
          src/b.rs
          ...
          (3 more)

    Corner cases:
        - Empty list: returns empty string
        - Single file: no count suffix
        - Exactly max_display: no '(N more)'

    >>> format_conflict_files([])
    ''
    >>> format_conflict_files(['a.rs'])
    '  a.rs'
    >>> format_conflict_files(['a.rs', 'b.rs'])
    '  a.rs\\n  b.rs'
    >>> format_conflict_files(['a.rs', 'b.rs', 'c.rs'], max_display=2)
    '  a.rs\\n  b.rs\\n  (1 more)'
    """
    if not files:
        return ""

    displayed = files[:max_display]
    remaining = len(files) - len(displayed)

    lines = [f"  {f}" for f in displayed]
    if remaining > 0:
        lines.append(f"  ({remaining} more)")

    return "\n".join(lines)


def format_notification(
    requested_by: str | None,
    subject: str,
    body: str,
) -> str:
    """Format a single notification message with identity header and body.

    The identity header follows the sudo/SUDO_USER pattern:
    - requested_by set:  "From: ci (on behalf of rand)"
    - requested_by None: "From: ci"

    Returns the full message string ready for atm send.
    """
    header = _format_identity_header(requested_by)
    return f"{header}\n\n{subject}\n\n{body}"


def format_batch_notification(
    requested_by: str | None,
    notifications: list[tuple[str, str]],  # [(subject, body), ...]
) -> str:
    """Format a batch of notifications into a single message.

    Multiple events for the same requesting identity are batched into
    one message with a summary header and individual sections.

    Args:
        requested_by: ATM identity of the requestor (or None)
        notifications: list of (subject, body) pairs

    Returns:
        Formatted message string.
        Single notification: same as format_notification.
        Multiple (>1): summary header + sections.

    Corner cases:
        - Empty list: returns empty string
        - Single item: simple format (no "N events" header)
        - Multiple items: batched with summary
    """
    if not notifications:
        return ""

    header = _format_identity_header(requested_by)

    if len(notifications) == 1:
        subject, body = notifications[0]
        return f"{header}\n\n{subject}\n\n{body}"

    # Batched: summary header + sections
    lines = [header, "", f"PR status update — {len(notifications)} events", ""]
    for i, (subject, body) in enumerate(notifications):
        lines.append(f"{subject}")
        lines.append(body)
        if i < len(notifications) - 1:
            lines.append("")

    return "\n".join(lines)


def _format_identity_header(requested_by: str | None) -> str:
    """Format the identity header line.

    >>> _format_identity_header("rand")
    'From: ci (on behalf of rand)'
    >>> _format_identity_header(None)
    'From: ci'
    >>> _format_identity_header("")
    'From: ci'
    """
    if requested_by:
        return f"From: ci (on behalf of {requested_by})"
    return "From: ci"


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

def atm_configured() -> bool:
    """True if ATM_TEAM is set and atm binary is on PATH."""
    if not os.environ.get("ATM_TEAM"):
        return False
    return _atm_binary() is not None


# ═══════════════════════════════════════════════════════════════════════════
# Designated member resolution
# ═══════════════════════════════════════════════════════════════════════════

def atm_get_designated(db: sqlite3.Connection, owner_repo: str) -> str:
    """Read designated_member from repos table. Defaults to team-lead."""
    row = db.execute(
        "SELECT designated_member FROM repos WHERE owner_repo = ?",
        (owner_repo,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    return TEAM_LEAD


# ═══════════════════════════════════════════════════════════════════════════
# Low-level send
# ═══════════════════════════════════════════════════════════════════════════

def _atm_send(to: str, message: str) -> tuple[bool, bool]:
    """Send a formatted message to a team member via atm CLI.

    Args:
        to: ATM team member name
        message: pre-formatted message string (use format_notification
                 or format_batch_notification to produce this)

    Returns:
        (delivered, is_permanent_failure)
        - (True, _): message sent successfully
        - (False, True): permanent failure (member not in roster) — do not retry
        - (False, False): transient failure (timeout, lock, crash) — may retry

    Raises nothing — all failures are reported via the return tuple.
    """
    if not atm_configured():
        return (False, True)

    atm_path = _atm_binary()
    if not atm_path:
        return (False, True)

    env = os.environ.copy()
    env["ATM_IDENTITY"] = CI_IDENTITY

    try:
        proc = subprocess.run(
            [atm_path, "send", to, message],
            capture_output=True, text=True, timeout=SEND_TIMEOUT,
            env=env,
        )
        if proc.returncode == 0:
            return (True, False)

        # Non-zero exit: check stderr for permanent failure markers
        if _is_permanent_failure(proc.stderr):
            logger.warning(
                "atm send permanent failure: %s not in roster — %s",
                to, proc.stderr.strip(),
            )
            return (False, True)

        # Non-zero but no permanent marker — treat as transient
        logger.warning(
            "atm send transient failure (exit %d): %s",
            proc.returncode, proc.stderr.strip(),
        )
        return (False, False)

    except subprocess.TimeoutExpired:
        logger.warning("atm send timeout for %s", to)
        return (False, False)

    except FileNotFoundError:
        logger.error("atm binary not found at %s", atm_path)
        return (False, True)

    except Exception as exc:
        logger.warning("atm send unexpected error: %s", exc)
        return (False, False)


# ═══════════════════════════════════════════════════════════════════════════
# Notification routing with retry and fallback
# ═══════════════════════════════════════════════════════════════════════════

def atm_notify(
    db: sqlite3.Connection,
    owner_repo: str,
    requested_by: str | None,
    notifications: list[tuple[str, str]],  # [(subject, body), ...]
) -> bool:
    """Route one or more notifications through the fallback chain.

    Identity resolution:
    - requested_by is set → try to send to that member first
    - requested_by is None or not in roster → use designated member
    - All messages include "From: ci (on behalf of <requested_by>)" header

    Multiple notifications for the same requesting identity are batched
    into a single ATM message.

    Retries transient failures up to 3x with exponential backoff (1s/2s/4s).
    Falls back to team-lead on permanent failure or retry exhaustion.
    Returns True if message was delivered (to target or fallback).

    Args:
        db: SQLite connection for reading repos table
        owner_repo: "owner/repo" string for designated member lookup
        requested_by: ATM identity of the requestor, or None
        notifications: list of (subject, body) pairs, at least one

    Returns:
        True if notification was delivered to someone, False if all
        delivery attempts failed (including team-lead fallback).
        True also when ATM is not configured (no-op, not an error).
    """
    if not atm_configured():
        return True  # no-op, not an error

    if not notifications:
        return True  # nothing to send

    # Resolve delivery target
    target = _resolve_target(db, owner_repo, requested_by)

    # Format the message (batched if multiple notifications)
    message = format_batch_notification(requested_by, notifications)

    # Attempt delivery with retry
    if _send_with_retry(target, message):
        return True

    # Fallback to team-lead (unless we already tried team-lead)
    if target != TEAM_LEAD:
        logger.info(
            "atm_notify: falling back from %s to %s for %s",
            target, TEAM_LEAD, owner_repo,
        )
        if _send_with_retry(TEAM_LEAD, message):
            return True

    # Terminal: team-lead also not reachable
    logger.error(
        "atm_notify: all delivery attempts failed for %s "
        "(target=%s, team-lead=%s)",
        owner_repo, target, TEAM_LEAD,
    )
    return False


def _resolve_target(
    db: sqlite3.Connection,
    owner_repo: str,
    requested_by: str | None,
) -> str:
    """Resolve the delivery target for a notification.

    - requested_by set and not 'ci' → that member
    - requested_by None or 'ci' → designated member
    """
    if requested_by and requested_by != CI_IDENTITY:
        return requested_by
    return atm_get_designated(db, owner_repo)


def _send_with_retry(to: str, message: str) -> bool:
    """Send with retry on transient failures. Returns True if delivered."""
    for attempt in range(MAX_RETRIES + 1):  # 0, 1, 2, 3
        delivered, permanent = _atm_send(to, message)
        if delivered:
            return True
        if permanent:
            return False  # don't retry roster failures

        # Transient — retry with backoff
        if attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF[attempt]
            logger.debug(
                "atm send retry %d/%d for %s in %ds",
                attempt + 1, MAX_RETRIES, to, delay,
            )
            time.sleep(delay)

    return False
