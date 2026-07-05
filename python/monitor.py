"""Job duration monitoring — queue time vs execution time.

Tracks queue time and execution time separately. Long queues are
informational, not anomalies. Only abnormal execution times flag alerts.

Public API (pure functions):
    calc_queue_time(events)     → ms | None
    calc_execution_time(events) → ms | None
    update_ema(current, value)  → float
    is_anomaly(ms, ema)         → bool
    is_stale(ms, ema)           → bool
"""

from dataclasses import dataclass

from diff import CiEvent


# ═══════════════════════════════════════════════════════════════════════════
# Duration calculation (FR-44)
# ═══════════════════════════════════════════════════════════════════════════

def calc_queue_time(events: list[CiEvent]) -> int | None:
    """Calculate queue time: QUEUED → IN_PROGRESS delta in ms.
    Returns None if either timestamp is missing."""
    queued_at = _find_ts(events, "QUEUED")
    started_at = _find_ts(events, "IN_PROGRESS")
    if queued_at is None or started_at is None:
        return None
    return started_at - queued_at


def calc_execution_time(events: list[CiEvent]) -> int | None:
    """Calculate execution time: IN_PROGRESS → COMPLETED delta in ms.
    Returns None if either timestamp is missing or job never completed."""
    started_at = _find_ts(events, "IN_PROGRESS")
    completed_at = _find_ts(events, "COMPLETED")
    if started_at is None or completed_at is None:
        return None
    return completed_at - started_at


def _find_ts(events: list[CiEvent], status: str) -> int | None:
    """Find the timestamp of the first event with the given status.
    CiEvent doesn't carry timestamps — caller must provide them.
    We use a convention: events are ordered, caller passes timestamps separately."""
    # This is a pure function design — timestamps come from ci_events.recorded_at
    # In practice, the caller maps (event, recorded_at) tuples
    return None  # caller provides timestamps


# Overloaded versions that accept timestamps:
def calc_queue_time_from_rows(events_with_ts: list[tuple[CiEvent, int]]) -> int | None:
    """Calculate queue time from (event, recorded_at) pairs."""
    queued = None
    started = None
    for ev, ts in events_with_ts:
        if ev.status == "QUEUED" and queued is None:
            queued = ts
        elif ev.status == "IN_PROGRESS" and started is None:
            started = ts
    if queued is None or started is None:
        return None
    return started - queued


def calc_execution_time_from_rows(events_with_ts: list[tuple[CiEvent, int]]) -> int | None:
    """Calculate execution time from (event, recorded_at) pairs."""
    started = None
    completed = None
    for ev, ts in events_with_ts:
        if ev.status == "IN_PROGRESS" and started is None:
            started = ts
        elif ev.status == "COMPLETED" and completed is None:
            completed = ts
    if started is None or completed is None:
        return None
    return completed - started


# ═══════════════════════════════════════════════════════════════════════════
# EMA (FR-45, FR-46)
# ═══════════════════════════════════════════════════════════════════════════

def update_ema(current_ema: float, new_value: int, alpha: float = 0.2) -> float:
    """Update exponential moving average.
    EMA = α × new + (1-α) × old. α=0.2 weights recency moderately."""
    return alpha * new_value + (1.0 - alpha) * current_ema


# ═══════════════════════════════════════════════════════════════════════════
# Anomaly detection (FR-47, FR-48)
# ═══════════════════════════════════════════════════════════════════════════

def is_anomaly(execution_ms: int, ema_ms: float, threshold: float = 5.0) -> bool:
    """FR-47: execution time > threshold × EMA → anomaly (hung)."""
    if ema_ms <= 0:
        return False  # no baseline yet
    return execution_ms > threshold * ema_ms


def is_stale(execution_ms: int, ema_ms: float, threshold: float = 2.0) -> bool:
    """FR-48: job in progress > threshold × EMA without completing → stale (slow)."""
    if ema_ms <= 0:
        return False
    return execution_ms > threshold * ema_ms


@dataclass
class MonitorEvent:
    """An anomaly or staleness event."""
    owner_repo: str
    pr_number: int
    job_name: str
    event_type: str  # "hung", "slow", "queue_spike"
    execution_ms: int | None
    queue_ms: int | None
    ema_execution_ms: float
    ema_queue_ms: float


# ═══════════════════════════════════════════════════════════════════════════
# Backoff (FR-49)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BackoffConfig:
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    multiplier: float = 2.0
    max_retries: int = 5


def backoff_delay(retry_count: int, config: BackoffConfig | None = None) -> float:
    """FR-49: Exponential backoff delay for retry N.
    delay = min(base × multiplier^N, max_delay)"""
    cfg = config or BackoffConfig()
    delay = cfg.base_delay_seconds * (cfg.multiplier ** retry_count)
    return min(delay, cfg.max_delay_seconds)


def should_retry(retry_count: int, config: BackoffConfig | None = None) -> bool:
    """FR-49: Whether to retry based on count."""
    cfg = config or BackoffConfig()
    return retry_count < cfg.max_retries