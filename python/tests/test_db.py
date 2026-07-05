"""Tests for db.py — schema and connection management."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        c = db.ensure_db(db_path)
        yield c
        c.close()


class TestSchema:
    def test_creates_tables(self, conn):
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [t[0] for t in tables]
        assert "cli_events" in names
        assert "repos" in names
        assert "pull_requests" in names
        assert "ci_events" in names

    def test_idempotent(self, conn):
        # ensure_db already called by fixture; calling again is idempotent
        db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        db.ensure_db(db_path)  # no error

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "nested" / "deep" / "test.db"
            conn = db.ensure_db(nested)
            assert nested.exists()
            conn.close()

    def test_wal_mode(self, conn):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_cli_events_has_blocked_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cli_events)")}
        assert "blocked" in cols

    def test_repos_has_provider_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "provider" in cols
        conn.execute(
            "INSERT INTO repos (owner_repo, gh_account) VALUES ('test/repo', 'test')"
        )
        provider = conn.execute("SELECT provider FROM repos").fetchone()[0]
        assert provider == "github"

    def test_ci_events_has_index(self, conn):
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_ci_lookup'"
        ).fetchone()
        assert idx is not None


class TestAdr:
    def test_FR03_schema(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cli_events)")}
        assert cols >= {"id", "command", "args_json", "exit_code",
                        "duration_ms", "recorded_at"}

    def test_FR05_wal_mode(self, conn):
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
