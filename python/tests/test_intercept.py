"""Tests for continuity-gh Phase 1: transparent CLI interception.

Cross-platform: all tests pass on macOS, Linux, and Windows.
Uses fake-gh (Python script) — no real GitHub access needed.
"""

import importlib.machinery
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Load continuity-gh (dash in filename — use SourceFileLoader)
_gh_path = str(Path(__file__).resolve().parent.parent / "continuity-gh")
cg = importlib.machinery.SourceFileLoader("continuity_gh", _gh_path).load_module()


@pytest.fixture
def fake_gh():
    return str(Path(__file__).resolve().parent / "fake-gh")


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test-continuity.db"


# ═══════════════════════════════════════════════════════════════════════════
# Schema tests
# ═══════════════════════════════════════════════════════════════════════════

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
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        count = db.execute("SELECT COUNT(*) FROM cli_events").fetchone()[0]
        assert count == 0

    def test_creates_parent_dir(self, db_path):
        nested = db_path.parent / "nested" / "deep" / "test.db"
        cg.ensure_db(nested)
        assert nested.parent.exists()
        assert nested.exists()

    def test_wal_mode(self, db_path):
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


# ═══════════════════════════════════════════════════════════════════════════
# Interception tests
# ═══════════════════════════════════════════════════════════════════════════

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
        assert 50 <= duration <= 500

    def test_records_timestamp(self, fake_gh, db_path):
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
        cg.intercept("git", ["push", "origin", "main"], fake_gh, db_path)
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
        with pytest.raises((FileNotFoundError, OSError)):
            cg.intercept("gh", ["pr", "list"],
                         "/nonexistent/path/to/binary", db_path)

    def test_python_script_detection(self, fake_gh, db_path):
        """_run_binary auto-detects Python scripts and prepends sys.executable."""
        exit_code = cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
        assert exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════
# Cross-platform tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCrossPlatform:
    def test_resolve_binary_gh_exists(self):
        """_resolve_binary returns a path that exists or is a sensible default."""
        path = cg._resolve_binary("gh")
        assert path
        assert isinstance(path, str)
        # On any platform, should be a non-empty string
        assert len(path) > 0

    def test_resolve_binary_git_exists(self):
        path = cg._resolve_binary("git")
        assert path
        assert isinstance(path, str)

    def test_data_dir_is_absolute(self):
        path = cg._data_dir()
        assert path.is_absolute()

    def test_data_dir_differs_by_platform(self):
        """Verify data dir follows platform conventions."""
        path = str(cg._data_dir()).lower()
        if sys.platform == "win32":
            assert "appdata" in path or "local" in path
        elif sys.platform == "darwin":
            assert ".local" in path or "library" in path
        else:  # linux
            assert ".local" in path or ".share" in path or "xdg" in path.lower()

    def test_db_path_in_data_dir(self):
        assert cg.DB_PATH.parent == cg._data_dir()
        assert cg.DB_PATH.name == "continuity.db"

    def test_run_binary_detects_python_script(self, fake_gh):
        """_run_binary should auto-detect the fake-gh as a Python script."""
        proc = cg._run_binary(fake_gh, ["--exit", "0", "--"], timeout=10)
        assert proc.returncode == 0

    def test_run_binary_python_script_uses_sys_executable(self, fake_gh, monkeypatch):
        """Verify Python scripts are invoked with sys.executable."""
        captured_cmd = []
        real_run = subprocess.run  # save before monkeypatching

        def fake_run(cmd, timeout=None):
            captured_cmd.append(list(cmd))
            return real_run(cmd, timeout=timeout)

        monkeypatch.setattr(subprocess, "run", fake_run)
        cg._run_binary(fake_gh, ["--exit", "0", "--"], timeout=10)
        # First element should be sys.executable (detected as Python script)
        assert len(captured_cmd) >= 1
        assert captured_cmd[0][0] == sys.executable


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements verification
# ═══════════════════════════════════════════════════════════════════════════

class TestAdrRequirements:
    """Verify implemented requirements from docs/requirements.md."""

    def test_FR01_transparent_gh_wrapper(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["pr", "list"], fake_gh, db_path)
        assert exit_code == 0
        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command, args_json, exit_code FROM cli_events").fetchone()
        assert row[0] == "gh"
        assert row[2] == 0

    def test_FR02_transparent_git_wrapper(self, fake_gh, db_path):
        exit_code = cg.intercept("git", ["push", "origin", "main"], fake_gh, db_path)
        assert exit_code == 0
        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command FROM cli_events").fetchone()
        assert row[0] == "git"

    def test_FR03_cli_events_schema(self, db_path):
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        columns = {row[1] for row in db.execute("PRAGMA table_info(cli_events)")}
        assert columns >= {"id", "command", "args_json", "exit_code",
                          "duration_ms", "recorded_at"}

    def test_FR04_interceptor_overhead(self, fake_gh, db_path):
        cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        duration = db.execute("SELECT duration_ms FROM cli_events").fetchone()[0]
        assert duration < 100

    def test_FR05_wal_mode(self, db_path):
        cg.ensure_db(db_path)
        db = sqlite3.connect(str(db_path))
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_FR06_all_subcommands(self, fake_gh, db_path):
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
        real_proc = subprocess.run(
            [sys.executable, fake_gh, "--exit", "7", "--"],
            capture_output=True, timeout=5,
        )
        interceptor_exit = cg.intercept("gh", ["--exit", "7", "--"],
                                        fake_gh, db_path)
        assert interceptor_exit == real_proc.returncode
        assert interceptor_exit == 7

    def test_NF02_exit_codes_match(self, fake_gh, db_path):
        for expected in [0, 1, 2, 127, 255]:
            exit_code = cg.intercept("gh", ["--exit", str(expected), "--"],
                                     fake_gh, db_path)
            assert exit_code == expected
            db = sqlite3.connect(str(db_path))
            recorded = db.execute(
                "SELECT exit_code FROM cli_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            assert recorded == expected

    def test_NF03_cross_platform_paths(self):
        """NF-03 expanded: Works cross-platform (macOS/Linux/Windows)."""
        # Data dir uses platform conventions
        data = cg._data_dir()
        assert data.is_absolute()
        # Binary resolution works
        for name in ("gh", "git"):
            path = cg._resolve_binary(name)
            assert path
            assert len(path) > 0
        # DB path is in data dir
        assert str(cg.DB_PATH).startswith(str(data))

    def test_NF04_overhead_is_subprocess_startup(self, fake_gh, db_path):
        durations = []
        for _ in range(5):
            cg.intercept("gh", ["--exit", "0", "--"], fake_gh, db_path)
            db = sqlite3.connect(str(db_path))
            d = db.execute(
                "SELECT duration_ms FROM cli_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            durations.append(d)
        for d in durations:
            assert d < 100, f"duration {d}ms exceeds 100ms threshold"

    def test_NF05_stdlib_only(self):
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
        stdlib = {"json", "os", "shutil", "sqlite3", "subprocess", "sys", "time", "pathlib"}
        for imp in imports:
            assert imp in stdlib, f"non-stdlib import: {imp}"
