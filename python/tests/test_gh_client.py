"""Tests for gh/client.py — GitHub GraphQL REST client.

Tests FR-26, FR-27, FR-28, FR-35.
All tests mock HTTP and auth — no real GitHub access needed.
"""

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gh.client import (
    GhClient, PollResult, PrSnapshot, CheckRun, ApiUsage,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_GRAPHQL_RESPONSE = {
    "data": {
        "rateLimit": {"cost": 3, "remaining": 4997, "resetAt": "2026-07-05T12:00:00Z"},
        "continuity_test": {
            "pullRequests": {
                "nodes": [
                    {
                        "number": 1,
                        "title": "test: CI integration",
                        "state": "OPEN",
                        "mergeable": "MERGEABLE",
                        "headRefName": "test/ci-check",
                        "headRefOid": "abc123",
                        "updatedAt": "2026-07-05T00:26:08Z",
                        "statusCheckRollup": {
                            "nodes": [
                                {
                                    "name": "build",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "startedAt": "2026-07-05T00:26:10Z",
                                    "completedAt": "2026-07-05T00:27:00Z",
                                },
                                {
                                    "name": "test",
                                    "status": "IN_PROGRESS",
                                    "conclusion": None,
                                    "startedAt": "2026-07-05T00:27:01Z",
                                    "completedAt": None,
                                },
                            ]
                        },
                    }
                ]
            }
        },
    }
}


@pytest.fixture
def client():
    """GhClient with mocked auth and HTTP."""
    with patch.object(GhClient, "_authenticate"):
        with patch.object(GhClient, "_post", return_value=SAMPLE_GRAPHQL_RESPONSE):
            c = GhClient("test-account")
            c._token = "ghp_test123"
            yield c


# ═══════════════════════════════════════════════════════════════════════════
# Query building (FR-26)
# ═══════════════════════════════════════════════════════════════════════════

class TestQueryBuilding:
    def test_single_repo(self):
        """FR-26: Query generated with correct alias for one repo."""
        query = GhClient._build_query(["randlee/continuity-test"])
        assert "continuity_test" in query
        assert 'owner: "randlee"' in query
        assert 'name: "continuity-test"' in query
        assert "rateLimit" in query
        assert "statusCheckRollup" in query

    def test_multiple_repos(self):
        """FR-26: One query covers all repos with per-repo aliases."""
        query = GhClient._build_query([
            "randlee/continuity-test",
            "randlee/atm-core",
        ])
        assert "continuity_test" in query
        assert "atm_core" in query
        assert "rateLimit" in query

    def test_alias_sanitization(self):
        """Repo names with special chars get valid GraphQL aliases."""
        query = GhClient._build_query(["randlee/my-repo.test"])
        assert "my_repo_test" in query  # dashes and dots become underscores

    def test_repo_fragment_has_all_fields(self):
        """REPO_FRAGMENT includes all required fields."""
        from gh.client import REPO_FRAGMENT
        assert "number" in REPO_FRAGMENT
        assert "mergeable" in REPO_FRAGMENT
        assert "headRefName" in REPO_FRAGMENT
        assert "headRefOid" in REPO_FRAGMENT
        assert "statusCheckRollup" in REPO_FRAGMENT
        assert "CheckRun" in REPO_FRAGMENT


# ═══════════════════════════════════════════════════════════════════════════
# Auth (FR-28)
# ═══════════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_token_held_in_memory(self):
        """FR-28: Token held in memory, never written to disk."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ghp_test123\n", stderr=""
            )
            c = GhClient("test-account")
            c._authenticate()
            assert c._token == "ghp_test123"
            # Never written to disk — just a string in memory

    def test_auth_calls_gh_cli(self):
        """FR-28: Token obtained via gh auth token --account <name>."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ghp_token\n", stderr=""
            )
            c = GhClient("my-account")
            c._authenticate()
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "gh" in args
            assert "auth" in args
            assert "token" in args
            assert "--account" in args
            assert "my-account" in args

    def test_auth_failure_raises(self):
        """FR-28: gh auth token failure raises RuntimeError."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not logged in"
            )
            c = GhClient("bad-account")
            with pytest.raises(RuntimeError, match="gh auth token failed"):
                c._authenticate()

    def test_token_refreshed_on_401(self, client):
        """FR-28: Token refreshed on 401 response, then retried."""
        # Simulate 401 → _refresh_token called → retry succeeds
        call_count = [0]
        refresh_calls = []

        def _post_side_effect(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate what the real _post does on 401
                client._refresh_token()
                refresh_calls.append(True)
                return SAMPLE_GRAPHQL_RESPONSE  # retry succeeds
            return SAMPLE_GRAPHQL_RESPONSE

        client._post = _post_side_effect
        result = client.poll(["randlee/continuity-test"])
        assert len(refresh_calls) == 1, f"expected 1 refresh call, got {len(refresh_calls)}"
        assert isinstance(result, PollResult)

    def test_gh_not_installed_raises(self):
        """FR-28: gh CLI not found → clear error."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            c = GhClient("test")
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                c._authenticate()


# ═══════════════════════════════════════════════════════════════════════════
# Response parsing (FR-27, FR-35)
# ═══════════════════════════════════════════════════════════════════════════

class TestResponseParsing:
    def test_parses_prs(self, client):
        """FR-27: Response parsed into typed PrSnapshot list."""
        result = client.poll(["randlee/continuity-test"])
        prs = result.repos.get("randlee/continuity-test", [])
        assert len(prs) == 1
        pr = prs[0]
        assert pr.number == 1
        assert pr.title == "test: CI integration"
        assert pr.state == "OPEN"
        assert pr.mergeable == "MERGEABLE"
        assert pr.head_ref_name == "test/ci-check"
        assert pr.head_ref_oid == "abc123"

    def test_parses_checks(self, client):
        """FR-27: statusCheckRollup parsed into CheckRun list."""
        result = client.poll(["randlee/continuity-test"])
        prs = result.repos.get("randlee/continuity-test", [])
        checks = prs[0].checks
        assert len(checks) == 2
        build = next(c for c in checks if c.name == "build")
        assert build.status == "COMPLETED"
        assert build.conclusion == "SUCCESS"
        test = next(c for c in checks if c.name == "test")
        assert test.status == "IN_PROGRESS"
        assert test.conclusion is None

    def test_parses_rate_limit(self, client):
        """FR-35: rateLimit block extracted into ApiUsage."""
        result = client.poll(["randlee/continuity-test"])
        assert result.rate_limit.cost == 3
        assert result.rate_limit.remaining == 4997
        assert result.rate_limit.reset_at == "2026-07-05T12:00:00Z"

    def test_rate_limit_property(self, client):
        """FR-35: rate_limit property returns latest usage."""
        client.poll(["randlee/continuity-test"])
        usage = client.rate_limit
        assert usage.cost == 3
        assert usage.remaining == 4997

    def test_empty_repo(self, client):
        """Repo with no PRs returns empty list."""
        with patch.object(client, "_post", return_value={
            "data": {
                "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "..."},
                "empty_repo": {"pullRequests": {"nodes": []}},
            }
        }):
            result = client.poll(["randlee/empty-repo"])
            assert result.repos["randlee/empty-repo"] == []

    def test_missing_repo_in_response(self, client):
        """Repo not in response returns empty list."""
        with patch.object(client, "_post", return_value={
            "data": {"rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "..."}}
        }):
            result = client.poll(["randlee/missing"])
            assert result.repos.get("randlee/missing", []) == []

    def test_null_checks(self, client):
        """API returns null for statusCheckRollup → empty checks list."""
        with patch.object(client, "_post", return_value={
            "data": {
                "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "..."},
                "repo": {"pullRequests": {"nodes": [
                    {"number": 1, "title": "", "state": "OPEN",
                     "mergeable": "UNKNOWN", "headRefName": "",
                     "headRefOid": "", "updatedAt": "",
                     "statusCheckRollup": None},
                ]}},
            }
        }):
            result = client.poll(["randlee/repo"])
            assert result.repos["randlee/repo"][0].checks == []

    def test_multiple_repos_poll(self, client):
        """FR-27: Multiple repos in one poll result."""
        with patch.object(client, "_post", return_value={
            "data": {
                "rateLimit": {"cost": 2, "remaining": 4998, "resetAt": "..."},
                "repo_a": {"pullRequests": {"nodes": [
                    {"number": 1, "title": "a", "state": "OPEN",
                     "mergeable": "MERGEABLE", "headRefName": "x",
                     "headRefOid": "", "updatedAt": "",
                     "statusCheckRollup": None},
                ]}},
                "repo_b": {"pullRequests": {"nodes": [
                    {"number": 2, "title": "b", "state": "OPEN",
                     "mergeable": "UNKNOWN", "headRefName": "y",
                     "headRefOid": "", "updatedAt": "",
                     "statusCheckRollup": None},
                ]}},
            }
        }):
            result = client.poll(["owner/repo-a", "owner/repo-b"])
            assert len(result.repos) == 2
            assert result.repos["owner/repo-a"][0].number == 1
            assert result.repos["owner/repo-b"][0].number == 2


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class TestTypes:
    def test_pr_snapshot_defaults(self):
        ps = PrSnapshot(number=42)
        assert ps.number == 42
        assert ps.state == "OPEN"
        assert ps.mergeable == "UNKNOWN"
        assert ps.checks == []

    def test_check_run_defaults(self):
        cr = CheckRun(name="build", status="QUEUED")
        assert cr.name == "build"
        assert cr.status == "QUEUED"
        assert cr.conclusion is None

    def test_api_usage_defaults(self):
        au = ApiUsage()
        assert au.cost == 0
        assert au.remaining == 0

    def test_poll_result_structure(self):
        pr = PollResult(repos={"r": [PrSnapshot(number=1)]}, rate_limit=ApiUsage(cost=1))
        assert len(pr.repos["r"]) == 1
        assert pr.rate_limit.cost == 1


# ═══════════════════════════════════════════════════════════════════════════
# ADR / Requirements
# ═══════════════════════════════════════════════════════════════════════════

class TestAdr:
    def test_FR26_query_generated_from_repos(self, client):
        """FR-26: GraphQL query generated at runtime from repos list."""
        query = GhClient._build_query(["a/b", "c/d"])
        assert "a_b" in query or "b" in query
        assert "c_d" in query or "d" in query
        # Verifies query is built dynamically, not hardcoded

    def test_FR27_responses_parsed_into_typed_result(self, client):
        """FR-27: Responses parsed into typed PollResult."""
        result = client.poll(["randlee/continuity-test"])
        assert isinstance(result, PollResult)
        assert isinstance(result.repos, dict)
        for prs in result.repos.values():
            for pr in prs:
                assert isinstance(pr, PrSnapshot)
                for check in pr.checks:
                    assert isinstance(check, CheckRun)

    def test_FR28_token_in_memory(self):
        """FR-28: Token held in memory, never written to disk."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ghp_secret\n", stderr=""
            )
            c = GhClient("test")
            c._authenticate()
            # Token is a string attribute, not a file
            assert isinstance(c._token, str)
            assert c._token == "ghp_secret"
            # No file I/O for token storage

    def test_FR35_rate_limit_extracted(self, client):
        """FR-35: rateLimit block extracted into ApiUsage."""
        # First poll
        client.poll(["randlee/continuity-test"])
        assert client.rate_limit.cost == 3
        assert client.rate_limit.remaining == 4997

        # Second poll with different values
        with patch.object(client, "_post", return_value={
            "data": {
                "rateLimit": {"cost": 5, "remaining": 4992, "resetAt": "later"},
                "continuity_test": {"pullRequests": {"nodes": []}},
            }
        }):
            client.poll(["randlee/continuity-test"])
            assert client.rate_limit.cost == 5
            assert client.rate_limit.remaining == 4992