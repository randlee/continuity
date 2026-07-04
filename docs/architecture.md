# Continuity Architecture

Language-agnostic architecture for Continuity, the CI/PR monitoring system
for the Synaptic Canvas agent fleet.

## 1. Overview

Continuity tracks the state of pull requests and CI runs across registered
repositories. It maintains an immutable append-only event log and surfaces
state changes to agents and dashboards.

Two complementary data sources feed the system:

1. **CLI interception** — transparent wrappers around `gh` and `git` that log
   every invocation. This catches agent-driven actions (pr create, pr merge,
   pr checks, git push) with zero polling infrastructure.
2. **GitHub polling** — a daemon that queries the GitHub GraphQL API on an
   adaptive interval. This catches events from outside the CLI (web UI,
   other users, CI completion).

Both paths write to the same SQLite event log. Consumers (agents via ATM,
dashboards via sc-mux) read from SQLite and a structured log stream.

## 2. Goals

- Single source of truth for "what is the CI state of this repo/PR"
- Agents never poll GitHub directly — they read their ATM inbox or query
  continuity's SQLite
- Immutable audit trail of every state transition
- Adaptive polling conserves API tokens
- CLI interception provides immediate visibility with no polling overhead
- Extension points for ATM messaging and sc-mux dashboard

## 3. Non-Goals

- No webhook server — no tunnel, no public endpoint
- No multi-user or networked operation in v1
- No GitHub API beyond GraphQL (polling) and CLI pass-through (interception)
- ATM and sc-mux integration via defined extension points, not built-in

## 4. System Architecture

```
                    ┌──────────────┐
                    │   GitHub     │
                    └──┬───────┬───┘
                       │       │
              GraphQL  │       │  gh/git CLI
              polling  │       │  (agents)
                       │       │
                 ┌─────▼───────▼─────┐
                 │    Continuity     │
                 │                   │
                 │  ┌─────────────┐  │
                 │  │ CLI shim    │  │  ← transparent gh/git wrappers
                 │  │ (intercept) │  │
                 │  └──────┬──────┘  │
                 │         │         │
                 │  ┌──────▼──────┐  │
                 │  │ Poll daemon │  │  ← GraphQL polling loop
                 │  │ (future)    │  │
                 │  └──────┬──────┘  │
                 │         │         │
                 │  ┌──────▼──────┐  │
                 │  │   SQLite    │  │  ← append-only ci_events
                 │  │   + JSONL   │  │     structured log stream
                 │  └──────┬──────┘  │
                 └─────────┼─────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         ┌────▼───┐  ┌─────▼──────┐  ┌─▼──────────┐
         │  ATM   │  │  sc-mux    │  │  CLI status │
         │ inbox  │  │  dashboard │  │  log/history│
         └────────┘  └────────────┘  └─────────────┘
```

## 5. Component Details

### 5.1 CLI Interceptor (Phase 1)

Transparent wrappers that sit at higher PATH priority than the real `gh`/`git`.
Every invocation is logged to SQLite, then delegated to the real binary with
identical stdin/stdout/stderr/exit code. Zero observable difference.

**Intercepted commands of interest:**

| Command | Structured data extracted |
|---|---|
| `gh pr create` | PR number, branch, repo |
| `gh pr merge` | PR merged event |
| `gh pr checks` | CI job statuses |
| `gh pr view --json` | PR metadata + CI status rollup |
| `git push` | Push event (potential CI trigger) |

### 5.2 Poll Daemon (Phase 3+)

A persistent daemon that polls GitHub GraphQL on an adaptive interval.

**Lifecycle:**
1. Startup: open SQLite, authenticate via `gh auth token` (tokens in memory only)
2. Loop: sleep → query all repos per account → diff against SQLite → record
   changes → re-evaluate interval
3. Shutdown: SIGTERM graceful exit, SIGUSR1 immediate poll

**State diffing:** Incoming job states are compared against the latest
`ci_events` row per (repo, PR, job). A new row is inserted only when status
or conclusion differs. This prevents the event log from becoming a heartbeat.

### 5.3 SQLite Database

Single database at `~/.local/share/continuity/continuity.db`.
Override: `CONTINUITY_DB` environment variable.

WAL mode for concurrent reads (CLI) and writes (daemon/interceptor).

## 6. Activity Modes

The poll daemon operates in three modes, re-evaluated after each cycle:

| Mode | Condition | Interval |
|---|---|---|
| ACTIVE | Any PR has a job in QUEUED or IN_PROGRESS | 30s |
| WATCHFUL | Open PRs exist but no CI running | 5 min |
| IDLE | No open PRs across any registered repo | 30 min |

SIGUSR1 from a post-push hook triggers an immediate poll and transitions to
ACTIVE if new CI is detected. Rate limit pressure overrides mode — if
remaining points fall below threshold the interval increases regardless.

## 7. Data Model

### 7.1 `cli_events` — Raw Interception Log

Append-only. Every gh/git invocation recorded with args, exit code, and
duration. The audit trail before structured parsing.

### 7.2 `repos` — Tracked Repositories

| Column | Description |
|---|---|
| `owner_repo` | `owner/repo` (unique) |
| `gh_account` | GitHub keychain account name |
| `last_synced` | Unix timestamp of last poll |
| `avg_ci_duration` | EMA of successful CI duration (seconds) |
| `max_ci_duration` | Hard timeout ceiling |

### 7.3 `pull_requests` — PR State

| Column | Description |
|---|---|
| `owner_repo`, `pr_number` | Composite key |
| `branch` | Head branch name |
| `head_sha` | Latest commit |
| `mergeable` | MERGEABLE / CONFLICTING / UNKNOWN |
| `state` | OPEN / MERGED / CLOSED |

### 7.4 `ci_events` — Immutable CI Event Log

Append-only. Never updated, never deleted. Current state derived as latest
row per (owner_repo, pr_number, job_name).

| Column | Description |
|---|---|
| `owner_repo`, `pr_number` | Which PR |
| `job_name` | CI job identifier |
| `status` | QUEUED / IN_PROGRESS / COMPLETED / CONFLICT / TIMEOUT |
| `conclusion` | SUCCESS / FAILURE / CANCELLED / TIMED_OUT / SKIPPED / NULL |
| `recorded_at` | Unix timestamp |

**Current state query:**
```sql
SELECT job_name, status, conclusion, recorded_at
FROM ci_events
WHERE owner_repo = ? AND pr_number = ?
GROUP BY job_name
HAVING recorded_at = MAX(recorded_at);
```

### 7.5 `api_usage` — Rate Limit Tracking *(polling daemon only)*

| Column | Description |
|---|---|
| `gh_account` | Account name |
| `queried_at` | Timestamp |
| `cost` | GraphQL points consumed |
| `remaining` | Points remaining in window |
| `reset_at` | Window reset timestamp |

## 8. Trigger Events

State changes produce structured trigger events. These are the extension
points for ATM messaging and dashboard notifications.

| Event | Condition |
|---|---|
| `PrConflictDetected` | mergeable = CONFLICTING detected |
| `CiStarted` | First job transitions to QUEUED or IN_PROGRESS |
| `CiJobChanged` | Any job status or conclusion transition |
| `CiCompleted` | All jobs in terminal state |
| `CiSlow` | Elapsed > 2× avg_ci_duration (non-fatal) |
| `CiTimeout` | Elapsed > max_ci_duration |

## 9. Extension Points

### 9.1 ATM Integration

Trigger events are emitted as structured log entries. An ATM adapter
consumes these and calls `atm send <agent> "CI: PR #42 — checks passed"`.
Agents read their ATM inbox — they never poll GitHub directly.

### 9.2 sc-mux Dashboard

The dashboard queries continuity's SQLite directly for CI status.
Read path is non-critical — a slow dashboard load does not block
`atm send` or daemon operation.

### 9.3 Post-Push Hook

Optional hook installed by `continuity register`:
```sh
#!/bin/sh
# .git/hooks/post-push
[ "$1" = "origin" ] || exit 0
pkill -SIGUSR1 -x continuity 2>/dev/null || true
```

Wakes the daemon immediately on push rather than waiting for the next
scheduled poll.

## 10. Auth Model

Bearer tokens obtained once at daemon startup via the gh CLI keychain and
held in memory. Never written to disk. Refreshed on 401 response — not on
a timer.

```
# Startup: one subprocess per account
gh auth token --account randlee   → "ghp_xxxx" (in memory)

# Hot path: pure HTTP, no subprocesses
POST https://api.github.com/graphql
Authorization: Bearer ***
```

## 11. Key Design Decisions

| Decision | Rationale |
|---|---|
| CLI interception before polling | Catches agent actions immediately. No polling infrastructure needed for day-one value. |
| Append-only ci_events | Immutable audit log. Current state is derivable. No UPDATE logic. |
| GraphQL batch query per account | One API call covers all repos and all PRs. O(accounts), not O(repos). |
| Persistent daemon | Holds tokens in memory. Eliminates per-poll auth subprocess overhead. |
| Adaptive poll interval | Conserves API tokens when idle. ACTIVE/WATCHFUL/IDLE modes. |
| SQLite (not Postgres/MySQL) | Single binary, no server. WAL mode for concurrent reads. |
| CLI reads SQLite only | Status is always instant. No API calls on the read path. |
| Three-source architecture | Interception + polling + structured log. Each adds data without replacing the others. |
