"""Tests for hooks.py — non-destructive post-push hook installer.

Tests: install (fresh/append/idempotent), uninstall, is_installed.
"""

import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hooks import install, uninstall, is_installed


@pytest.fixture
def repo():
    """Temp git repo with hooks dir."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / ".git" / "hooks").mkdir(parents=True)
        yield repo


# ═══════════════════════════════════════════════════════════════════════════
# Install — fresh repo
# ═══════════════════════════════════════════════════════════════════════════

class TestInstall:
    def test_creates_hook_on_fresh_repo(self, repo):
        path = install(repo)
        assert path is not None
        assert path.exists()
        assert path.name == "post-push"

    def test_hook_is_executable(self, repo):
        path = install(repo)
        if sys.platform == "win32":
            assert path.exists()
        else:
            assert path.stat().st_mode & stat.S_IEXEC

    def test_hook_contains_curl_and_poll(self, repo):
        path = install(repo)
        content = path.read_text()
        assert "curl" in content
        assert "/poll" in content
        assert "origin" in content

    def test_hook_has_sentinel_markers(self, repo):
        path = install(repo)
        content = path.read_text()
        assert "=== continuity start" in content
        assert "=== continuity end" in content

    def test_creates_hooks_dir(self, repo):
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.rmdir()
        assert not hooks_dir.exists()
        install(repo)
        assert hooks_dir.exists()

    # ── Append to existing hook ──────────────────────────────────────

    def test_appends_to_existing_hook(self, repo):
        """Preserves user's existing hook content."""
        hook_path = repo / ".git" / "hooks" / "post-push"
        hook_path.write_text("#!/bin/sh\necho 'custom hook'\n")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

        result = install(repo)
        assert result is not None
        content = hook_path.read_text()
        assert "custom hook" in content
        assert "=== continuity start" in content
        assert "curl" in content

    def test_does_not_duplicate_on_reinstall(self, repo):
        """Second install returns None — already installed."""
        install(repo)
        result = install(repo)
        assert result is None

    def test_does_not_duplicate_on_existing_with_block(self, repo):
        """Appending to a hook that already has continuity block."""
        hook_path = repo / ".git" / "hooks" / "post-push"
        hook_path.write_text("#!/bin/sh\necho custom\n\n# === continuity start (managed, do not edit) ===\nif [ \"$1\" = \"origin\" ]; then\n  PORT=$(cat \"${CONTINUITY_HOME:-$HOME/.local/share/continuity}/daemon.port\" 2>/dev/null)\n  [ -n \"$PORT\" ] && curl -s -X POST \"http://localhost:$PORT/poll\" >/dev/null 2>&1 &\nfi\n# === continuity end ===\n")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

        result = install(repo)
        assert result is None
        content = hook_path.read_text()
        # Only one continuity block
        assert content.count("=== continuity start") == 1


# ═══════════════════════════════════════════════════════════════════════════
# Uninstall
# ═══════════════════════════════════════════════════════════════════════════

class TestUninstall:
    def test_removes_block_preserves_user_content(self, repo):
        """Uninstall removes continuity block, keeps user's hook."""
        hook_path = repo / ".git" / "hooks" / "post-push"
        hook_path.write_text("#!/bin/sh\necho 'custom'\n")
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
        install(repo)

        assert uninstall(repo)
        content = hook_path.read_text()
        assert "custom" in content
        assert "=== continuity start" not in content

    def test_removes_entire_file_if_only_continuity(self, repo):
        """If hook only had continuity, remove the file entirely."""
        install(repo)
        assert uninstall(repo)
        assert not (repo / ".git" / "hooks" / "post-push").exists()

    def test_returns_false_if_not_installed(self, repo):
        assert not uninstall(repo)

    def test_returns_false_if_no_continuity_block(self, repo):
        hook_path = repo / ".git" / "hooks" / "post-push"
        hook_path.write_text("#!/bin/sh\necho custom\n")
        assert not uninstall(repo)


# ═══════════════════════════════════════════════════════════════════════════
# is_installed
# ═══════════════════════════════════════════════════════════════════════════

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

    def test_returns_false_for_hook_without_block(self, repo):
        hook_path = repo / ".git" / "hooks" / "post-push"
        hook_path.write_text("#!/bin/sh\necho custom\n")
        assert not is_installed(repo)


# ═══════════════════════════════════════════════════════════════════════════
# Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestRequirements:
    def test_FR45_installed_by_register(self, repo):
        path = install(repo)
        assert path is not None
        assert path.exists()
        assert is_installed(repo)

    def test_FR46_uses_curl_poll(self, repo):
        path = install(repo)
        content = path.read_text()
        assert "curl" in content
        assert "/poll" in content
        assert "origin" in content
