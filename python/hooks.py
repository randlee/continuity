"""Post-push hook installer — appends continuity wake to existing hooks.

FR-45: Installed automatically by ci register.
FR-46: Cross-platform hook — curl POST /poll (sh on Unix, cmd on Windows).

Non-destructive: preserves existing user hook content, appends continuity
wake block. Detects already-installed block to avoid duplication.

Public API:
    install(repo_path) → Path  (installed hook path, or None if already present)
    uninstall(repo_path) → bool (True if continuity block removed)
    is_installed(repo_path) → bool
"""

import os
import stat
import sys
from pathlib import Path


# Sentinels for identifying the continuity-managed block
_CONTINUITY_START = "# === continuity start (managed, do not edit) ==="
_CONTINUITY_END = "# === continuity end ==="
# Windows uses same sentinels (:: comment prefix), content differs
_CONTINUITY_START_WIN = ":: === continuity start (managed, do not edit) ==="
_CONTINUITY_END_WIN = ":: === continuity end ==="

# Unix (sh) block
_CONTINUITY_BLOCK_SH = """# === continuity start (managed, do not edit) ===
if [ "$1" = "origin" ]; then
  PORT=$(cat "${CONTINUITY_HOME:-$HOME/.local/share/continuity}/daemon.port" 2>/dev/null)
  [ -n "$PORT" ] && curl -s -X POST "http://localhost:$PORT/poll" >/dev/null 2>&1 &
fi
# === continuity end ===
"""

# Windows (cmd) block
_CONTINUITY_BLOCK_WIN = r""":: === continuity start (managed, do not edit) ===
if /I "%1"=="origin" (
  set /p PORT=<"%CONTINUITY_HOME%\daemon.port" 2>nul
  if defined PORT curl -s -X POST http://localhost:%PORT%/poll >nul 2>&1
)
:: === continuity end ===
"""

# Full hook templates
_FULL_HOOK_SH = "#!/bin/sh\n" + _CONTINUITY_BLOCK_SH + "\n"
_FULL_HOOK_WIN = "@echo off\r\n" + _CONTINUITY_BLOCK_WIN + "\r\n"


def _is_windows() -> bool:
    return sys.platform == "win32"


def install(repo_path: str | Path) -> Path | None:
    """Install continuity wake in post-push hook. Preserves existing content.
    Returns hook path on new install, None if already installed."""
    repo = Path(repo_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_path = hooks_dir / "post-push"
    win = _is_windows()
    block = _CONTINUITY_BLOCK_WIN if win else _CONTINUITY_BLOCK_SH
    full = _FULL_HOOK_WIN if win else _FULL_HOOK_SH
    start_sentinel = _CONTINUITY_START_WIN if win else _CONTINUITY_START

    if hook_path.exists():
        existing = hook_path.read_text()
        if start_sentinel in existing or _CONTINUITY_START in existing:
            return None  # already installed
        content = existing.rstrip("\n") + "\n\n" + block + "\n"
    else:
        content = full

    hook_path.write_text(content)
    if not win:
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
    return hook_path


def uninstall(repo_path: str | Path) -> bool:
    """Remove continuity block from post-push hook.
    Returns True if block was removed, False if not found.
    Keeps the hook file if user content remains; removes file if only
    continuity content existed."""
    hook_path = Path(repo_path) / ".git" / "hooks" / "post-push"
    if not hook_path.exists():
        return False

    content = hook_path.read_text()
    win = _is_windows()
    start_sentinel = _CONTINUITY_START_WIN if win else _CONTINUITY_START
    end_sentinel = _CONTINUITY_END_WIN if win else _CONTINUITY_END

    if start_sentinel not in content and _CONTINUITY_START not in content:
        return False

    lines = content.split("\n")
    result = []
    in_block = False
    found_block = False

    for line in lines:
        stripped = line.strip()
        if stripped in (start_sentinel, _CONTINUITY_START):
            in_block = True
            found_block = True
            continue
        if stripped in (end_sentinel, _CONTINUITY_END):
            in_block = False
            continue
        if not in_block:
            result.append(line)

    stripped = "\n".join(result).strip()

    if not stripped or all(
        l.strip() in ("", "#!/bin/sh", "#!/bin/bash", "@echo off")
        for l in stripped.split("\n")
    ):
        hook_path.unlink()
    else:
        hook_path.write_text(stripped + "\n")

    return found_block


def is_installed(repo_path: str | Path) -> bool:
    """Check if continuity wake block is present in post-push hook."""
    hook_path = Path(repo_path) / ".git" / "hooks" / "post-push"
    try:
        content = hook_path.read_text()
        return (_CONTINUITY_START in content or
                _CONTINUITY_START_WIN in content)
    except (FileNotFoundError, PermissionError):
        return False
