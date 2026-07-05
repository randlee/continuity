"""Tests for continuity-gh Phase 3: dangerous command blocking.

Blocking happens BEFORE delegation — the command is never executed.
Logged with blocked=1, exit_code=-1, message to stderr.
"""

import importlib.machinery
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure python/ is on sys.path so continuity-gh can import its submodules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
# check_dangerous() unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckDangerousGh:
    def test_blocks_pr_merge(self):
        msg = cg.check_dangerous("gh", ["pr", "merge"])
        assert msg is not None
        assert "pr merge blocked" in msg

    def test_blocks_pr_merge_with_number(self):
        msg = cg.check_dangerous("gh", ["pr", "merge", "42"])
        assert msg is not None
        assert "pr merge blocked" in msg

    def test_allows_pr_merge_auto(self):
        """--auto flag bypasses the block (auto-merge is safe)."""
        msg = cg.check_dangerous("gh", ["pr", "merge", "42", "--auto"])
        assert msg is None

    def test_allows_pr_merge_auto_first(self):
        msg = cg.check_dangerous("gh", ["pr", "merge", "--auto", "42"])
        assert msg is None

    def test_blocks_repo_delete(self):
        msg = cg.check_dangerous("gh", ["repo", "delete", "myrepo"])
        assert msg is not None
        assert "repo delete blocked" in msg

    def test_allows_repo_view(self):
        """Non-destructive repo commands pass through."""
        msg = cg.check_dangerous("gh", ["repo", "view"])
        assert msg is None

    def test_allows_repo_clone(self):
        msg = cg.check_dangerous("gh", ["repo", "clone", "owner/repo"])
        assert msg is None

    def test_blocks_api_delete(self):
        msg = cg.check_dangerous("gh", ["api", "repos/owner/repo", "--method", "DELETE"])
        assert msg is not None
        assert "destructive gh api" in msg

    def test_blocks_api_patch(self):
        msg = cg.check_dangerous("gh", ["api", "--method", "PATCH", "endpoint"])
        assert msg is not None

    def test_blocks_api_put(self):
        msg = cg.check_dangerous("gh", ["api", "-X", "PUT", "endpoint"])
        assert msg is not None

    def test_allows_api_get(self):
        msg = cg.check_dangerous("gh", ["api", "repos/owner/repo", "--method", "GET"])
        assert msg is None

    def test_allows_api_post(self):
        msg = cg.check_dangerous("gh", ["api", "--method", "POST", "endpoint"])
        assert msg is None

    def test_allows_normal_gh_commands(self):
        for args in [
            ["pr", "list"],
            ["pr", "view", "42"],
            ["pr", "create", "--title", "fix"],
            ["issue", "list"],
            ["repo", "view"],
            ["--version"],
            ["auth", "status"],
        ]:
            msg = cg.check_dangerous("gh", args)
            assert msg is None, f"should not block: gh {' '.join(args)}"


class TestCheckDangerousGit:
    def test_blocks_push_force(self):
        msg = cg.check_dangerous("git", ["push", "--force", "origin", "main"])
        assert msg is not None
        assert "force push blocked" in msg

    def test_blocks_push_f_short_flag(self):
        msg = cg.check_dangerous("git", ["push", "-f"])
        assert msg is not None

    def test_blocks_push_force_at_end(self):
        """--force anywhere in push args triggers block."""
        msg = cg.check_dangerous("git", ["push", "origin", "main", "--force"])
        assert msg is not None

    def test_allows_push_force_with_lease(self):
        """force-with-lease is the safe alternative."""
        msg = cg.check_dangerous("git", ["push", "--force-with-lease", "origin", "main"])
        assert msg is None

    def test_allows_normal_push(self):
        msg = cg.check_dangerous("git", ["push", "origin", "main"])
        assert msg is None

    def test_allows_push_no_args(self):
        msg = cg.check_dangerous("git", ["push"])
        assert msg is None

    def test_blocks_branch_force_delete(self):
        msg = cg.check_dangerous("git", ["branch", "-D", "mybranch"])
        assert msg is not None
        assert "force delete branch blocked" in msg

    def test_allows_branch_delete(self):
        """Lowercase -d is safe (only deletes merged branches)."""
        msg = cg.check_dangerous("git", ["branch", "-d", "mybranch"])
        assert msg is None

    def test_blocks_push_delete_remote(self):
        msg = cg.check_dangerous("git", ["push", "origin", "--delete", "mybranch"])
        assert msg is not None

    def test_allows_normal_git_commands(self):
        for args in [
            ["status"],
            ["log"],
            ["diff"],
            ["branch"],
            ["checkout", "main"],
            ["commit", "-m", "fix"],
            ["fetch"],
            ["pull"],
        ]:
            msg = cg.check_dangerous("git", args)
            assert msg is None, f"should not block: git {' '.join(args)}"


class TestOverride:
    def test_allow_dangerous_env_disables_all_checks(self, monkeypatch):
        monkeypatch.setenv("CONTINUITY_ALLOW_DANGEROUS", "1")
        for command, args in [
            ("gh", ["pr", "merge"]),
            ("gh", ["repo", "delete", "x"]),
            ("gh", ["api", "--method", "DELETE"]),
            ("git", ["push", "--force"]),
            ("git", ["branch", "-D", "x"]),
            ("git", ["push", "--delete", "x"]),
        ]:
            msg = cg.check_dangerous(command, args)
            assert msg is None, f"override should allow: {command} {' '.join(args)}"

    def test_override_is_exact_match(self, monkeypatch):
        """Only '1' enables override, not 'true' or 'yes'."""
        for val in ["true", "yes", "0", ""]:
            monkeypatch.setenv("CONTINUITY_ALLOW_DANGEROUS", val)
            msg = cg.check_dangerous("gh", ["pr", "merge"])
            assert msg is not None, f"'{val}' should not enable override"


class TestEmptyArgs:
    def test_empty_args_safe(self):
        assert cg.check_dangerous("gh", []) is None
        assert cg.check_dangerous("git", []) is None


# ═══════════════════════════════════════════════════════════════════════════
# intercept() integration tests — blocking happens before delegation
# ═══════════════════════════════════════════════════════════════════════════

class TestInterceptBlocking:
    def test_blocked_command_not_delegated(self, fake_gh, db_path):
        """The real binary is never called for blocked commands."""
        # Use a nonexistent binary — if it were delegated, it would FileNotFoundError
        exit_code = cg.intercept("gh", ["pr", "merge"], "/nonexistent/binary", db_path)
        assert exit_code == 1

    def test_blocked_command_logged(self, fake_gh, db_path):
        cg.intercept("gh", ["pr", "merge", "42"], fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command, args_json, exit_code, blocked FROM cli_events"
        ).fetchone()
        assert row[0] == "gh"
        assert json.loads(row[1]) == ["pr", "merge", "42"]
        assert row[2] == -1
        assert row[3] == 1

    def test_blocked_command_duration_zero(self, fake_gh, db_path):
        """Blocked commands have 0ms duration — they never execute."""
        cg.intercept("gh", ["repo", "delete", "x"], fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        duration = db.execute("SELECT duration_ms FROM cli_events").fetchone()[0]
        assert duration == 0

    def test_safe_command_still_works(self, fake_gh, db_path):
        """Blocking layer doesn't affect safe commands."""
        exit_code = cg.intercept("gh", ["--version", "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT blocked FROM cli_events").fetchone()
        assert row[0] == 0

    def test_git_push_force_blocked(self, fake_gh, db_path):
        exit_code = cg.intercept("git", ["push", "--force", "origin", "main"],
                                 fake_gh, db_path)
        assert exit_code == 1

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT command, blocked FROM cli_events").fetchone()
        assert row[0] == "git"
        assert row[1] == 1

    def test_git_branch_D_blocked(self, fake_gh, db_path):
        exit_code = cg.intercept("git", ["branch", "-D", "old-feature"],
                                 fake_gh, db_path)
        assert exit_code == 1

    def test_override_allows_blocked(self, fake_gh, db_path, monkeypatch):
        monkeypatch.setenv("CONTINUITY_ALLOW_DANGEROUS", "1")
        # With override, delegation happens (fake-gh returns 0)
        exit_code = cg.intercept("gh", ["pr", "merge", "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0  # delegated to fake-gh, which returns 0

        db = sqlite3.connect(str(db_path))
        row = db.execute("SELECT blocked, exit_code FROM cli_events").fetchone()
        assert row[0] == 0  # not flagged as blocked
        assert row[1] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1/2 regression — blocking layer doesn't break existing behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestRegression:
    def test_phase1_intercept_still_works(self, fake_gh, db_path):
        exit_code = cg.intercept("gh", ["--version", "--exit", "0", "--"],
                                 fake_gh, db_path)
        assert exit_code == 0

    def test_phase2_parse_still_works(self, fake_gh, db_path):
        stdout = "https://github.com/randlee/continuity/pull/42\n"
        cg.intercept("gh", ["pr", "create", "--head", "feat/x",
                            "--stdout", stdout, "--exit", "0", "--"],
                     fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        pr = db.execute("SELECT owner_repo, pr_number FROM pull_requests").fetchone()
        assert pr == ("randlee/continuity", 42)

    def test_safe_commands_zero_blocked(self, fake_gh, db_path):
        """All safe commands have blocked=0."""
        for args in [
            ["pr", "list", "--exit", "0", "--"],
            ["--version", "--exit", "0", "--"],
            ["repo", "view", "--exit", "0", "--"],
        ]:
            cg.intercept("gh", args, fake_gh, db_path)

        db = sqlite3.connect(str(db_path))
        blocked = db.execute(
            "SELECT COUNT(*) FROM cli_events WHERE blocked = 1"
        ).fetchone()[0]
        assert blocked == 0


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements — Phase 3
# ═══════════════════════════════════════════════════════════════════════════

class TestAdrPhase3:
    def test_FR18_pr_merge_blocked(self):
        """FR-18: Block gh pr merge (without --auto)."""
        assert cg.check_dangerous("gh", ["pr", "merge"]) is not None
        assert cg.check_dangerous("gh", ["pr", "merge", "--auto"]) is None

    def test_FR19_repo_delete_blocked(self):
        """FR-19: Block gh repo delete."""
        assert cg.check_dangerous("gh", ["repo", "delete", "x"]) is not None

    def test_FR20_destructive_api_blocked(self):
        """FR-20: Block destructive gh api (DELETE, PATCH, PUT)."""
        for method in ["DELETE", "PATCH", "PUT"]:
            assert cg.check_dangerous("gh", ["api", "--method", method]) is not None
        assert cg.check_dangerous("gh", ["api", "--method", "GET"]) is None
        assert cg.check_dangerous("gh", ["api", "--method", "POST"]) is None

    def test_FR21_force_push_blocked(self):
        """FR-21: Block git push --force / -f."""
        assert cg.check_dangerous("git", ["push", "--force"]) is not None
        assert cg.check_dangerous("git", ["push", "-f"]) is not None
        # --force-with-lease allowed
        assert cg.check_dangerous("git", ["push", "--force-with-lease"]) is None

    def test_FR22_branch_delete_blocked(self):
        """FR-22: Block git branch -D and git push --delete."""
        assert cg.check_dangerous("git", ["branch", "-D", "x"]) is not None
        assert cg.check_dangerous("git", ["push", "--delete", "x"]) is not None

    def test_FR23_override_env(self, monkeypatch):
        """FR-23: CONTINUITY_ALLOW_DANGEROUS=1 disables blocking."""
        monkeypatch.setenv("CONTINUITY_ALLOW_DANGEROUS", "1")
        assert cg.check_dangerous("gh", ["pr", "merge"]) is None
        assert cg.check_dangerous("git", ["push", "--force"]) is None

    def test_FR24_blocked_logged(self, fake_gh, db_path):
        """FR-24: Blocked commands logged with blocked=1, exit_code=-1."""
        cg.intercept("gh", ["pr", "merge"], fake_gh, db_path)
        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT blocked, exit_code FROM cli_events"
        ).fetchone()
        assert row[0] == 1
        assert row[1] == -1

    def test_FR25_block_message_to_stderr(self, fake_gh, db_path, capsys):
        """FR-25: Block message written to stderr explaining what was blocked."""
        cg.intercept("gh", ["repo", "delete", "x"], fake_gh, db_path)
        captured = capsys.readouterr()
        assert "repo delete blocked" in captured.err
        assert "CONTINUITY_ALLOW_DANGEROUS" in captured.err


class TestPrefixStripping:
    """Corner case: -R (gh) and -C (git) prefix flags."""

    def test_gh_R_prefix_pr_merge_blocked(self):
        """gh -R owner/repo pr merge → still blocked."""
        msg = cg.check_dangerous("gh", ["-R", "owner/repo", "pr", "merge"])
        assert msg is not None
        assert "pr merge blocked" in msg

    def test_gh_R_prefix_pr_merge_auto_allowed(self):
        """gh -R owner/repo pr merge --auto → allowed."""
        msg = cg.check_dangerous("gh", ["-R", "owner/repo", "pr", "merge", "--auto"])
        assert msg is None

    def test_gh_RR_double_prefix(self):
        """gh -R a/b -R c/d pr merge → still blocked (both stripped)."""
        msg = cg.check_dangerous("gh", ["-R", "a/b", "-R", "c/d", "pr", "merge"])
        assert msg is not None

    def test_git_C_prefix_force_push_blocked(self):
        """git -C /path push --force → still blocked."""
        msg = cg.check_dangerous("git", ["-C", "/tmp/repo", "push", "--force"])
        assert msg is not None
        assert "force push blocked" in msg

    def test_git_C_prefix_normal_push_allowed(self):
        """git -C /path push → allowed."""
        msg = cg.check_dangerous("git", ["-C", "/tmp/repo", "push", "origin", "main"])
        assert msg is None

    def test_git_C_prefix_branch_D_blocked(self):
        """git -C /path branch -D x → blocked."""
        msg = cg.check_dangerous("git", ["-C", "/tmp/repo", "branch", "-D", "x"])
        assert msg is not None

    def test_git_CC_double_prefix(self):
        """git -C a -C b push -f → still blocked (both stripped)."""
        msg = cg.check_dangerous("git", ["-C", "a", "-C", "b", "push", "-f"])
        assert msg is not None

    def test_gh_R_prefix_not_blocking_normal(self):
        """gh -R owner/repo pr list → not blocked."""
        msg = cg.check_dangerous("gh", ["-R", "owner/repo", "pr", "list"])
        assert msg is None
