"""Tests for cli/http_client.py — daemon HTTP communication.

Tests: get, post, error handling, port discovery, URL construction.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli.http_client import get, post, daemon_url, DaemonError, _read_port


class TestDaemonUrl:
    def test_builds_url(self):
        with mock.patch("cli.http_client._read_port", return_value=9119):
            assert daemon_url("/prs") == "http://127.0.0.1:9119/prs"

    def test_builds_url_with_query(self):
        with mock.patch("cli.http_client._read_port", return_value=9119):
            url = daemon_url("/prs?closed=true&repo=o/r")
            assert "9119" in url
            assert "closed=true" in url


class TestGet:
    def test_successful_get(self):
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(
            {"status": "ok", "prs": []}
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            result = get("/prs")
            assert result["status"] == "ok"

    def test_connection_refused(self):
        import urllib.error
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError()),
        ):
            with pytest.raises(DaemonError, match="daemon not running"):
                get("/prs")

    def test_invalid_json(self):
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__.return_value = mock_response

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(DaemonError, match="invalid response"):
                get("/prs")


class TestPost:
    def test_successful_post(self):
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(
            {"status": "ok", "message": "poll completed"}
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            result = post("/poll")
            assert result["status"] == "ok"

    def test_connection_refused_post(self):
        import urllib.error
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError()),
        ):
            with pytest.raises(DaemonError, match="daemon not running"):
                post("/poll")


class TestReadPort:
    def test_reads_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            port_file = Path(td) / "daemon.port"
            port_file.write_text("9999")
            with mock.patch.dict("os.environ", {"CONTINUITY_HOME": td}):
                assert _read_port() == 9999

    def test_default_when_no_file(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("cli.http_client._data_dir",
                           return_value=Path("/nonexistent")):
                assert _read_port() == 9119  # DEFAULT_PORT

    def test_default_when_invalid_content(self):
        with tempfile.TemporaryDirectory() as td:
            port_file = Path(td) / "daemon.port"
            port_file.write_text("not-a-number")
            with mock.patch.dict("os.environ", {"CONTINUITY_HOME": td}):
                assert _read_port() == 9119  # falls back
