"""Post-push hook installer — appends continuity wake to existing hooks.

FR-45: Installed automatically by ci register.
FR-46: Single cross-platform hook script — curl POST /poll.

Non-destructive: preserves existing user hook content, appends continuity
wake block. Detects already-installed block to avoid duplication.

Public API:
    install(repo_path) → Path  (installed hook path, or None if already present)
    uninstall(repo_path) → bool (True if continuity block removed)
    is_installed(repo_path) → bool
"""

import os
import stat
from pathlib import Path


# Sentinels for identifying the continuity-managed block
_CONTINUITY_START = "# === continuity start (managed, do not edit) ==="
_CONTINUITY_END = "# === continuity end ==="

# Block appended to existing hooks (no shebang — piggybacks on existing one)
_CONTINUITY_BLOCK = f"""{_CONTINUITY_START}
if [ "$1" = "origin" ]; then
  PORT=$(cat "${{CONTINUITY_HOME:-$HOME/.local/share/continuity}}/daemon.port" 2>/dev/null)
  [ -n "$PORT" ] && curl -s -X POST "http://localhost:$PORT/poll" >/dev/null 2>&1 &
fi
{_CONTINUITY_END}
"""

# Full hook for repos with no existing post-push hook
_FULL_HOOK = f"""#!/bin/sh
{_CONTINUITY_BLOCK}
"""


def install(repo_path: str | Path) -> Path | None:
    """Install continuity wake in post-push hook. Preserves existing content.
    Returns hook path on new install, None if already installed."""
    repo = Path(repo_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_path = hooks_dir / "post-push"

    if hook_path.exists():
        existing = hook_path.read_text()
        if _CONTINUITY_START in existing:
            return None  # already installed
        # Append our block to existing hook
        content = existing.rstrip("\n") + "\n\n" + _CONTINUITY_BLOCK + "\n"
    else:
        # Brand new hook
        content = _FULL_HOOK

    hook_path.write_text(content)
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
    if _CONTINUITY_START not in content:
        return False

    # Remove the continuity block (including surrounding whitespace)
    lines = content.split("\n")
    result = []
    in_block = False
    found_block = False

    for line in lines:
        if line.strip() == _CONTINUITY_START:
            in_block = True
            found_block = True
            continue
        if line.strip() == _CONTINUITY_END:
            in_block = False
            continue
        if not in_block:
            result.append(line)

    stripped = "\n".join(result).strip()

    if not stripped or all(
        l.strip() in ("", "#!/bin/sh", "#!/bin/bash")
        for l in stripped.split("\n")
    ):
        # Only continuity content remained — remove entire hook
        hook_path.unlink()
    else:
        hook_path.write_text(stripped + "\n")

    return found_block


def is_installed(repo_path: str | Path) -> bool:
    """Check if continuity wake block is present in post-push hook."""
    hook_path = Path(repo_path) / ".git" / "hooks" / "post-push"
    try:
        return _CONTINUITY_START in hook_path.read_text()
    except (FileNotFoundError, PermissionError):
        return False
