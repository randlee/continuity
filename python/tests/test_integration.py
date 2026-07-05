"""Integration tests against real gh/git CLI using randlee/continuity-test.

Verifies the installed interceptor works end-to-end with actual binaries.
Uses a dedicated test repo to avoid side effects on real projects.

Requires: gh CLI installed, continuity-test repo cloned.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REAL_GH = shutil.which("gh")
REAL_GIT = shutil.which("git")
GH_AVAILABLE = REAL_GH is not None and REAL_GIT is not None

# Resolve REAL binaries by scanning PATH, skipping any continuity wrappers
def _find_real_binary(name: str) -> str:
    """Find the real gh/git, skipping continuity wrappers on PATH."""
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for p in paths:
        candidate = os.path.join(p, name)
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            content = Path(candidate).read_text()
            if "continuity" in content.lower():
                continue  # skip wrapper
        except (OSError, UnicodeDecodeError):
            pass  # binary file, not a wrapper
        return candidate
    return shutil.which(name)  # fallback

pytestmark = pytest.mark.skipif(not GH_AVAILABLE, reason="gh CLI not installed")

TEST_REPO = "randlee/continuity-test"
TEST_REPO_CLONE = Path(__file__).resolve().parent.parent.parent / "continuity-test"


@pytest.fixture(scope="module")
def test_repo():
    """Ensure continuity-test is cloned and on main."""
    if not TEST_REPO_CLONE.exists():
        subprocess.run(
            ["git", "clone", f"https://github.com/{TEST_REPO}.git",
             str(TEST_REPO_CLONE)],
            check=False, timeout=60,
        )
    if TEST_REPO_CLONE.exists():
        subprocess.run(["git", "-C", str(TEST_REPO_CLONE), "checkout", "main"],
                       capture_output=True, timeout=15)
        subprocess.run(["git", "-C", str(TEST_REPO_CLONE), "pull", "origin", "main"],
                       capture_output=True, timeout=15)
    return TEST_REPO_CLONE


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.db"


@pytest.fixture
def install_interceptor(db_path, monkeypatch):
    """Install the interceptor for this test. Returns (wrapper_path, db_path)."""
    monkeypatch.setenv("CONTINUITY_DB", str(db_path))

    # Install entire python/ dir to a temp location so imports resolve
    src_dir = Path(__file__).resolve().parent.parent
    install_dir = Path(tempfile.mkdtemp())
    shutil.copytree(src_dir, install_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
    gh_file = install_dir / "continuity-gh"

    # Resolve real binaries cleanly (skip any stale wrappers on PATH)
    real_gh = _find_real_binary("gh")
    real_git = _find_real_binary("git")
    monkeypatch.setenv("CONTINUITY_REAL_GH", real_gh)
    monkeypatch.setenv("CONTINUITY_REAL_GIT", real_git)

    # Create bin dir with wrappers
    bin_dir = Path(tempfile.mkdtemp())
    wrapper_gh = bin_dir / "gh"
    wrapper_gh.write_text(f"""#!/bin/bash
exec {sys.executable} {gh_file} "$@"
""")
    wrapper_gh.chmod(0o755)

    wrapper_git = bin_dir / "git"
    wrapper_git.write_text(f"""#!/bin/bash
export CONTINUITY_COMMAND=git
exec {sys.executable} {gh_file} "$@"
""")
    wrapper_git.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    yield wrapper_gh, db_path

    shutil.rmtree(install_dir, ignore_errors=True)
    shutil.rmtree(bin_dir, ignore_errors=True)


class TestTransparentPassthrough:
    """Verify wrapper is indistinguishable from real binaries."""

    def test_gh_version(self, install_interceptor):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0
        assert "gh version" in proc.stdout

    def test_git_status(self, install_interceptor, test_repo):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["git", "-C", str(test_repo), "status"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0

    def test_exit_codes_preserved(self, install_interceptor):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["gh", "nonexistent-command"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode != 0
        db = sqlite3.connect(str(db_path))
        recorded = db.execute(
            "SELECT exit_code FROM cli_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert recorded == proc.returncode


class TestEventLogging:
    """Verify every invocation produces cli_events."""

    def test_gh_logged(self, install_interceptor):
        wrapper, db_path = install_interceptor
        subprocess.run(["gh", "--version"], capture_output=True, timeout=15)

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command, exit_code, blocked FROM cli_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "gh"
        assert row[1] == 0
        assert row[2] == 0

    def test_git_logged(self, install_interceptor, test_repo):
        wrapper, db_path = install_interceptor
        subprocess.run(
            ["git", "-C", str(test_repo), "status"],
            capture_output=True, timeout=15,
        )
        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT command FROM cli_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "git"

    def test_args_json_recorded(self, install_interceptor):
        wrapper, db_path = install_interceptor
        subprocess.run(
            ["gh", "pr", "list", "--limit", "1"],
            capture_output=True, timeout=15,
        )
        db = sqlite3.connect(str(db_path))
        args_json = db.execute(
            "SELECT args_json FROM cli_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        args = json.loads(args_json)
        assert "pr" in args
        assert "list" in args


class TestStructuredParsing:
    """Verify gh commands populate pull_requests and ci_events tables."""

    def test_pr_view_parses_ci(self, db_path):
        """gh pr view --json should auto-register repo and parse CI state."""
        # Run gh directly (not through wrapper), capture output
        proc = subprocess.run(
            ["gh", "pr", "view", "1",
             "--json", "number,headRefName,mergeable,state,statusCheckRollup"],
            capture_output=True, text=True, timeout=15,
            cwd="/Volumes/Extreme Pro/github/continuity/continuity-test",
        )
        assert proc.returncode == 0, f"gh failed: {proc.stderr[:500]}"

        # Feed output through the parser manually
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import db as _db
        conn = _db.ensure_db(db_path)
        from gh.pr_view import parse as parse_view
        parse_view(conn, ["pr", "view", "1", "--json", "..."], proc.stdout,
                   lambda: "randlee/continuity-test")
        conn.commit()

        # Verify parsed data
        pr = conn.execute("SELECT pr_number, state FROM pull_requests").fetchone()
        assert pr is not None, "no PR metadata parsed"
        assert pr[0] == 1
        ci_count = conn.execute("SELECT COUNT(*) FROM ci_events").fetchone()[0]
        assert ci_count > 0, f"no CI events parsed (got {ci_count})"

    def test_pr_create_parsed(self, db_path):
        """gh pr create output should be parseable into pull_requests."""
        # Simulate gh pr create output
        stdout = "https://github.com/randlee/continuity-test/pull/99\n"

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import db as _db
        conn = _db.ensure_db(db_path)
        from gh.pr_create import parse as parse_create

        parse_create(conn, ["pr", "create", "--head", "feat/x"], stdout,
                     lambda r: conn.execute(
                         "INSERT OR IGNORE INTO repos (owner_repo, gh_account) VALUES (?, ?)",
                         (r, r.split("/")[0]) if "/" in r else (r, r)))
        conn.commit()

        pr = conn.execute(
            "SELECT owner_repo, pr_number, branch, state FROM pull_requests"
        ).fetchone()
        assert pr == ("randlee/continuity-test", 99, "feat/x", "OPEN")


class TestDangerousCommandBlocking:
    """Verify dangerous commands blocked BEFORE delegation."""

    def test_pr_merge_blocked(self, install_interceptor):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["gh", "pr", "merge", "99999"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 1
        assert "pr merge blocked" in proc.stderr

        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT blocked, exit_code FROM cli_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 1
        assert row[1] == -1

    def test_repo_delete_blocked(self, install_interceptor):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["gh", "repo", "delete", "nonexistent/repo"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 1
        assert "repo delete blocked" in proc.stderr

    def test_force_push_blocked(self, install_interceptor, test_repo):
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["git", "-C", str(test_repo), "push", "--force", "origin", "main"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 1
        assert "force push blocked" in proc.stderr

    def test_override_unblocks(self, install_interceptor, monkeypatch):
        monkeypatch.setenv("CONTINUITY_ALLOW_DANGEROUS", "1")
        wrapper, db_path = install_interceptor
        proc = subprocess.run(
            ["gh", "pr", "merge", "99999"],
            capture_output=True, text=True, timeout=15,
        )
        # Should reach GitHub (fail on nonexistent PR, not "blocked")
        assert "pr merge blocked" not in proc.stderr


class TestAzureGuard:
    """Verify gh blocked on Azure repos."""

    def test_azure_remote_blocks_gh(self, install_interceptor, test_repo, monkeypatch):
        """Simulate an Azure remote and verify gh is blocked."""
        monkeypatch.chdir(test_repo)
        wrapper, db_path = install_interceptor
        # Temporarily change origin to an Azure-looking URL
        old_url = subprocess.run(
            ["git", "-C", str(test_repo), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        try:
            subprocess.run(
                ["git", "-C", str(test_repo), "remote", "set-url", "origin",
                 "https://dev.azure.com/org/project/_git/repo"],
                capture_output=True, timeout=5, check=True,
            )
            proc = subprocess.run(
                ["gh", "-R", TEST_REPO, "pr", "list"],
                capture_output=True, text=True, timeout=15,
            )
            assert proc.returncode == 1
            assert "Azure DevOps" in proc.stderr
            assert "Use 'az' CLI" in proc.stderr
        finally:
            subprocess.run(
                ["git", "-C", str(test_repo), "remote", "set-url", "origin", old_url],
                capture_output=True, timeout=5,
            )


class TestCONTINUITY_DB:
    """Verify CONTINUITY_DB env var respected."""

    def test_custom_db_path(self, monkeypatch, test_repo):
        """Custom CONTINUITY_DB writes to specified path."""
        custom = Path(tempfile.mktemp(suffix=".db"))
        monkeypatch.setenv("CONTINUITY_DB", str(custom))

        # Build interceptor
        src_dir = Path(__file__).resolve().parent.parent
        install_dir = Path(tempfile.mkdtemp())
        shutil.copytree(src_dir, install_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        gh_file = install_dir / "continuity-gh"
        monkeypatch.setenv("CONTINUITY_REAL_GH", _find_real_binary("gh"))
        monkeypatch.setenv("CONTINUITY_REAL_GIT", _find_real_binary("git"))

        bin_dir = Path(tempfile.mkdtemp())
        wrapper = bin_dir / "gh"
        wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} {gh_file} \"$@\"\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        subprocess.run(["gh", "--version"], capture_output=True, timeout=15)

        assert custom.exists()
        db = sqlite3.connect(str(custom))
        count = db.execute("SELECT COUNT(*) FROM cli_events").fetchone()[0]
        assert count >= 1

        shutil.rmtree(install_dir, ignore_errors=True)
        shutil.rmtree(bin_dir, ignore_errors=True)
        custom.unlink(missing_ok=True)
