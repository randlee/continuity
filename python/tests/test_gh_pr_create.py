"""Tests for gh/pr_create.py."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from gh.pr_create import parse


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = db.ensure_db(db_path)
        yield c
        c.close()


def _ensure_repo(conn, owner_repo):
    conn.execute(
        "INSERT OR IGNORE INTO repos (owner_repo, gh_account) VALUES (?, ?)",
        (owner_repo, owner_repo.split("/")[0]),
    )


class TestPrCreate:
    def test_parses_output_url(self, conn):
        stdout = "https://github.com/randlee/continuity/pull/42\n"
        parse(conn, ["pr", "create", "--head", "feat/x"], stdout,
              lambda r: _ensure_repo(conn, r))
        pr = conn.execute(
            "SELECT owner_repo, pr_number, branch, state FROM pull_requests"
        ).fetchone()
        assert pr == ("randlee/continuity", 42, "feat/x", "OPEN")

    def test_auto_registers_repo(self, conn):
        stdout = "https://github.com/neworg/newrepo/pull/1\n"
        parse(conn, ["pr", "create"], stdout, lambda r: _ensure_repo(conn, r))
        repo = conn.execute("SELECT owner_repo FROM repos").fetchone()
        assert repo[0] == "neworg/newrepo"

    def test_no_match_does_nothing(self, conn):
        parse(conn, ["pr", "create"], "garbage output",
              lambda r: _ensure_repo(conn, r))
        count = conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
        assert count == 0

    def test_FR09(self, conn):
        """FR-09: gh pr create → PR number, branch, owner/repo."""
        stdout = "https://github.com/randlee/atm-core/pull/99\n"
        parse(conn, ["pr", "create", "--head", "fix/bug"], stdout,
              lambda r: _ensure_repo(conn, r))
        pr = conn.execute(
            "SELECT owner_repo, pr_number, branch FROM pull_requests"
        ).fetchone()
        assert pr == ("randlee/atm-core", 99, "fix/bug")

    def test_FR13(self, conn):
        """FR-13: Unknown repos auto-registered."""
        stdout = "https://github.com/neworg/newrepo/pull/1\n"
        parse(conn, ["pr", "create"], stdout, lambda r: _ensure_repo(conn, r))
        assert conn.execute("SELECT owner_repo FROM repos").fetchone()[0] == "neworg/newrepo"
