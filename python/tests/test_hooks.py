"""Tests for hooks.py — post-push hook installer."""

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks import install, uninstall, is_installed, HOOK_SCRIPT


@pytest.fixture
def repo():
    """Temp git repo with hooks dir."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / ".git" / "hooks").mkdir(parents=True)
        yield repo


class TestInstall:
    def test_creates_hook(self, repo):
        path = install(repo)
        assert path.exists()
        assert path.name == "post-push"

    def test_hook_is_executable(self, repo):
        path = install(repo)
        assert path.stat().st_mode & stat.S_IEXEC

    def test_hook_contains_continuity(self, repo):
        path = install(repo)
        content = path.read_text()
        assert "continuity" in content
        assert "SIGUSR1" in content
        assert "origin" in content

    def test_idempotent(self, repo):
        install(repo)
        install(repo)  # should not raise

    def test_creates_hooks_dir(self, repo):
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.rmdir()
        assert not hooks_dir.exists()
        install(repo)
        assert hooks_dir.exists()


class TestUninstall:
    def test_removes_hook(self, repo):
        install(repo)
        assert uninstall(repo)
        assert not (repo / ".git" / "hooks" / "post-push").exists()

    def test_returns_false_if_not_installed(self, repo):
        assert not uninstall(repo)

    def test_only_removes_continuity_hook(self, repo):
        """Don't remove non-continuity post-push hooks."""
        hook = repo / ".git" / "hooks" / "post-push"
        hook.write_text("#!/bin/sh\necho custom")
        assert not uninstall(repo)
        assert hook.exists()


class TestIsInstalled:
    def test_returns_true_after_install(self, repo):
        assert not is_installed(repo)
        install(repo)
        assert is_installed(repo)

    def test_returns_false_after_uninstall(self, repo):
        install(repo)
        uninstall(repo)
        assert not is_installed(repo)

    def test_returns_false_for_nonexistent(self, repo):
        assert not is_installed(repo)


class TestHookScript:
    def test_exits_on_non_origin_remote(self):
        """Hook should exit if remote is not origin."""
        assert 'REMOTE=$1' in HOOK_SCRIPT
        assert '[ "$REMOTE" = "origin" ]' in HOOK_SCRIPT

    def test_uses_continuity_home(self):
        """Hook should use CONTINUITY_HOME or default."""
        assert "CONTINUITY_HOME" in HOOK_SCRIPT
        assert "daemon.pid" in HOOK_SCRIPT

    def test_sends_sigusr1(self):
        """Hook should send SIGUSR1 to daemon."""
        assert "SIGUSR1" in HOOK_SCRIPT


class TestAdr:
    def test_FR34_installed_by_register(self, repo):
        """FR-34: Hook installed by continuity register."""
        # This is what continuity register would call
        path = install(repo)
        assert path.exists()
        assert is_installed(repo)

    def test_FR33_sends_sigusr1(self, repo):
        """FR-33: Hook sends SIGUSR1 on push to origin."""
        path = install(repo)
        content = path.read_text()
        assert "SIGUSR1" in content
        assert "origin" in content