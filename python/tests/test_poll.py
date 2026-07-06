"""Tests for poll.py — adaptive interval calculator (pure functions)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poll import next_interval, re_evaluate_mode, ActivityMode, PollConfig


class TestNextInterval:
    def test_pr_changed_mode_default(self):
        """FR-31: PR_CHANGED → 30s."""
        assert next_interval(ActivityMode.PR_CHANGED, 5000) == 30

    def test_active_mode_default(self):
        """FR-31b: ACTIVE → 300s (5 min)."""
        assert next_interval(ActivityMode.ACTIVE, 5000) == 300

    def test_inactive_mode_default(self):
        """FR-31b: INACTIVE → 1200s (20 min)."""
        assert next_interval(ActivityMode.INACTIVE, 5000) == 1200

    def test_rate_limit_backoff(self):
        """FR-36: Rate limit < LOW_WATER → doubled."""
        assert next_interval(ActivityMode.ACTIVE, 100, PollConfig(low_water=1000)) == 600

    def test_rate_limit_no_backoff(self):
        """Rate limit above LOW_WATER → no change."""
        assert next_interval(ActivityMode.ACTIVE, 1100, PollConfig(low_water=1000)) == 300

    def test_backoff_capped(self):
        """FR-36: Backoff capped at max_backoff."""
        assert next_interval(
            ActivityMode.PR_CHANGED, 10,
            PollConfig(pr_changed_interval=30, low_water=1000, max_backoff=90),
        ) == 60  # 30 * 2 = 60, under cap of 90

    def test_backoff_at_cap(self):
        """Backoff hits max_backoff."""
        assert next_interval(
            ActivityMode.PR_CHANGED, 10,
            PollConfig(pr_changed_interval=30, low_water=1000, max_backoff=45),
        ) == 45  # capped at 45

    def test_custom_config(self):
        """Custom intervals."""
        assert next_interval(
            ActivityMode.ACTIVE, 5000,
            PollConfig(active_interval=150),
        ) == 150

    def test_rate_limit_exactly_at_water(self):
        """Exactly at LOW_WATER → no backoff (only below triggers)."""
        assert next_interval(ActivityMode.ACTIVE, 1000, PollConfig(low_water=1000)) == 300

    def test_rate_limit_one_below_water(self):
        """One below LOW_WATER → backoff."""
        assert next_interval(ActivityMode.ACTIVE, 999, PollConfig(low_water=1000)) == 600


class TestReEvaluateMode:
    def test_pr_changed_when_unknown_mergeable(self):
        """PR_CHANGED when any PR has mergeable=UNKNOWN."""
        assert re_evaluate_mode(set(), {"UNKNOWN"}) == ActivityMode.PR_CHANGED

    def test_pr_changed_overrides_active(self):
        """PR_CHANGED takes priority over ACTIVE."""
        assert re_evaluate_mode({"QUEUED"}, {"UNKNOWN"}) == ActivityMode.PR_CHANGED

    def test_active_when_queued(self):
        assert re_evaluate_mode({"QUEUED"}, {"MERGEABLE"}) == ActivityMode.ACTIVE

    def test_active_when_in_progress(self):
        assert re_evaluate_mode({"IN_PROGRESS"}, {"MERGEABLE"}) == ActivityMode.ACTIVE

    def test_active_when_both(self):
        assert re_evaluate_mode(
            {"QUEUED", "IN_PROGRESS", "COMPLETED"}, {"MERGEABLE"},
        ) == ActivityMode.ACTIVE

    def test_inactive_when_completed_no_unknown(self):
        """INACTIVE: CI done, no UNKNOWN mergeable."""
        assert re_evaluate_mode({"COMPLETED"}, {"MERGEABLE"}) == ActivityMode.INACTIVE

    def test_inactive_when_empty(self):
        assert re_evaluate_mode(set(), set()) == ActivityMode.INACTIVE


class TestAdr:
    def test_FR31_mode_intervals(self):
        """FR-31: Each mode has correct default interval."""
        assert next_interval(ActivityMode.PR_CHANGED, 5000) == 30
        assert next_interval(ActivityMode.ACTIVE, 5000) == 300
        assert next_interval(ActivityMode.INACTIVE, 5000) == 1200

    def test_FR31b_post_push_delay(self):
        """FR-31b: PollConfig has 60s post_push_delay."""
        assert PollConfig().post_push_delay == 60

    def test_FR36_backoff(self):
        """FR-36: Rate limit backoff when < LOW_WATER."""
        normal = next_interval(ActivityMode.ACTIVE, 5000)
        backed_off = next_interval(ActivityMode.ACTIVE, 100, PollConfig(low_water=1000))
        assert backed_off > normal
        assert backed_off == normal * 2