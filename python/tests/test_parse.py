"""Tests for continuity-gh Phase 2: structured CI event parsing.

All tests use fake-gh — no real GitHub access needed.
Parse failures must never affect exit code or output.
"""

import importlib.machinery
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Load continuity-gh
_gh_path = str(Path(__file__).resolve().parent.parent / "continuity-gh")
cg = importlib.machinery.SourceFileLoader("continuity_gh", _gh_path).load_module()


@pytest.fixture
def fake_gh():
    return str(Path(__file__).resolve().parent / "fake-gh")


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test-continuity.db"


@pytest.fixture
def pr_view_json():
    """Fixture: real-looking gh pr view --json output."""
    return (Path(__file__).resolve().parent / "fixtures" / "pr_view.json").read_text()


@pytest.fixture
def pr_checks_json():
    """Fixture: real-looking gh pr checks --json output."""
    return (Path(__file__).resolve().parent / "fixtures" / "pr_checks.json").read_text()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Structured Parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestParsePrCreate:
    """FR-09: gh pr create → extract PR number, branch, repo."""

    def test_parses_pr_create_output(self, fake_gh, db_path):
        stdout = "https://github.com/randlee/continuity/pull/42\n"
        exit_code = cg.intercept("gh", ["pr", "create", "--head", "feat/x",
                                        "--stdout", stdout, "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        pr = db.execute(
            "SELECT owner_repo, pr_number, branch, state FROM pull_requests"
        ).fetchone()
        assert pr[0] == "randlee/continuity"
        assert pr[1] == 42
        assert pr[2] == "feat/x"
        assert pr[3] == "OPEN"

    def test_auto_registers_repo(self, fake_gh, db_path):
        """FR-13: Unknown repos auto-registered on first encounter."""
        stdout = "https://github.com/neworg/newrepo/pull/1\n"
        cg.intercept("gh", ["pr", "create", "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        repo = db.execute(
            "SELECT owner_repo, gh_account FROM repos"
        ).fetchone()
        assert repo[0] == "neworg/newrepo"
        assert repo[1] == "neworg"  # gh_account is owner, not owner/repo

    def test_parse_failure_does_not_affect_exit_code(self, fake_gh, db_path):
        """Parse failures must never affect exit code (NF-07)."""
        exit_code = cg.intercept("gh", ["pr", "create", "--stdout", "garbage",
                                        "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0  # exit code from real binary, not parser


class TestParsePrMerge:
    """FR-10: gh pr merge → mark PR as MERGED."""

    def test_marks_pr_merged(self, fake_gh, db_path):
        # First create the PR via parsing
        stdout = "https://github.com/randlee/continuity/pull/42\n"
        cg.intercept("gh", ["pr", "create", "--head", "feat/x",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)

        # Then merge it
        cg.intercept("gh", ["pr", "merge", "42", "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        state = db.execute(
            "SELECT state FROM pull_requests WHERE pr_number = 42"
        ).fetchone()[0]
        assert state == "MERGED"


class TestParsePrView:
    """FR-11/FR-15/FR-16: gh pr view --json → PR metadata + CI status."""

    def test_parses_pr_metadata(self, fake_gh, db_path, pr_view_json):
        cg.intercept("gh", ["pr", "view", "42", "--json", "number,headRefName,mergeable,state,statusCheckRollup",
                            "--file", "pr_view.json", "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        pr = db.execute(
            "SELECT owner_repo, pr_number, branch, head_sha, mergeable, state FROM pull_requests"
        ).fetchone()
        assert pr[1] == 42
        assert pr[2] == "feat/new-thing"
        assert pr[3] == "abc123def456"
        assert pr[4] == "MERGEABLE"
        assert pr[5] == "OPEN"

    def test_parses_ci_jobs_from_check_rollup(self, fake_gh, db_path, pr_view_json):
        """FR-11: statusCheckRollup → ci_events with correct statuses."""
        cg.intercept("gh", ["pr", "view", "42", "--json", "...",
                            "--file", "pr_view.json", "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        jobs = db.execute(
            "SELECT job_name, status, conclusion FROM ci_events ORDER BY job_name"
        ).fetchall()
        assert len(jobs) == 4
        assert ("build", "COMPLETED", "SUCCESS") in jobs
        assert ("lint", "COMPLETED", "SUCCESS") in jobs
        assert ("test", "IN_PROGRESS", None) in jobs
        assert ("coverage", "QUEUED", None) in jobs

    def test_status_mapping(self, fake_gh, db_path):
        """Statuses are mapped to canonical QUEUED/IN_PROGRESS/COMPLETED."""
        stdout = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                             "statusCheckRollup": [
                                 {"name": "a", "status": "PENDING", "conclusion": None},
                                 {"name": "b", "status": "REQUESTED", "conclusion": None},
                                 {"name": "c", "status": "WAITING", "conclusion": None},
                             ]})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        statuses = db.execute(
            "SELECT job_name, status FROM ci_events ORDER BY job_name"
        ).fetchall()
        for _, status in statuses:
            assert status == "QUEUED"  # all should map to QUEUED


class TestParsePrChecks:
    """FR-12: gh pr checks --json → CI job statuses."""

    def test_parses_checks_output(self, fake_gh, db_path, pr_checks_json):
        cg.intercept("gh", ["pr", "checks", "42", "--json",
                            "--file", "pr_checks.json", "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        jobs = db.execute(
            "SELECT job_name, status, conclusion FROM ci_events ORDER BY job_name"
        ).fetchall()
        assert len(jobs) == 2
        assert ("build", "IN_PROGRESS", None) in jobs
        assert ("lint", "QUEUED", None) in jobs


class TestStateDiffing:
    """FR-14: State diffing — only write ci_events when status/conclusion changes."""

    def test_no_duplicate_on_identical_poll(self, fake_gh, db_path, pr_view_json):
        # First poll
        cg.intercept("gh", ["pr", "view", "42", "--json", "...",
                            "--file", "pr_view.json", "--exit", "0", "--"],
                     fake_gh, db_path)
        # Identical second poll
        cg.intercept("gh", ["pr", "view", "42", "--json", "...",
                            "--file", "pr_view.json", "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        counts = db.execute(
            "SELECT job_name, COUNT(*) FROM ci_events GROUP BY job_name"
        ).fetchall()
        for _, count in counts:
            assert count == 1, f"duplicate events for job with count {count}"

    def test_records_change(self, fake_gh, db_path):
        """When status changes, a new event is recorded."""
        # First: QUEUED
        stdout1 = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                              "statusCheckRollup": [
                                  {"name": "test", "status": "QUEUED", "conclusion": None},
                              ]})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout1, "--exit", "0", "--"],
                     fake_gh, db_path)

        # Second: IN_PROGRESS (changed!)
        stdout2 = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                              "statusCheckRollup": [
                                  {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
                              ]})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout2, "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        events = db.execute(
            "SELECT status FROM ci_events WHERE job_name='test' ORDER BY recorded_at"
        ).fetchall()
        assert len(events) == 2
        assert events[0][0] == "QUEUED"
        assert events[1][0] == "IN_PROGRESS"


class TestPrUpsert:
    """pull_requests uses INSERT OR REPLACE — latest state wins."""

    def test_upsert_updates_pr_state(self, fake_gh, db_path):
        # Create as OPEN
        stdout1 = json.dumps({"number": 1, "headRefName": "x"})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout1, "--exit", "0", "--"],
                     fake_gh, db_path)

        # Now it's MERGED
        stdout2 = json.dumps({"number": 1, "headRefName": "x", "state": "MERGED"})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout2, "--exit", "0", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
        assert count == 1  # one row, upserted
        state = db.execute("SELECT state FROM pull_requests").fetchone()[0]
        assert state == "MERGED"


class TestParseSafety:
    """Parse failures are always silent — never affect exit code or output."""

    def test_invalid_json_does_not_crash(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                                        "--stdout", "not json{{{", "--exit", "3", "--"],
                                 fake_gh, db_path)
        assert exit_code == 3  # real binary's exit code preserved

    def test_parse_exception_does_not_affect_exit(self, fake_gh, db_path):
        """Any exception in parser is caught — NF-07."""
        for args in [
            ["pr", "merge"],  # no PR number
            ["pr", "view", "--json", "..."],  # no PR number
            ["unknown", "command"],
        ]:
            exit_code = cg.intercept("gh", args + ["--exit", "0", "--"],
                                     fake_gh, db_path)
            assert exit_code == 0

    def test_nonzero_exit_skips_parsing(self, fake_gh, db_path):
        """FR-07/NF-07: Non-zero exit codes skip parsing entirely."""
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--file", "pr_view.json", "--exit", "1", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
        assert count == 0  # nothing parsed on failure


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 regression — ensure Phase 2 didn't break anything
# ═══════════════════════════════════════════════════════════════════════════

class TestPhase1Regression:
    def test_basic_intercept_still_works(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["--version", "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command, exit_code FROM cli_events").fetchone()
        assert row[0] == "gh"
        assert row[1] == 0

    def test_git_mode_still_works(self, fake_gh, db_path):
        exit_code = cg.intercept("git", ["push", "origin", "main",
                                         "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0

    def test_exit_code_preserved(self, fake_gh, db_path):
        for code in [0, 1, 127]:
            exit_code = cg.intercept("gh", ["--exit", str(code), "--"],
                                     fake_gh, db_path)
            assert exit_code == code


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements verification — Phase 2
# ═══════════════════════════════════════════════════════════════════════════

class TestAdrPhase2:
    """Verify implemented Phase 2 requirements from docs/requirements.md."""

    def test_FR09_pr_create_parsing(self, fake_gh, db_path):
        """FR-09: gh pr create output parsed → PR number, branch, owner/repo."""
        stdout = "https://github.com/randlee/atm-core/pull/99\n"
        cg.intercept("gh", ["pr", "create", "--head", "fix/bug",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        pr = db.execute("SELECT owner_repo, pr_number, branch FROM pull_requests").fetchone()
        assert pr == ("randlee/atm-core", 99, "fix/bug")

    def test_FR10_pr_merge_parsing(self, fake_gh, db_path):
        """FR-10: gh pr merge parsed → mark PR as MERGED."""
        stdout = "https://github.com/randlee/x/pull/1\n"
        cg.intercept("gh", ["pr", "create", "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        cg.intercept("gh", ["pr", "merge", "1", "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        state = db.execute("SELECT state FROM pull_requests WHERE pr_number=1").fetchone()[0]
        assert state == "MERGED"

    def test_FR11_pr_view_json_parsing(self, fake_gh, db_path, pr_view_json):
        """FR-11: gh pr view --json statusCheckRollup parsed."""
        cg.intercept("gh", ["pr", "view", "42", "--json", "...",
                            "--file", "pr_view.json", "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0]
        assert count == 4

    def test_FR13_auto_register_repos(self, fake_gh, db_path):
        """FR-13: Unknown repos auto-registered on first encounter."""
        stdout = "https://github.com/neworg/newrepo/pull/1\n"
        cg.intercept("gh", ["pr", "create", "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        repo = db.execute("SELECT owner_repo FROM repos").fetchone()
        assert repo[0] == "neworg/newrepo"

    def test_FR14_state_diffing(self, fake_gh, db_path):
        """FR-14: Only write ci_events when status/conclusion differs."""
        stdout = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                             "statusCheckRollup": [
                                 {"name": "test", "status": "QUEUED", "conclusion": None},
                             ]})
        # Two identical calls
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0]
        assert count == 1

    def test_FR17_ci_events_append_only(self, fake_gh, db_path):
        """FR-17: ci_events is append-only — rows only increase."""
        stdout1 = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                              "statusCheckRollup": [
                                  {"name": "a", "status": "QUEUED", "conclusion": None},
                              ]})
        stdout2 = json.dumps({"number": 1, "headRefName": "x", "mergeable": "UNKNOWN",
                              "statusCheckRollup": [
                                  {"name": "a", "status": "IN_PROGRESS", "conclusion": None},
                              ]})
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout1, "--exit", "0", "--"],
                     fake_gh, db_path)
        cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                            "--stdout", stdout2, "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0]
        assert count == 2

    def test_NF06_parsing_after_delegation(self, fake_gh, db_path):
        """NF-06: Parsing runs after delegation, not before."""
        # The exit code comes from the binary, not the parser
        exit_code = cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                                        "--stdout", "bad json", "--exit", "5", "--"],
                                 fake_gh, db_path)
        assert exit_code == 5

    def test_NF07_parse_failures_never_affect_exit(self, fake_gh, db_path):
        """NF-07: Parse failures must never affect exit code or output."""
        for exit_val in [0, 1, 42]:
            exit_code = cg.intercept("gh", ["pr", "view", "1", "--json", "...",
                                            "--stdout", "{{{bad", "--exit", str(exit_val), "--"],
                                     fake_gh, db_path)
            assert exit_code == exit_val
