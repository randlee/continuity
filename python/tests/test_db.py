"""Tests for db.py — schema and connection management."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.db"


class TestSchema:
    def test_creates_tables(self, db_path):
        conn = db.ensure_db(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [t[0] for t in tables]
        assert "cli_events" in names
        assert "repos" in names
        assert "pull_requests" in names
        assert "ci_events" in names

    def test_idempotent(self, db_path):
        db.ensure_db(db_path)
        db.ensure_db(db_path)  # no error

    def test_creates_parent_dir(self, db_path):
        nested = db_path.parent / "nested" / "deep" / "test.db"
        db.ensure_db(nested)
        assert nested.exists()

    def test_wal_mode(self, db_path):
        conn = db.ensure_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_cli_events_has_blocked_column(self, db_path):
        conn = db.ensure_db(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cli_events)")}
        assert "blocked" in cols

    def test_repos_has_provider_column(self, db_path):
        conn = db.ensure_db(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
        assert "provider" in cols
        # Default value is 'github'
        conn.execute(
            "INSERT INTO repos (owner_repo, gh_account) VALUES ('test/repo', 'test')"
        )
        provider = conn.execute("SELECT provider FROM repos").fetchone()[0]
        assert provider == "github"

    def test_ci_events_has_index(self, db_path):
        conn = db.ensure_db(db_path)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_ci_lookup'"
        ).fetchone()
        assert idx is not None


class TestAdr:
    def test_FR03_schema(self, db_path):
        conn = db.ensure_db(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cli_events)")}
        assert cols >= {"id", "command", "args_json", "exit_code",
                        "duration_ms", "recorded_at"}

    def test_FR05_wal_mode(self, db_path):
        conn = db.ensure_db(db_path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
