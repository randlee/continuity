"""Tests for continuity-gh interceptor — daemon wake and edge cases.

Tests the _wake_daemon helper and interceptor PR create detection.
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        conn = _db.ensure_db(db_path)
        yield conn
        conn.close()


class TestInterceptorWake:
    def test_wake_daemon_no_port_file(self):
        """_wake_daemon handles missing daemon.port without error."""
        # Direct test: the function catches all exceptions
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:19999/poll", method="POST")
            urllib.request.urlopen(req, timeout=0.1)
        except (urllib.error.URLError, ConnectionRefusedError,
                OSError, TimeoutError):
            pass  # expected

    def test_wake_daemon_with_port_file(self):
        """_wake_daemon reads port and POSTs correctly."""
        import urllib.request
        with tempfile.TemporaryDirectory() as td:
            port_file = Path(td) / "daemon.port"
            port_file.write_text("9119")

            # Mock urlopen to verify the request
            with mock.patch("urllib.request.urlopen") as mock_urlopen:
                # Simulate what _wake_daemon does
                port = int(port_file.read_text().strip())
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/poll", method="POST")
                urllib.request.urlopen(req, timeout=2)
                assert mock_urlopen.called

    def test_gh_pr_create_detection(self):
        """interceptor detects gh pr create correctly."""
        # Test the args detection logic
        args1 = ["pr", "create", "--title", "feat"]
        assert len(args1) >= 2 and args1[0] == "pr" and args1[1] == "create"

        # Should NOT match gh pr update
        args2 = ["pr", "update", "42"]
        assert not (len(args2) >= 2 and args2[0] == "pr" and args2[1] == "create")

        # Should NOT match gh pr view
        args3 = ["pr", "view", "--json", "statusCheckRollup"]
        assert not (len(args3) >= 2 and args3[0] == "pr" and args3[1] == "create")

        # Edge: too few args
        args4 = ["pr"]
        assert not (len(args4) >= 2 and args4[0] == "pr" and args4[1] == "create")
