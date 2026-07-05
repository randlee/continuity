"""Binary resolution and subprocess runner. Cross-platform.

Public API:
    resolve_binary(name)   → str   (platform-aware gh/git path)
    run_binary(binary, args, timeout) → CompletedProcess
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def resolve_binary(name: str) -> str:
    """Resolve real gh/git binary path, platform-aware.
    Skips continuity wrapper scripts to avoid infinite recursion."""
    if sys.platform == "win32":
        name = f"{name}.exe"
    resolved = shutil.which(name)
    if resolved:
        # Check if resolved path is a continuity wrapper — skip it
        try:
            content = Path(resolved).read_text()
            if "continuity" in content.lower():
                # This is our wrapper — find the real binary by scanning PATH
                paths = os.environ.get("PATH", "").split(os.pathsep)
                for p in paths:
                    # Skip the directory containing this wrapper and ~/.local/bin
                    if p == str(Path(resolved).parent) or ".local/bin" in p:
                        continue
                    candidate = os.path.join(p, name)
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        return candidate
        except (OSError, UnicodeDecodeError):
            pass
        return resolved
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA",
                              os.path.expandvars(r"%USERPROFILE%\AppData\Local"))
        return str(Path(base) / "Programs" / "Git" / "cmd" / f"{name}")
    elif sys.platform == "darwin":
        return f"/opt/homebrew/bin/{name}" if "gh" in name else f"/usr/bin/{name}"
    else:
        return f"/usr/bin/{name}"


def run_binary(binary: str, args: list[str],
               timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a binary or script, auto-detecting Python scripts for cross-platform
    compatibility (shebangs don't work on Windows)."""
    is_python_script = False
    try:
        with open(binary, "r", encoding="utf-8") as f:
            first_line = f.readline()
        is_python_script = (
            ("python" in first_line and first_line.startswith("#!"))
            or binary.endswith(".py")
        )
    except (OSError, UnicodeDecodeError):
        pass

    cmd = [sys.executable, binary] if is_python_script else [binary]
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
