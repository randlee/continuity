"""Tests for poll.py — adaptive interval calculator (pure functions)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poll import next_interval, activity_mode, ActivityMode, PollConfig


class TestNextInterval:
    def test_active_mode_default(self):
        """FR-31: ACTIVE → 30s."""
        assert next_interval(ActivityMode.ACTIVE, 5000) == 30

    def test_watchful_mode_default(self):
        """FR-31: WATCHFUL → 300s."""
        assert next_interval(ActivityMode.WATCHFUL, 5000) == 300

    def test_idle_mode_default(self):
        """FR-31: IDLE → 1800s."""
        assert next_interval(ActivityMode.IDLE, 5000) == 1800

    def test_rate_limit_backoff(self):
        """FR-36: Rate limit < LOW_WATER → doubled."""
        assert next_interval(ActivityMode.ACTIVE, 100, PollConfig(low_water=500)) == 60

    def test_rate_limit_no_backoff(self):
        """Rate limit above LOW_WATER → no change."""
        assert next_interval(ActivityMode.ACTIVE, 600, PollConfig(low_water=500)) == 30

    def test_backoff_capped(self):
        """FR-36: Backoff capped at max_backoff."""
        assert next_interval(
            ActivityMode.ACTIVE, 10,
            PollConfig(active_interval=30, low_water=500, max_backoff=90),
        ) == 60  # 30 * 2 = 60, under cap of 90

    def test_backoff_at_cap(self):
        """Backoff hits max_backoff."""
        assert next_interval(
            ActivityMode.ACTIVE, 10,
            PollConfig(active_interval=30, low_water=500, max_backoff=45),
        ) == 45  # capped at 45

    def test_custom_config(self):
        """Custom intervals."""
        assert next_interval(
            ActivityMode.ACTIVE, 5000,
            PollConfig(active_interval=15),
        ) == 15

    def test_rate_limit_exactly_at_water(self):
        """Exactly at LOW_WATER → no backoff (only below triggers)."""
        assert next_interval(ActivityMode.ACTIVE, 500, PollConfig(low_water=500)) == 30

    def test_rate_limit_one_below_water(self):
        """One below LOW_WATER → backoff."""
        assert next_interval(ActivityMode.ACTIVE, 499, PollConfig(low_water=500)) == 60


class TestActivityMode:
    def test_active_when_queued(self):
        assert activity_mode({"QUEUED"}, 1) == ActivityMode.ACTIVE

    def test_active_when_in_progress(self):
        assert activity_mode({"IN_PROGRESS"}, 1) == ActivityMode.ACTIVE

    def test_active_when_both(self):
        assert activity_mode({"QUEUED", "IN_PROGRESS", "COMPLETED"}, 5) == ActivityMode.ACTIVE

    def test_watchful_when_open_prs_no_ci(self):
        assert activity_mode({"COMPLETED"}, 3) == ActivityMode.WATCHFUL

    def test_watchful_when_open_prs_empty_statuses(self):
        assert activity_mode(set(), 1) == ActivityMode.WATCHFUL

    def test_idle_when_no_prs(self):
        assert activity_mode(set(), 0) == ActivityMode.IDLE

    def test_idle_when_no_prs_with_statuses(self):
        """Edge case: statuses exist but no open PRs → IDLE."""
        assert activity_mode({"COMPLETED"}, 0) == ActivityMode.IDLE


class TestAdr:
    def test_FR31_mode_intervals(self):
        """FR-31: Each mode has correct default interval."""
        assert next_interval(ActivityMode.ACTIVE, 5000) == 30
        assert next_interval(ActivityMode.WATCHFUL, 5000) == 300
        assert next_interval(ActivityMode.IDLE, 5000) == 1800

    def test_FR36_backoff(self):
        """FR-36: Rate limit backoff when < LOW_WATER."""
        normal = next_interval(ActivityMode.ACTIVE, 5000)
        backed_off = next_interval(ActivityMode.ACTIVE, 100, PollConfig(low_water=500))
        assert backed_off > normal
        assert backed_off == normal * 2