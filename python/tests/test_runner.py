"""Tests for runner.py — binary resolution and subprocess delegation."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runner


@pytest.fixture
def fake_gh():
    return str(Path(__file__).resolve().parent / "fake-gh")


class TestResolveBinary:
    def test_gh_resolves(self):
        path = runner.resolve_binary("gh")
        assert path and len(path) > 0

    def test_git_resolves(self):
        path = runner.resolve_binary("git")
        assert path and len(path) > 0


class TestRunBinary:
    def test_runs_and_returns_exit_code(self, fake_gh):
        proc = runner.run_binary(fake_gh, ["--exit", "7", "--"], timeout=10)
        assert proc.returncode == 7

    def test_detects_python_script(self, fake_gh):
        """Auto-detects Python scripts and prepends sys.executable."""
        proc = runner.run_binary(fake_gh, ["--exit", "0", "--"], timeout=10)
        assert proc.returncode == 0

    def test_uses_sys_executable(self, fake_gh, monkeypatch):
        captured = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        runner.run_binary(fake_gh, ["--exit", "0", "--"], timeout=10)
        assert len(captured) >= 1
        assert captured[0][0] == sys.executable
