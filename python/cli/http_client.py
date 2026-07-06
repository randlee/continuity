"""HTTP client for CLI commands — talks to daemon HTTP RPC server.

Zero external dependencies — urllib.request is stdlib.

Public API:
    get(endpoint)  → dict   (GET request, parsed JSON)
    post(endpoint) → dict   (POST request, parsed JSON)
    daemon_url(endpoint) → str  (full URL with port)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from constants import DEFAULT_PORT


def _data_dir() -> Path:
    """Platform-appropriate data directory (same as interceptor)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA",
                              os.path.expandvars(r"%USERPROFILE%\AppData\Local"))
        return Path(base) / "continuity"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return Path(xdg) / "continuity"
    return Path.home() / ".local" / "share" / "continuity"


def _read_port() -> int:
    """Read daemon port from $CONTINUITY_HOME/daemon.port.
    Returns DEFAULT_PORT if file not found."""
    continuity_home = os.environ.get("CONTINUITY_HOME", "")
    if continuity_home:
        port_file = Path(continuity_home) / "daemon.port"
    else:
        port_file = _data_dir() / "daemon.port"
    try:
        return int(port_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        # Fallback: try default port for compatibility
        return DEFAULT_PORT


def daemon_url(endpoint: str) -> str:
    """Build full daemon URL: http://127.0.0.1:{port}{endpoint}"""
    return f"http://127.0.0.1:{_read_port()}{endpoint}"


def get(endpoint: str) -> dict:
    """GET request to daemon, returns parsed JSON. Raises on error."""
    url = daemon_url(endpoint)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError) or "refused" in str(e.reason).lower():
            raise DaemonError("daemon not running — start with 'ci daemon'")
        raise DaemonError(f"daemon not responding: {e.reason}")
    except json.JSONDecodeError:
        raise DaemonError("invalid response from daemon")


def post(endpoint: str) -> dict:
    """POST request to daemon, returns parsed JSON. Raises on error."""
    url = daemon_url(endpoint)
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError) or "refused" in str(e.reason).lower():
            raise DaemonError("daemon not running — start with 'ci daemon'")
        raise DaemonError(f"daemon not responding: {e.reason}")
    except json.JSONDecodeError:
        raise DaemonError("invalid response from daemon")


class DaemonError(Exception):
    """Raised when daemon communication fails."""
    pass
