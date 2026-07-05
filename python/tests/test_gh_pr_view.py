"""Tests for gh/pr_view.py and gh/pr_checks.py."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from gh.pr_view import parse as parse_view
from gh.pr_checks import parse as parse_checks


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = db.ensure_db(db_path)
        yield c
        c.close()


def _repo_fn():
    return "test-owner/test-repo"


PR_VIEW_JSON = json.dumps({
    "number": 42, "headRefName": "feat/x", "headRefOid": "abc123",
    "mergeable": "MERGEABLE", "state": "OPEN",
    "statusCheckRollup": [
        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
        {"name": "coverage", "status": "QUEUED", "conclusion": None},
    ],
})


class TestPrView:
    def test_parses_pr_metadata(self, conn):
        parse_view(conn, ["pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        pr = conn.execute(
            "SELECT pr_number, branch, head_sha, mergeable, state FROM pull_requests"
        ).fetchone()
        assert pr[0] == 42
        assert pr[1] == "feat/x"
        assert pr[2] == "abc123"
        assert pr[3] == "MERGEABLE"
        assert pr[4] == "OPEN"

    def test_parses_ci_jobs(self, conn):
        parse_view(conn, ["pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        jobs = conn.execute(
            "SELECT job_name, status, conclusion FROM ci_events ORDER BY job_name"
        ).fetchall()
        assert len(jobs) == 4
        assert ("build", "COMPLETED", "SUCCESS") in jobs
        assert ("test", "IN_PROGRESS", None) in jobs
        assert ("coverage", "QUEUED", None) in jobs

    def test_state_diffing_no_duplicate(self, conn):
        """Identical polls produce no duplicate ci_events."""
        parse_view(conn, ["pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        parse_view(conn, ["pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        counts = conn.execute(
            "SELECT COUNT(*) FROM ci_events GROUP BY job_name"
        ).fetchall()
        for (c,) in counts:
            assert c == 1

    def test_state_diffing_records_change(self, conn):
        stdout1 = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [{"name": "t", "status": "QUEUED", "conclusion": None}],
        })
        stdout2 = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [{"name": "t", "status": "IN_PROGRESS", "conclusion": None}],
        })
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout1, _repo_fn)
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout2, _repo_fn)
        events = conn.execute(
            "SELECT status FROM ci_events WHERE job_name='t' ORDER BY recorded_at"
        ).fetchall()
        assert len(events) == 2
        assert events[0][0] == "QUEUED"
        assert events[1][0] == "IN_PROGRESS"

    def test_upsert_updates_pr(self, conn):
        stdout1 = json.dumps({"number": 1, "headRefName": "x"})
        stdout2 = json.dumps({"number": 1, "headRefName": "x", "state": "MERGED"})
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout1, _repo_fn)
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout2, _repo_fn)
        count = conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
        assert count == 1
        state = conn.execute("SELECT state FROM pull_requests").fetchone()[0]
        assert state == "MERGED"

    def test_invalid_json_does_not_crash(self, conn):
        parse_view(conn, ["pr", "view", "1", "--json", "..."], "not json", _repo_fn)
        # No exception

    def test_status_mapping(self, conn):
        """PENDING/REQUESTED/WAITING → QUEUED."""
        stdout = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [
                {"name": "a", "status": "PENDING", "conclusion": None},
                {"name": "b", "status": "REQUESTED", "conclusion": None},
                {"name": "c", "status": "WAITING", "conclusion": None},
            ],
        })
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout, _repo_fn)
        statuses = conn.execute(
            "SELECT status FROM ci_events"
        ).fetchall()
        for (s,) in statuses:
            assert s == "QUEUED"


class TestPrChecks:
    def test_parses_checks_output(self, conn):
        stdout = json.dumps([
            {"name": "build", "status": "IN_PROGRESS", "conclusion": None},
            {"name": "lint", "status": "QUEUED", "conclusion": None},
        ])
        parse_checks(conn, ["pr", "checks", "42", "--json"], stdout, _repo_fn)
        jobs = conn.execute(
            "SELECT job_name, status FROM ci_events ORDER BY job_name"
        ).fetchall()
        assert len(jobs) == 2
        assert ("build", "IN_PROGRESS") in jobs
        assert ("lint", "QUEUED") in jobs


class TestAdr:
    def test_FR11(self, conn):
        """FR-11: gh pr view --json statusCheckRollup parsed."""
        parse_view(conn, ["pr", "view", "42", "--json", "..."], PR_VIEW_JSON, _repo_fn)
        assert conn.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0] == 4

    def test_FR12(self, conn):
        """FR-12: gh pr checks --json parsed."""
        stdout = json.dumps([{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        parse_checks(conn, ["pr", "checks", "1", "--json"], stdout, _repo_fn)
        assert conn.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0] == 1

    def test_FR14_state_diffing(self, conn):
        stdout = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [{"name": "t", "status": "QUEUED", "conclusion": None}],
        })
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout, _repo_fn)
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout, _repo_fn)
        assert conn.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0] == 1

    def test_FR17_append_only(self, conn):
        stdout1 = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [{"name": "a", "status": "QUEUED", "conclusion": None}],
        })
        stdout2 = json.dumps({
            "number": 1, "headRefName": "x",
            "statusCheckRollup": [{"name": "a", "status": "IN_PROGRESS", "conclusion": None}],
        })
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout1, _repo_fn)
        parse_view(conn, ["pr", "view", "1", "--json", "..."], stdout2, _repo_fn)
        assert conn.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0] == 2


class TestRPrefix:
    """Corner case: gh -R owner/repo prefix in args."""

    def test_R_prefix_pr_view_parses(self, conn):
        """gh -R owner/repo pr view --json → still parses correctly."""
        parse_view(conn, ["-R", "other/repo", "pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        pr = conn.execute("SELECT pr_number FROM pull_requests").fetchone()
        assert pr[0] == 42

    def test_RR_double_prefix(self, conn):
        """gh -R a/b -R c/d pr view → still parses (both stripped)."""
        parse_view(conn, ["-R", "a/b", "-R", "c/d", "pr", "view", "42", "--json", "..."],
                   PR_VIEW_JSON, _repo_fn)
        pr = conn.execute("SELECT pr_number FROM pull_requests").fetchone()
        assert pr[0] == 42
