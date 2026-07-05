"""GitHub GraphQL REST client. Pure HTTP interface — no daemon, no SQLite.

Public API:
    GhClient(account)     — holds token in memory, never on disk
    .poll(repos)          → PollResult (PRs + CI state + rate limit)
    .rate_limit           → ApiUsage (current state)

Testable by mocking _post() — no real HTTP needed.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CheckRun:
    name: str
    status: str
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class PrSnapshot:
    number: int
    title: str = ""
    state: str = "OPEN"
    mergeable: str = "UNKNOWN"
    head_ref_name: str = ""
    head_ref_oid: str = ""
    updated_at: str = ""
    checks: list[CheckRun] = field(default_factory=list)


@dataclass
class ApiUsage:
    cost: int = 0
    remaining: int = 0
    reset_at: str = ""


@dataclass
class PollResult:
    repos: dict[str, list[PrSnapshot]]  # owner/repo → PRs
    rate_limit: ApiUsage


# ═══════════════════════════════════════════════════════════════════════════
# Client
# ═══════════════════════════════════════════════════════════════════════════

GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

GRAPHQL_QUERY = """
query ContinuityPoll($repos: [RepositoryInput!]!) {
  rateLimit { cost remaining resetAt }
  _repos: repositories(input: $repos) {
    ... on Repository {
      ownerRepo
      pullRequests(states: OPEN, first: 20) {
        nodes {
          number title state mergeable
          headRefName headRefOid updatedAt
          statusCheckRollup {
            nodes {
              ... on CheckRun {
                name status conclusion startedAt completedAt
              }
            }
          }
        }
      }
    }
  }
}
"""

# Fallback: GitHub doesn't support repo aliases in a single query the way
# we want. We generate per-repo aliases instead.
PER_REPO_QUERY_TEMPLATE = """
query ContinuityPoll {{
  rateLimit {{ cost remaining resetAt }}
{queries}
}}
"""

REPO_FRAGMENT = """
  {alias}: repository(owner: "{owner}", name: "{repo}") {{
    pullRequests(states: OPEN, first: 20) {{
      nodes {{
        number title state mergeable
        headRefName headRefOid updatedAt
        statusCheckRollup {{
          nodes {{
            ... on CheckRun {{
              name status conclusion startedAt completedAt
            }}
          }}
        }}
      }}
    }}
  }}
"""


class GhClient:
    """GitHub GraphQL client. One instance per gh_account."""

    def __init__(self, account: str):
        self._account = account
        self._token: str | None = None
        self._last_usage = ApiUsage()

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def rate_limit(self) -> ApiUsage:
        return self._last_usage

    def poll(self, repos: list[str]) -> PollResult:
        """Query all repos for this account. repos = ['owner/repo', ...]."""
        if not self._token:
            self._authenticate()

        query = self._build_query(repos)
        data = self._post({"query": query})
        return self._parse_response(data, repos)

    # ── Auth (FR-28) ────────────────────────────────────────────────────

    def _authenticate(self):
        """Obtain token via gh auth token. Token held in memory, never on disk."""
        try:
            proc = subprocess.run(
                ["gh", "auth", "token", "--account", self._account],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"gh auth token failed for {self._account}: {proc.stderr.strip()}"
                )
            self._token = proc.stdout.strip()
        except FileNotFoundError:
            raise RuntimeError("gh CLI not found. Install gh: https://cli.github.com")

    def _refresh_token(self):
        """Refresh token on 401."""
        self._token = None
        self._authenticate()

    # ── Query building (FR-26) ──────────────────────────────────────────

    @staticmethod
    def _build_query(repos: list[str]) -> str:
        """Build GraphQL query with per-repo aliases."""
        fragments = []
        for r in repos:
            owner, _, repo = r.partition("/")
            alias = repo.replace("-", "_").replace(".", "_")
            fragments.append(REPO_FRAGMENT.format(
                alias=alias, owner=owner, repo=repo,
            ))
        return PER_REPO_QUERY_TEMPLATE.format(queries="".join(fragments))

    # ── HTTP (mockable for testing) ─────────────────────────────────────

    def _post(self, payload: dict) -> dict:
        """POST to GitHub GraphQL. Override in tests via mock."""
        req = urllib.request.Request(
            GRAPHQL_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._refresh_token()
                return self._post(payload)  # retry once
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub GraphQL {e.code}: {body[:500]}") from e

    # ── Response parsing (FR-27, FR-35) ─────────────────────────────────

    def _parse_response(self, data: dict, repos: list[str]) -> PollResult:
        """Parse GraphQL response into PollResult."""
        # rateLimit
        rl = data.get("data", {}).get("rateLimit", {})
        self._last_usage = ApiUsage(
            cost=rl.get("cost", 0),
            remaining=rl.get("remaining", 0),
            reset_at=rl.get("resetAt", ""),
        )

        # Per-repo PRs
        result: dict[str, list[PrSnapshot]] = {}
        d = data.get("data", {})
        for r in repos:
            _, _, repo = r.partition("/")
            alias = repo.replace("-", "_").replace(".", "_")
            repo_data = d.get(alias)
            if not repo_data:
                result[r] = []
                continue

            prs = []
            nodes = repo_data.get("pullRequests", {}).get("nodes", []) or []
            for node in nodes:
                checks = []
                rollup = node.get("statusCheckRollup", {}) or {}
                for check in (rollup.get("nodes", []) or []):
                    checks.append(CheckRun(
                        name=check.get("name", ""),
                        status=check.get("status", ""),
                        conclusion=check.get("conclusion"),
                        started_at=check.get("startedAt"),
                        completed_at=check.get("completedAt"),
                    ))
                prs.append(PrSnapshot(
                    number=node.get("number", 0),
                    title=node.get("title", ""),
                    state=node.get("state", "OPEN"),
                    mergeable=node.get("mergeable", "UNKNOWN"),
                    head_ref_name=node.get("headRefName", ""),
                    head_ref_oid=node.get("headRefOid", ""),
                    updated_at=node.get("updatedAt", ""),
                    checks=checks,
                ))
            result[r] = prs

        return PollResult(repos=result, rate_limit=self._last_usage)