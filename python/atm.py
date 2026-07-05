"""ATM adapter module for continuity notifications.

Self-contained module. All ATM CLI invocation, notification routing,
retry/backoff, and fallback logic lives here. Callers never branch on
ATM availability — call atm_notify() and the module handles everything
internally.

Public API:
    atm_configured()       → bool
    atm_send(to, subject, body) → bool
    atm_get_designated(db, owner_repo) → str
    atm_notify(db, owner_repo, target, subject, body) → bool
"""

import logging
import os
import shutil
import subprocess
import sqlite3
import time

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Configuration checks
# ═══════════════════════════════════════════════════════════════════════════

ATM_BINARY = "/opt/homebrew/bin/atm"
TEAM_LEAD = "team-lead"
CI_IDENTITY = "ci"

# Patterns in atm send stderr that indicate permanent roster failure
_PERMANENT_FAILURE_PATTERNS = [
    "not a member",
    "not found in team",
    "unknown member",
    "no such member",
    "is not in",
]


def atm_configured() -> bool:
    """True if ATM_TEAM is set and atm binary is available."""
    if not os.environ.get("ATM_TEAM"):
        return False
    return shutil.which("atm") is not None or os.path.isfile(ATM_BINARY)


# ═══════════════════════════════════════════════════════════════════════════
# Low-level send
# ═══════════════════════════════════════════════════════════════════════════

def atm_send(to: str, subject: str, body: str) -> tuple[bool, bool]:
    """Send an ATM message to a team member.

    Returns (delivered, is_permanent_failure).
    - (True, _): message sent successfully
    - (False, True): permanent failure (member not in roster) — do not retry
    - (False, False): transient failure (timeout, lock, crash) — may retry

    Raises nothing — all failures are reported via the return tuple.
    """
    if not atm_configured():
        return (False, True)

    message = f"{subject}\n\n{body}"
    env = os.environ.copy()
    env["ATM_IDENTITY"] = CI_IDENTITY

    atm_path = shutil.which("atm") or ATM_BINARY

    try:
        proc = subprocess.run(
            [atm_path, "send", to, message],
            capture_output=True, text=True, timeout=5,
            env=env,
        )
        if proc.returncode == 0:
            return (True, False)

        # Non-zero exit: check stderr for permanent failure markers
        stderr_lower = proc.stderr.lower()
        for pattern in _PERMANENT_FAILURE_PATTERNS:
            if pattern in stderr_lower:
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
# Notification routing with retry and fallback
# ═══════════════════════════════════════════════════════════════════════════

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # seconds


def atm_notify(
    db: sqlite3.Connection,
    owner_repo: str,
    target: str | None,
    subject: str,
    body: str,
) -> bool:
    """Route a notification through the fallback chain.

    target=None means route directly to designated member.
    target='ci' is treated as None (prevents self-send loops).

    Retries transient failures up to 3x with exponential backoff.
    Falls back to team-lead on permanent failure or retry exhaustion.
    Returns True if message was delivered (to target or fallback).
    """
    if not atm_configured():
        return True  # no-op, not an error

    # Resolve target
    if target is None or target == CI_IDENTITY:
        resolved = atm_get_designated(db, owner_repo)
    else:
        resolved = target

    # Attempt delivery with retry
    delivered = _send_with_retry(resolved, subject, body)
    if delivered:
        return True

    # Fallback to team-lead (unless we already tried team-lead)
    if resolved != TEAM_LEAD:
        logger.info(
            "atm_notify: falling back from %s to %s for %s",
            resolved, TEAM_LEAD, owner_repo,
        )
        delivered = _send_with_retry(TEAM_LEAD, subject, body)
        if delivered:
            return True

    # Terminal: team-lead also not reachable
    logger.error(
        "atm_notify: all delivery attempts failed for %s "
        "(target=%s, team-lead=%s)",
        owner_repo, resolved, TEAM_LEAD,
    )
    return False


def _send_with_retry(to: str, subject: str, body: str) -> bool:
    """Send with retry on transient failures. Returns True if delivered."""
    for attempt in range(MAX_RETRIES + 1):  # 0, 1, 2, 3 (initial + 3 retries)
        delivered, permanent = atm_send(to, subject, body)
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
