"""Adaptive polling interval calculator. Pure function — no I/O.

FR-31: next_interval(mode, rate_limit_remaining) → seconds
FR-31b: PR_CHANGED=30s, ACTIVE=5min, INACTIVE=20min
FR-36: Rate limit backoff: double interval when remaining < LOW_WATER

Public API:
    next_interval(mode, rate_limit_remaining, config) → int
    re_evaluate_mode(job_statuses, mergeable_states) → ActivityMode
"""

from dataclasses import dataclass
from enum import Enum


class ActivityMode(Enum):
    PR_CHANGED = "PR_CHANGED"  # post-push inspection: 30s
    ACTIVE = "ACTIVE"          # CI running: 5 min
    INACTIVE = "INACTIVE"      # nothing happening: 20 min


@dataclass
class PollConfig:
    pr_changed_interval: int = 30
    active_interval: int = 300           # 5 min (ADR-21)
    inactive_interval: int = 1200         # 20 min
    low_water: int = 1000                 # rate limit remaining threshold
    max_backoff: int = 3600               # max backoff (1 hour)
    backoff_multiplier: float = 2.0


_BASE_INTERVALS = {
    ActivityMode.PR_CHANGED: lambda c: c.pr_changed_interval,
    ActivityMode.ACTIVE: lambda c: c.active_interval,
    ActivityMode.INACTIVE: lambda c: c.inactive_interval,
}


def next_interval(
    mode: ActivityMode,
    rate_limit_remaining: int,
    config: PollConfig | None = None,
) -> int:
    """Calculate next poll interval based on mode and rate limit.

    FR-31: Mode determines base interval.
    FR-36: Rate limit below LOW_WATER doubles the interval, capped at max_backoff.
    """
    cfg = config or PollConfig()
    base = _BASE_INTERVALS[mode](cfg)

    if rate_limit_remaining < cfg.low_water:
        base = int(base * cfg.backoff_multiplier)
        if base > cfg.max_backoff:
            base = cfg.max_backoff

    return base


def re_evaluate_mode(
    job_statuses: set[str],
    mergeable_states: set[str],
) -> ActivityMode:
    """Determine activity mode from current state. Pure function.

    PR_CHANGED: any PR has mergeable=UNKNOWN (still computing after push)
    ACTIVE: any CI job QUEUED or IN_PROGRESS
    INACTIVE: neither condition — no active CI, all mergeable computed

    Note: PR_CHANGED takes priority over ACTIVE because post-push
    inspection is the most time-sensitive window.
    """
    # PR_CHANGED: GitHub is still computing mergeable after a push
    if "UNKNOWN" in mergeable_states:
        return ActivityMode.PR_CHANGED

    # ACTIVE: CI is running
    if job_statuses & {"QUEUED", "IN_PROGRESS"}:
        return ActivityMode.ACTIVE

    # INACTIVE: nothing is happening
    return ActivityMode.INACTIVE