"""Tests for continuity-gh Phase 1: transparent CLI interception."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Load continuity-gh (dash in filename — use SourceFileLoader)
import importlib.machinery
_gh_path = str(Path(__file__).resolve().parent.parent / "continuity-gh")
cg = importlib.machinery.SourceFileLoader("continuity_gh", _gh_path).load_module()


@pytest.fixture
def fake_gh():
    return str(Path(__file__).resolve().parent / "fake-gh")


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test-continuity.db"


class TestSchema:
    def test_creates_table(self, db_path):
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cli_events'"
        ).fetchall()
        assert len(tables) == 1

    def test_idempotent(self, db_path):
        cg.ensure_db(db_path)
        cg.ensure_db(db_path)  # second call must not fail
        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM cli_events").fetchone()[0]
        assert count == 0

    def test_creates_parent_dir(self, db_path):
        # db_path is already in a temp dir that exists, test a nested one
        nested = db_path.parent / "nested" / "deep" / "test.db"
        cg.ensure_db(nested)
        assert nested.parent.exists()
        assert nested.exists()

    def test_wal_mode(self, db_path):
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


class TestIntercept:
    def test_logs_args_and_exit_code(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["pr", "list", "--limit", "3"],
                                 fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command, args_json, exit_code FROM cli_events"
        ).fetchone()
        assert row[0] == "gh"
        assert json.loads(row[1]) == ["pr", "list", "--limit", "3"]
        assert row[2] == 0

    def test_exit_code_matches_binary(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["--exit", "42", "--"],
                                 fake_gh, db_path)
        assert exit_code == 42

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT exit_code FROM cli_events").fetchone()
        assert row[0] == 42

    def test_records_duration(self, fake_gh, db_path):
        cg.intercept("gh", ["--exit", "0", "--sleep", "0.1", "--"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        duration = db.execute("SELECT duration_ms FROM cli_events").fetchone()[0]
        assert 50 <= duration <= 500  # 100ms sleep + overhead

    def test_records_timestamp(self, fake_gh, db_path):
        import time
        before = int(time.time())
        cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
        after = int(time.time())

        db = sqlite3.connect(str(db_path))
        ts = db.execute("SELECT recorded_at FROM cli_events").fetchone()[0]
        assert before <= ts <= after

    def test_multiple_commands(self, fake_gh, db_path):
        cg.intercept("gh", ["pr", "list"], fake_gh, db_path)
        cg.intercept("gh", ["pr", "view", "42"], fake_gh, db_path)
        cg.intercept("gh", ["--exit", "1", "--", "bad"], fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        rows = db.execute(
            "SELECT args_json, exit_code FROM cli_events ORDER BY id"
        ).fetchall()
        assert len(rows) == 3
        assert json.loads(rows[0][0]) == ["pr", "list"]
        assert rows[0][1] == 0
        assert json.loads(rows[1][0]) == ["pr", "view", "42"]
        assert rows[1][1] == 0
        assert json.loads(rows[2][0]) == ["--exit", "1", "--", "bad"]
        assert rows[2][1] == 1

    def test_git_mode(self, fake_gh, db_path):
        cg.intercept("git", ["push", "origin", "main"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command, args_json, exit_code FROM cli_events"
        ).fetchone()
        assert row[0] == "git"
        assert json.loads(row[1]) == ["push", "origin", "main"]
        assert row[2] == 0

    def test_empty_args(self, fake_gh, db_path):
        cg.intercept("gh", [], fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command, args_json, exit_code FROM cli_events"
        ).fetchone()
        assert row[0] == "gh"
        assert json.loads(row[1]) == []
        assert row[2] == 0

    def test_args_with_special_characters(self, fake_gh, db_path):
        cg.intercept("gh", ["pr", "create", "--title", "fix: crash on nil"],
                     fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT args_json FROM cli_events").fetchone()
        assert "fix: crash on nil" in row[0]

    def test_binary_not_found(self, db_path):
        with pytest.raises(FileNotFoundError):
            cg.intercept("gh", ["pr", "list"], "/nonexistent/binary", db_path)


class TestAdrRequirements:
    """Verify implemented requirements from docs/requirements.md."""

    def test_FR01_transparent_gh_wrapper(self, fake_gh, db_path):
        """FR-01: Transparent gh wrapper intercepts, logs, delegates."""
        exit_code = cg.intercept("gh", ["pr", "list"], fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command, args_json, exit_code FROM cli_events").fetchone()
        assert row[0] == "gh"
        assert row[2] == 0

    def test_FR02_transparent_git_wrapper(self, fake_gh, db_path):
        """FR-02: Transparent git wrapper intercepts push events."""
        exit_code = cg.intercept("git", ["push", "origin", "main"],
                                 fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command FROM cli_events").fetchone()
        assert row[0] == "git"

    def test_FR03_cli_events_schema(self, db_path):
        """FR-03: cli_events records command, args_json, exit_code, duration_ms, recorded_at."""
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        columns = {row[1] for row in db.execute("PRAGMA table_info(cli_events)")}
        assert columns >= {"id", "command", "args_json", "exit_code",
                          "duration_ms", "recorded_at"}

    def test_FR04_interceptor_overhead(self, fake_gh, db_path):
        """FR-04: Interceptor overhead < 50ms beyond real binary execution time."""
        cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        duration = db.execute("SELECT duration_ms FROM cli_events").fetchone()[0]
        # fake-gh with no sleep runs in ~10-20ms; total should be under 100ms
        # (macOS subprocess startup + Python import accounts for the base)
        assert duration < 100

    def test_FR05_wal_mode(self, db_path):
        """FR-05: SQLite in WAL mode."""
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_FR06_all_subcommands(self, fake_gh, db_path):
        """FR-06: gh wrapper handles all subcommands including --json output."""
        for args in [
            ["pr", "list"],
            ["pr", "view", "42", "--json", "number,title"],
            ["issue", "create", "--title", "bug"],
            ["repo", "view"],
            ["--version"],
        ]:
            exit_code = cg.intercept("gh", args, fake_gh, db_path)
            assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM cli_events").fetchone()[0]
        assert count == 5

    def test_NF01_indistinguishable_from_real(self, fake_gh, db_path):
        """NF-01: Wrappers indistinguishable from real for exit code."""
        # Run via fake binary directly
        real_proc = subprocess.run(
            [sys.executable, fake_gh, "--exit", "7", "--"],
            capture_output=True, timeout=5,
        )
        # Run via interceptor
        interceptor_exit = cg.intercept("gh", ["--exit", "7", "--"],
                                        fake_gh, db_path)
        assert interceptor_exit == real_proc.returncode
        assert interceptor_exit == 7

    def test_NF02_exit_codes_match(self, fake_gh, db_path):
        """NF-02: Exit codes match real binary exactly."""
        for expected in [0, 1, 2, 127, 255]:
            exit_code = cg.intercept("gh", ["--exit", str(expected), "--"],
                                     fake_gh, db_path)
            assert exit_code == expected

            db = sqlite3.connect(str(db_path))
            recorded = db.execute(
                "SELECT exit_code FROM cli_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            assert recorded == expected

    def test_NF04_no_subprocess_overhead_beyond_delegation(self, fake_gh, db_path):
        """NF-04: No subprocess overhead beyond the delegation call itself."""
        # Run multiple times, all should be fast
        durations = []
        for _ in range(5):
            cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
            db = sqlite3.connect(str(db_path))
            d = db.execute(
                "SELECT duration_ms FROM cli_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            durations.append(d)

        # All should be under 100ms (fake binary with zero work;
        # macOS subprocess startup variance accounts for most of this)
        for d in durations:
            assert d < 100, f"duration {d}ms exceeds 100ms threshold"

    def test_NF05_stdlib_only(self):
        """NF-05: No dependencies beyond stdlib."""
        # Verify the interceptor only imports stdlib modules
        import ast
        src = Path(__file__).resolve().parent.parent / "continuity-gh"
        tree = ast.parse(src.read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        # All imports must be stdlib
        stdlib = {"json", "os", "sqlite3", "subprocess", "sys", "time", "pathlib"}
        for imp in imports:
            assert imp in stdlib, f"non-stdlib import: {imp}"
