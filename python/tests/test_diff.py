"""Tests for diff.py — state diffing engine (pure functions).

Tests FR-29, FR-30, FR-37.
All tests are pure function calls — no I/O, no mocks needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diff import diff_jobs, diff_prs, diff_conflicts, CiEvent, PrState, PrDiff
from gh.client import CheckRun, PrSnapshot


# ═══════════════════════════════════════════════════════════════════════════
# Job diffing (FR-29, FR-30)
# ═══════════════════════════════════════════════════════════════════════════

class TestDiffJobs:
    def test_new_job_produces_event(self):
        """FR-29: New job → CiEvent."""
        incoming = [("owner/repo", 1, [CheckRun(name="build", status="QUEUED")])]
        current = {}
        events = diff_jobs(incoming, current)
        assert len(events) == 1
        assert events[0].owner_repo == "owner/repo"
        assert events[0].pr_number == 1
        assert events[0].job_name == "build"
        assert events[0].status == "QUEUED"

    def test_unchanged_job_no_event(self):
        """FR-30: Identical poll → no events."""
        incoming = [("owner/repo", 1, [CheckRun(name="build", status="COMPLETED", conclusion="SUCCESS")])]
        current = {("owner/repo", 1, "build"): CiEvent(
            owner_repo="owner/repo", pr_number=1, job_name="build",
            status="COMPLETED", conclusion="SUCCESS",
        )}
        events = diff_jobs(incoming, current)
        assert len(events) == 0

    def test_status_change_produces_event(self):
        """FR-29: Status change → CiEvent."""
        incoming = [("owner/repo", 1, [CheckRun(name="build", status="IN_PROGRESS")])]
        current = {("owner/repo", 1, "build"): CiEvent(
            owner_repo="owner/repo", pr_number=1, job_name="build",
            status="QUEUED", conclusion=None,
        )}
        events = diff_jobs(incoming, current)
        assert len(events) == 1
        assert events[0].status == "IN_PROGRESS"

    def test_conclusion_change_produces_event(self):
        """FR-29: Conclusion change → CiEvent."""
        incoming = [("owner/repo", 1, [CheckRun(name="build", status="COMPLETED", conclusion="FAILURE")])]
        current = {("owner/repo", 1, "build"): CiEvent(
            owner_repo="owner/repo", pr_number=1, job_name="build",
            status="COMPLETED", conclusion="SUCCESS",
        )}
        events = diff_jobs(incoming, current)
        assert len(events) == 1
        assert events[0].conclusion == "FAILURE"

    def test_multiple_jobs_partial_change(self):
        """FR-30: Only changed jobs produce events, unchanged ones don't."""
        incoming = [
            ("owner/repo", 1, [
                CheckRun(name="build", status="COMPLETED", conclusion="SUCCESS"),
                CheckRun(name="lint", status="IN_PROGRESS"),
            ]),
        ]
        current = {
            ("owner/repo", 1, "build"): CiEvent(
                owner_repo="owner/repo", pr_number=1, job_name="build",
                status="COMPLETED", conclusion="SUCCESS",
            ),
            ("owner/repo", 1, "lint"): CiEvent(
                owner_repo="owner/repo", pr_number=1, job_name="lint",
                status="QUEUED", conclusion=None,
            ),
        }
        events = diff_jobs(incoming, current)
        assert len(events) == 1  # only lint changed
        assert events[0].job_name == "lint"

    def test_multiple_repos(self):
        """Multiple repos in one poll."""
        incoming = [
            ("a/b", 1, [CheckRun(name="build", status="QUEUED")]),
            ("c/d", 2, [CheckRun(name="test", status="IN_PROGRESS")]),
        ]
        events = diff_jobs(incoming, {})
        assert len(events) == 2
        repos = {(e.owner_repo, e.pr_number) for e in events}
        assert ("a/b", 1) in repos
        assert ("c/d", 2) in repos

    def test_empty_incoming(self):
        """Empty incoming → no events."""
        assert diff_jobs([], {}) == []

    def test_status_mapping(self):
        """PENDING/REQUESTED/WAITING → QUEUED."""
        incoming = [("r", 1, [
            CheckRun(name="a", status="PENDING"),
            CheckRun(name="b", status="REQUESTED"),
            CheckRun(name="c", status="WAITING"),
        ])]
        events = diff_jobs(incoming, {})
        for e in events:
            assert e.status == "QUEUED"

    def test_unknown_status_preserved(self):
        """Unknown statuses pass through unmapped."""
        incoming = [("r", 1, [CheckRun(name="x", status="CUSTOM_STATUS")])]
        events = diff_jobs(incoming, {})
        assert events[0].status == "CUSTOM_STATUS"

    def test_conclusion_none_preserved(self):
        """None conclusion stays None."""
        incoming = [("r", 1, [CheckRun(name="x", status="IN_PROGRESS", conclusion=None)])]
        events = diff_jobs(incoming, {})
        assert events[0].conclusion is None

    def test_no_false_positive_on_conclusion_None(self):
        """Job with conclusion=None → next poll with conclusion=None → no change."""
        incoming = [("r", 1, [CheckRun(name="x", status="IN_PROGRESS", conclusion=None)])]
        current = {("r", 1, "x"): CiEvent(
            owner_repo="r", pr_number=1, job_name="x",
            status="IN_PROGRESS", conclusion=None,
        )}
        events = diff_jobs(incoming, current)
        assert len(events) == 0


# ═══════════════════════════════════════════════════════════════════════════
# PR diffing
# ═══════════════════════════════════════════════════════════════════════════

class TestDiffPrs:
    def test_new_pr_added(self):
        """New PR → added."""
        incoming = [PrSnapshot(number=1, state="OPEN")]
        diff = diff_prs(incoming, {})
        assert len(diff.added) == 1
        assert diff.added[0].number == 1
        assert diff.added[0].state == "OPEN"

    def test_pr_state_change_updated(self):
        """PR state changed → updated."""
        incoming = [PrSnapshot(number=1, state="MERGED")]
        current = {1: PrState(number=1, state="OPEN")}
        diff = diff_prs(incoming, current)
        assert len(diff.updated) == 1
        assert diff.updated[0].state == "MERGED"

    def test_mergeable_change_updated(self):
        """Mergeable changed → updated."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="MERGEABLE")}
        diff = diff_prs(incoming, current)
        assert len(diff.updated) == 1
        assert diff.updated[0].mergeable == "CONFLICTING"

    def test_unchanged_pr_not_updated(self):
        """Unchanged PR → no update."""
        incoming = [PrSnapshot(number=1, state="OPEN", mergeable="MERGEABLE")]
        current = {1: PrState(number=1, state="OPEN", mergeable="MERGEABLE")}
        diff = diff_prs(incoming, current)
        assert len(diff.updated) == 0

    def test_pr_not_in_incoming_closed(self):
        """PR in current but not in incoming → closed."""
        incoming = []
        current = {1: PrState(number=1, state="OPEN")}
        diff = diff_prs(incoming, current)
        assert len(diff.closed) == 1
        assert diff.closed[0].number == 1

    def test_pr_closed_explicitly(self):
        """PR closed in incoming → not in closed list (state=CLOSED is an update)."""
        incoming = [PrSnapshot(number=1, state="CLOSED")]
        current = {1: PrState(number=1, state="OPEN")}
        diff = diff_prs(incoming, current)
        assert len(diff.closed) == 0  # PR is still in incoming
        assert len(diff.updated) == 1  # state changed

    def test_mixed_changes(self):
        """Mix of added, updated, closed."""
        incoming = [
            PrSnapshot(number=1, state="OPEN"),           # new
            PrSnapshot(number=2, state="MERGED"),         # updated
        ]
        current = {
            2: PrState(number=2, state="OPEN"),
            3: PrState(number=3, state="OPEN"),  # closed
        }
        diff = diff_prs(incoming, current)
        assert len(diff.added) == 1
        assert diff.added[0].number == 1
        assert len(diff.updated) == 1
        assert diff.updated[0].number == 2
        assert len(diff.closed) == 1
        assert diff.closed[0].number == 3


# ═══════════════════════════════════════════════════════════════════════════
# Conflict detection (FR-37)
# ═══════════════════════════════════════════════════════════════════════════

class TestDiffConflicts:
    def test_new_conflict_detected(self):
        """FR-37: mergeable → CONFLICTING detected."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="MERGEABLE")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 1
        assert conflicts[0] == ("owner/repo", 1)

    def test_existing_conflict_not_reported(self):
        """Already CONFLICTING → not reported again."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="CONFLICTING")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 0

    def test_resolved_conflict_not_reported(self):
        """CONFLICTING → MERGEABLE → not reported."""
        incoming = [PrSnapshot(number=1, mergeable="MERGEABLE")]
        current = {1: PrState(number=1, mergeable="CONFLICTING")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 0

    def test_unknown_to_conflict(self):
        """UNKNOWN → CONFLICTING → reported."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="UNKNOWN")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 1

    def test_new_pr_unknown_not_reported(self):
        """New PR with UNKNOWN → not a conflict."""
        incoming = [PrSnapshot(number=1, mergeable="UNKNOWN")]
        current = {}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 0

    def test_multiple_conflicts(self):
        """Multiple PRs reporting conflicts."""
        incoming = [
            PrSnapshot(number=1, mergeable="CONFLICTING"),
            PrSnapshot(number=2, mergeable="CONFLICTING"),
        ]
        current = {
            1: PrState(number=1, mergeable="MERGEABLE"),
            2: PrState(number=2, mergeable="UNKNOWN"),
        }
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 2


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR29_only_changed_jobs(self):
        """FR-29: diff_jobs returns only jobs where status/conclusion differs."""
        incoming = [
            ("r", 1, [
                CheckRun(name="a", status="QUEUED"),          # new
                CheckRun(name="b", status="COMPLETED", conclusion="SUCCESS"),  # unchanged
                CheckRun(name="c", status="IN_PROGRESS"),     # changed
            ]),
        ]
        current = {
            ("r", 1, "b"): CiEvent(owner_repo="r", pr_number=1, job_name="b",
                                    status="COMPLETED", conclusion="SUCCESS"),
            ("r", 1, "c"): CiEvent(owner_repo="r", pr_number=1, job_name="c",
                                    status="QUEUED", conclusion=None),
        }
        events = diff_jobs(incoming, current)
        names = {e.job_name for e in events}
        assert names == {"a", "c"}  # b unchanged, a new, c changed

    def test_FR30_no_heartbeat(self):
        """FR-30: Identical poll produces empty diff — no heartbeat."""
        incoming = [
            ("r", 1, [CheckRun(name="x", status="COMPLETED", conclusion="SUCCESS")]),
        ]
        current = {
            ("r", 1, "x"): CiEvent(owner_repo="r", pr_number=1, job_name="x",
                                    status="COMPLETED", conclusion="SUCCESS"),
        }
        # 100 identical polls
        for _ in range(100):
            events = diff_jobs(incoming, current)
            assert len(events) == 0, "heartbeat detected — should be empty"

    def test_FR37_conflict_detection(self):
        """FR-37: mergeable switch to CONFLICTING detected."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="MERGEABLE")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 1
        assert conflicts[0] == ("owner/repo", 1)

    def test_FR37_no_false_conflict(self):
        """FR-37: CONFLICTING → CONFLICTING → no report."""
        incoming = [PrSnapshot(number=1, mergeable="CONFLICTING")]
        current = {1: PrState(number=1, mergeable="CONFLICTING")}
        conflicts = diff_conflicts("owner/repo", incoming, current)
        assert len(conflicts) == 0