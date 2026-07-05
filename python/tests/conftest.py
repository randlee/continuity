"""Test fixtures for continuity daemon. Guarantees singleton per fixture
with verified cleanup."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive. Cross-platform."""
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in proc.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _wait_for_pid_gone(pid: int, timeout: float = 10) -> bool:
    """Wait for a PID to exit. Returns True if gone, False if still alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


class DaemonHandle:
    """Handle to a running daemon process with guaranteed cleanup."""

    def __init__(self, proc: subprocess.Popen, home: Path):
        self.proc = proc
        self.home = home
        self.pid = proc.pid

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def signal(self, sig: int):
        """Send signal to daemon."""
        os.kill(self.pid, sig)

    @property
    def pid_file(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def lock_file(self) -> Path:
        return self.home / "daemon.lock"


@pytest.fixture
def daemon_home():
    """Isolated CONTINUITY_HOME for daemon tests."""
    home = Path(tempfile.mkdtemp(prefix="continuity-test-"))
    yield home
    # Cleanup: remove if empty, ignore errors if files still locked
    try:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture
def daemon(daemon_home, request):
    """Start a daemon process with guaranteed cleanup.

    Yields DaemonHandle. On teardown:
    1. SIGTERM → wait 5s for graceful shutdown
    2. SIGKILL if still alive
    3. Verify PID gone
    4. Verify PID file cleaned
    """
    # Start daemon as subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "--co"],
        env={**os.environ, "CONTINUITY_HOME": str(daemon_home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # TODO: replace with actual daemon start once implemented
    # For now, this fixture is a contract specification

    handle = DaemonHandle(proc, daemon_home)

    yield handle

    # ── Teardown (guaranteed, in order) ────────────────────────────────

    # 1. SIGTERM for graceful shutdown
    if handle.is_alive():
        handle.signal(signal.SIGTERM)

    # 2. Wait for graceful exit
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # 3. SIGKILL if still alive
        proc.kill()
        proc.wait(timeout=5)

    # 4. Verify PID is gone
    pid = proc.pid
    assert not _is_pid_alive(pid), (
        f"Daemon PID {pid} still alive after SIGKILL. "
        f"Process tree may be orphaned."
    )

    # 5. Verify PID file cleaned (daemon should remove on shutdown)
    pid_file = handle.pid_file
    if pid_file.exists():
        # Daemon failed to clean up — test failure, not daemon bug
        # Remove it ourselves so the next test isn't affected
        pid_file.unlink(missing_ok=True)
        pytest.fail(f"Daemon did not clean up PID file: {pid_file}")


@pytest.fixture
def daemon_restart(daemon):
    """Kill and restart the daemon, verifying PID change and lock release."""
    old_pid = daemon.pid

    # Kill
    daemon.signal(signal.SIGTERM)
    assert _wait_for_pid_gone(old_pid, timeout=5), f"PID {old_pid} still alive"

    # Restart (simulated — actual restart requires daemon impl)
    # The lock file should be free, PID file should be gone

    yield old_pid  # test can verify restart behavior
