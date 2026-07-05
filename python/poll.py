"""Adaptive polling interval calculator. Pure function — no I/O.

FR-31: next_interval(mode, rate_limit_remaining) → seconds
FR-36: Rate limit backoff: double interval when remaining < LOW_WATER

Public API:
    next_interval(mode, rate_limit_remaining, config) → int
    activity_mode(job_statuses, open_pr_count) → ActivityMode
"""

from dataclasses import dataclass
from enum import Enum


class ActivityMode(Enum):
    ACTIVE = "ACTIVE"       # CI running: 30s interval
    WATCHFUL = "WATCHFUL"   # Open PRs, no CI: 5min interval
    IDLE = "IDLE"           # No open PRs: 30min interval


@dataclass
class PollConfig:
    active_interval: int = 30
    watchful_interval: int = 300
    idle_interval: int = 1800
    low_water: int = 500
    max_backoff: int = 3600
    backoff_multiplier: float = 2.0


_BASE_INTERVALS = {
    ActivityMode.ACTIVE: lambda c: c.active_interval,
    ActivityMode.WATCHFUL: lambda c: c.watchful_interval,
    ActivityMode.IDLE: lambda c: c.idle_interval,
}


def next_interval(
    mode: ActivityMode,
    rate_limit_remaining: int,
    config: PollConfig | None = None,
) -> int:
    """Calculate next poll interval based on mode and rate limits.

    FR-31: Mode determines base interval.
    FR-36: Rate limit below LOW_WATER doubles the interval, capped.
    """
    cfg = config or PollConfig()
    base = _BASE_INTERVALS[mode](cfg)

    if rate_limit_remaining < cfg.low_water:
        base = int(base * cfg.backoff_multiplier)
        if base > cfg.max_backoff:
            base = cfg.max_backoff

    return base


def activity_mode(
    job_statuses: set[str],
    open_pr_count: int,
) -> ActivityMode:
    """Determine activity mode from current state. Pure function.

    job_statuses: set of current CI job statuses (QUEUED, IN_PROGRESS, COMPLETED)
    open_pr_count: number of open PRs
    """
    if job_statuses & {"QUEUED", "IN_PROGRESS"}:
        return ActivityMode.ACTIVE
    if open_pr_count > 0:
        return ActivityMode.WATCHFUL
    return ActivityMode.IDLE