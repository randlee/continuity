"""Tests for monitor.py — duration tracking + anomaly detection.

Tests FR-44 through FR-49.
All pure functions — no I/O, no mocks needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diff import CiEvent
from monitor import (
    calc_queue_time_from_rows,
    calc_execution_time_from_rows,
    update_ema,
    is_anomaly,
    is_stale,
    backoff_delay,
    should_retry,
    MonitorEvent,
    BackoffConfig,
)


# ═══════════════════════════════════════════════════════════════════════════
# Duration calculation (FR-44)
# ═══════════════════════════════════════════════════════════════════════════

class TestQueueTime:
    def test_queue_time_normal(self):
        """FR-44: Queue time = QUEUED → IN_PROGRESS delta."""
        events = [
            (CiEvent("r", 1, "build", "QUEUED", None), 1000),
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 5000),
        ]
        assert calc_queue_time_from_rows(events) == 4000  # 5000 - 1000

    def test_queue_time_missing_queued(self):
        """No QUEUED event → None."""
        events = [
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 5000),
        ]
        assert calc_queue_time_from_rows(events) is None

    def test_queue_time_missing_started(self):
        """No IN_PROGRESS event → None."""
        events = [
            (CiEvent("r", 1, "build", "QUEUED", None), 1000),
        ]
        assert calc_queue_time_from_rows(events) is None

    def test_queue_time_empty(self):
        assert calc_queue_time_from_rows([]) is None

    def test_queue_time_uses_first_occurrence(self):
        """Multiple QUEUED events → use first one."""
        events = [
            (CiEvent("r", 1, "build", "QUEUED", None), 1000),
            (CiEvent("r", 1, "build", "QUEUED", None), 2000),  # duplicate
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 5000),
        ]
        assert calc_queue_time_from_rows(events) == 4000


class TestExecutionTime:
    def test_execution_time_normal(self):
        """FR-44: Execution time = IN_PROGRESS → COMPLETED delta."""
        events = [
            (CiEvent("r", 1, "build", "QUEUED", None), 1000),
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 5000),
            (CiEvent("r", 1, "build", "COMPLETED", "SUCCESS"), 25000),
        ]
        assert calc_execution_time_from_rows(events) == 20000  # 25000 - 5000

    def test_execution_time_missing_started(self):
        events = [
            (CiEvent("r", 1, "build", "COMPLETED", "SUCCESS"), 25000),
        ]
        assert calc_execution_time_from_rows(events) is None

    def test_execution_time_missing_completed(self):
        events = [
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 5000),
        ]
        assert calc_execution_time_from_rows(events) is None

    def test_execution_time_empty(self):
        assert calc_execution_time_from_rows([]) is None

    def test_queue_and_execution_independent(self):
        """Long queue time doesn't affect execution time calculation."""
        events = [
            (CiEvent("r", 1, "build", "QUEUED", None), 0),       # 0
            (CiEvent("r", 1, "build", "IN_PROGRESS", None), 900000),  # 15 min queue!
            (CiEvent("r", 1, "build", "COMPLETED", "SUCCESS"), 920000),  # +20s exec
        ]
        queue = calc_queue_time_from_rows(events)
        exec_time = calc_execution_time_from_rows(events)
        assert queue == 900000  # long queue
        assert exec_time == 20000  # normal execution
        # FR-44: queue and execution are separate — long queue ≠ slow job


# ═══════════════════════════════════════════════════════════════════════════
# EMA (FR-45, FR-46)
# ═══════════════════════════════════════════════════════════════════════════

class TestEma:
    def test_update_ema_initial(self):
        """First value → EMA converges toward it."""
        ema = update_ema(0.0, 10000)
        assert ema == 2000.0  # α=0.2 × 10000 = 2000

    def test_update_ema_converges(self):
        """EMA converges toward steady state."""
        ema = 0.0
        for _ in range(10):
            ema = update_ema(ema, 10000)
        # After many updates, should approach 10000
        assert 8000 < ema < 10000

    def test_update_ema_reacts_to_spike(self):
        """EMA reacts to a spike but doesn't jump to it."""
        ema = 10000.0  # stable EMA
        ema = update_ema(ema, 100000)  # 10x spike
        # EMA should increase but not jump to 100000
        assert ema > 10000
        assert ema < 50000  # not a full jump

    def test_update_ema_custom_alpha(self):
        """Custom alpha weights recency differently."""
        ema_fast = update_ema(0.0, 10000, alpha=0.5)
        ema_slow = update_ema(0.0, 10000, alpha=0.1)
        assert ema_fast > ema_slow  # higher alpha → more weight on new value

    def test_update_ema_alpha_zero(self):
        """α=0 → EMA never changes."""
        assert update_ema(100.0, 99999, alpha=0.0) == 100.0

    def test_update_ema_alpha_one(self):
        """α=1 → EMA jumps to new value immediately."""
        assert update_ema(100.0, 99999, alpha=1.0) == 99999.0


# ═══════════════════════════════════════════════════════════════════════════
# Anomaly detection (FR-47, FR-48)
# ═══════════════════════════════════════════════════════════════════════════

class TestAnomaly:
    def test_hung_detected(self):
        """FR-47: execution > 5× EMA → hung."""
        assert is_anomaly(500000, 10000.0)  # 50s vs 10s EMA → hung

    def test_hung_not_detected_normal(self):
        """Normal execution → not hung."""
        assert not is_anomaly(15000, 10000.0)

    def test_hung_exactly_at_threshold(self):
        """Exactly at threshold → not hung (< is strict)."""
        assert not is_anomaly(50000, 10000.0, threshold=5.0)

    def test_hung_custom_threshold(self):
        """Custom threshold."""
        assert is_anomaly(30001, 10000.0, threshold=3.0)  # > 3x
        assert not is_anomaly(30000, 10000.0, threshold=3.0)  # exactly 3x → not hung

    def test_hung_no_baseline(self):
        """No EMA baseline yet → never hung."""
        assert not is_anomaly(999999, 0.0)


class TestStale:
    def test_stale_detected(self):
        """FR-48: in progress > 2× EMA → stale/slow."""
        assert is_stale(250000, 10000.0)  # 25s vs 10s EMA → stale

    def test_stale_not_detected(self):
        assert not is_stale(15000, 10000.0)

    def test_stale_threshold(self):
        """Custom threshold."""
        assert is_stale(20001, 10000.0, threshold=2.0)  # > 2x
        assert not is_stale(20000, 10000.0, threshold=2.0)  # exactly 2x → not stale

    def test_stale_no_baseline(self):
        assert not is_stale(999999, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# Backoff (FR-49)
# ═══════════════════════════════════════════════════════════════════════════

class TestBackoff:
    def test_exponential_growth(self):
        """Each retry doubles the delay."""
        assert backoff_delay(0) == 1.0
        assert backoff_delay(1) == 2.0
        assert backoff_delay(2) == 4.0
        assert backoff_delay(3) == 8.0

    def test_capped_at_max(self):
        """Delay never exceeds max_delay."""
        assert backoff_delay(10, BackoffConfig(max_delay_seconds=60)) == 60.0

    def test_should_retry(self):
        """Retry within limit."""
        assert should_retry(0)
        assert should_retry(4)
        assert not should_retry(5)
        assert not should_retry(10)

    def test_custom_max_retries(self):
        assert should_retry(2, BackoffConfig(max_retries=3))
        assert not should_retry(3, BackoffConfig(max_retries=3))


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class TestMonitorEvent:
    def test_fields(self):
        ev = MonitorEvent(
            owner_repo="r", pr_number=1, job_name="build",
            event_type="hung", execution_ms=500000, queue_ms=10000,
            ema_execution_ms=10000.0, ema_queue_ms=5000.0,
        )
        assert ev.event_type == "hung"
        assert ev.execution_ms == 500000


# ═══════════════════════════════════════════════════════════════════════════
# ADR
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR44_queue_and_execution_separate(self):
        """FR-44: Queue and execution calculated independently."""
        events = [
            (CiEvent("r", 1, "j", "QUEUED", None), 0),
            (CiEvent("r", 1, "j", "IN_PROGRESS", None), 1000),
            (CiEvent("r", 1, "j", "COMPLETED", "SUCCESS"), 5000),
        ]
        q = calc_queue_time_from_rows(events)
        e = calc_execution_time_from_rows(events)
        assert q == 1000
        assert e == 4000

    def test_FR45_ema_tracks_execution(self):
        """FR-45: EMA of execution time updated per job."""
        ema = 10000.0
        ema = update_ema(ema, 12000)  # slightly slower
        assert ema > 10000.0  # EMA increased
        assert ema < 12000.0  # but not to full value

    def test_FR46_ema_tracks_queue(self):
        """FR-46: EMA of queue time tracked (same formula, different metric)."""
        ema_queue = 5000.0
        ema_queue = update_ema(ema_queue, 900000)  # huge queue spike
        assert ema_queue > 5000.0  # EMA reacts

    def test_FR47_long_execution_not_queue(self):
        """FR-47: Anomaly based on execution, not total time."""
        # Scenario: 15 min queue, 20s execution — NOT hung
        # If we used total time (15m20s), it would look like a hung job
        # But execution is only 20s → normal
        execution_ms = 20000
        ema_execution = 10000.0
        assert not is_anomaly(execution_ms, ema_execution)
        # FR-47: only execution time matters for anomaly detection

    def test_FR48_stale_detection(self):
        """FR-48: Job in progress too long without completing."""
        assert is_stale(30000, 10000.0)  # 3x EMA
        assert not is_stale(15000, 10000.0)  # within 2x

    def test_FR49_exponential_backoff(self):
        """FR-49: Exponential backoff with cap."""
        cfg = BackoffConfig(base_delay_seconds=1.0, max_delay_seconds=16.0)
        delays = [backoff_delay(i, cfg) for i in range(10)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 16.0, 16.0, 16.0, 16.0, 16.0]
        assert not should_retry(5, cfg)