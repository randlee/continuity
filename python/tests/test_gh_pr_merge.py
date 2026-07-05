"""Tests for gh/pr_merge.py."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from gh.pr_merge import parse


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = db.ensure_db(db_path)
        # Seed a PR
        c.execute(
            "INSERT INTO pull_requests (owner_repo, pr_number, branch, state, updated_at) "
            "VALUES ('test-owner/test-repo', 42, 'feat/x', 'OPEN', 0)"
        )
        c.commit()
        yield c


def _repo_fn():
    return "test-owner/test-repo"


class TestPrMerge:
    def test_marks_merged(self, conn):
        parse(conn, ["pr", "merge", "42"], _repo_fn)
        state = conn.execute(
            "SELECT state FROM pull_requests WHERE pr_number=42"
        ).fetchone()[0]
        assert state == "MERGED"

    def test_looks_up_repo_from_db(self, conn):
        """Uses existing PR's repo, not repo_fn."""
        called = []
        parse(conn, ["pr", "merge", "42"], lambda: called.append(1) or "other/repo")
        state = conn.execute("SELECT state FROM pull_requests WHERE pr_number=42").fetchone()[0]
        assert state == "MERGED"
        assert len(called) == 0  # repo_fn not called, used DB lookup

    def test_no_pr_number_does_nothing(self, conn):
        parse(conn, ["pr", "merge"], _repo_fn)
        # PR still OPEN
        state = conn.execute("SELECT state FROM pull_requests WHERE pr_number=42").fetchone()[0]
        assert state == "OPEN"

    def test_FR10(self, conn):
        """FR-10: gh pr merge → mark PR as MERGED."""
        parse(conn, ["pr", "merge", "42"], _repo_fn)
        assert conn.execute("SELECT state FROM pull_requests WHERE pr_number=42").fetchone()[0] == "MERGED"
