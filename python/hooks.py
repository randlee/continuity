"""Post-push hook installer. Writes .git/hooks/post-push script.

FR-34: Installed automatically by continuity register.
FR-33: Sends SIGUSR1 to daemon on git push to origin.

Public API:
    install(repo_path) → Path  (installed hook path)
    uninstall(repo_path) → bool (True if removed)
    is_installed(repo_path) → bool
"""

import os
import stat
from pathlib import Path


HOOK_SCRIPT = """#!/bin/sh
# continuity post-push hook — wakes daemon on push to origin
REMOTE=$1
[ "$REMOTE" = "origin" ] || exit 0
# Find the continuity daemon PID file
PID_FILE="${CONTINUITY_HOME:-$HOME/.local/share/continuity}/daemon.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill -SIGUSR1 "$PID" 2>/dev/null || true
fi
exit 0
"""


def install(repo_path: str | Path) -> Path:
    """Install post-push hook in repo. Creates hooks dir if needed.
    Idempotent — overwrites existing continuity hook."""
    repo = Path(repo_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_path = hooks_dir / "post-push"
    hook_path.write_text(HOOK_SCRIPT)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
    return hook_path


def uninstall(repo_path: str | Path) -> bool:
    """Remove continuity post-push hook. Returns True if removed."""
    hook_path = Path(repo_path) / ".git" / "hooks" / "post-push"
    if hook_path.exists() and "continuity" in hook_path.read_text():
        hook_path.unlink()
        return True
    return False


def is_installed(repo_path: str | Path) -> bool:
    """Check if continuity post-push hook is installed."""
    hook_path = Path(repo_path) / ".git" / "hooks" / "post-push"
    try:
        return "continuity" in hook_path.read_text()
    except (FileNotFoundError, PermissionError):
        return False