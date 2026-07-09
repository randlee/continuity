# Continuity Requirements

Language-agnostic functional and non-functional requirements for Continuity,
the CI/PR monitoring system for the Synaptic Canvas agent fleet.

## 1. Phase 1 — CLI Interception (current)

### Functional

| ID | Requirement |
|---|---|
| FR-01 | Transparent `gh` wrapper intercepts all invocations, logs to SQLite, delegates to real binary with identical stdin/stdout/stderr/exit code |
| FR-02 | Transparent `git` wrapper intercepts push events, logs to SQLite, delegates to real binary identically |
| FR-03 | `cli_events` table records: command, args (JSON), exit code, duration (ms), timestamp |
| FR-04 | Interceptor overhead < 50ms beyond real binary execution time |
| FR-05 | SQLite in WAL mode. `CONTINUITY_DB` env var overrides database path |
| FR-06 | `gh` wrapper handles all subcommands including those with `--json` output and interactive prompts |
| FR-07 | `git` wrapper passes through all non-push commands with zero added latency |
| FR-08 | `git push` events recorded in `push_events` table with remote, ref, and timestamp |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-01 | Wrappers are indistinguishable from real binaries for all callers (agents, humans, scripts) |
| NF-02 | Exit codes match real binary exactly |
| NF-03 | Stdin/stdout/stderr flow unmodified — piping, redirection, and interactive output all work |
| NF-04 | No subprocess overhead beyond the delegation call itself |
| NF-05 | Single Python file per wrapper. No dependencies beyond stdlib |

## 2. Phase 2 — Structured Event Parsing

### Functional

| ID | Requirement |
|---|---|
| FR-09 | `gh pr create` output parsed: extract PR number, branch, owner/repo. Write to `pull_requests` |
| FR-10 | `gh pr merge` parsed: mark PR as MERGED in `pull_requests` |
| FR-11 | `gh pr view --json statusCheckRollup` parsed: extract CI job names, statuses, conclusions. Write to `ci_events` |
| FR-12 | `gh pr checks --json` parsed: same as statusCheckRollup |
| FR-13 | Unknown repos auto-registered in `repos` table on first encounter |
| FR-14 | State diffing: only write `ci_events` rows when status or conclusion differs from latest recorded |
| FR-15 | `repos` table schema: owner_repo (unique), gh_account, last_synced, avg_ci_duration, max_ci_duration, designated_member |
| FR-16 | `pull_requests` table schema: owner_repo + pr_number (unique), branch, head_sha, mergeable, state, updated_at |
| FR-17 | `ci_events` table: append-only. No UPDATE or DELETE. Index on (owner_repo, pr_number, job_name, recorded_at DESC) |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-06 | Parsing must not add observable latency — runs after delegation, not before |
| NF-07 | Parse failures must never affect the delegated command's exit code or output |
| NF-08 | Auto-registered repos must not require manual configuration |

## 3. Phase 3 — Dangerous Command Blocking

### Functional

| ID | Requirement |
|---|---|
| FR-18 | Block `gh pr merge` (without --auto): agent must merge via web UI or explicit override |
| FR-19 | Block `gh repo delete`: must use GitHub web UI |
| FR-20 | Block destructive `gh api` calls (DELETE, PATCH, PUT methods) |
| FR-21 | Block `git push --force` / `-f` (allow `--force-with-lease`) |
| FR-22 | Block `git branch -D` and `git push --delete` |
| FR-23 | `CONTINUITY_ALLOW_DANGEROUS=1` env var disables all blocking for a single invocation |
| FR-24 | Blocked commands logged to `cli_events` with `blocked=1` and exit code -1 |
| FR-25 | Block message written to stderr explaining what was blocked and how to override |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-09 | Blocking is immediate — no delegation to real binary for blocked commands |
| NF-10 | Unlike parsing, blocking happens before delegation (the command is never executed) |

## 4. Phase 4 — Polling Daemon

Decomposed into independent modules, each testable in isolation.

### 4.1 Module: `gh/client` — GitHub GraphQL REST Client

Pure REST interface to GitHub's GraphQL API. No daemon logic, no SQLite, no
polling. Testable with mocked HTTP responses.

| ID | Requirement |
|---|---|
| FR-26 | GraphQL query generated at runtime from `repos` table. One query per `gh_account` |
| FR-27 | Responses parsed into typed `PollResult` (PRs + statusCheckRollup + rateLimit) |
| FR-28 | Holds Bearer tokens in memory. Token via `gh auth token` at init. Refresh on 401 |
| FR-35 | Every response `rateLimit` block extracted into `ApiUsage` struct |

**Public API:**
```python
class GhClient:
    def __init__(self, account: str): ...
    def poll(self, repos: list[Repo]) -> PollResult: ...
    @property
    def rate_limit(self) -> ApiUsage: ...
```

### 4.2 Module: `diff` — State Diffing Engine

Pure functions. No I/O, no side effects, no DB writes. Compares incoming
data against current state.

| ID | Requirement |
|---|---|
| FR-29 | `diff_jobs(incoming, current)` → `list[CiEvent]` — only changed jobs |
| FR-30 | Identical poll results produce empty diff — no heartbeat entries |
| FR-37 | `diff_conflicts(incoming, current)` → mergeability changes |

**Public API:**
```python
def diff_jobs(incoming: list[JobState], current: dict[str, CiEvent]) -> list[CiEvent]: ...
def diff_prs(incoming: list[PrSnapshot], current: dict[int, PrState]) -> PrDiff: ...
```

### 4.3 Module: `daemon` — Per-Repo Poll Orchestrator

Orchestrates per-repo polling. Manages lifecycle, timer state, HTTP RPC
wake handling. Depends on `gh/client`, `diff`, `db`. Testable with mocked
`GhClient`.

| ID | Requirement |
|---|---|
| FR-31 | Each `owner/repo` tracks its own `next_poll_at` timestamp, independently recalculated after poll |
| FR-31a | Timer states: PR_CHANGED (30s, when any PR has mergeable=UNKNOWN), ACTIVE (5 min, when any CI job QUEUED or IN_PROGRESS), INACTIVE (20 min, otherwise). Shortest applicable interval wins |
| FR-31b | Poll loop sleeps for `min(repo_timers) - now`. Only repos whose timers have expired are polled |
| FR-31c | `POST /poll?repo=owner/repo` resets that repo's timer to `now` (immediate poll). `POST /poll` with no repo polls all repos |
| FR-31d | Account batching: multiple due repos under the same account are batched into a single GraphQL query |
| FR-32 | Timer recalculated after each repo poll based on that repo's current state |
| FR-33 | `ci register` adds repo + installs post-push hook |
| FR-36 | Rate limit backoff per account: when `remaining < LOW_WATER`, all repos under that account double their timer interval regardless of individual state |

**Singleton guarantees** (see Architecture §11):
- PID file at `$CONTINUITY_HOME/daemon.pid`
- Exclusive file lock at `$CONTINUITY_HOME/daemon.lock`
- Second instance detects lock → reads PID → error if alive, clears if stale

### 4.4 Module: `cli` — CLI Commands (Thin HTTP Client)

CLI commands are thin HTTP clients that call the daemon's RPC endpoints.
They do not read SQLite directly. The daemon owns all data access and
cache staleness logic.

| ID | Requirement |
|---|---|
| FR-38 | All CLI commands use daemon HTTP RPC. No direct SQLite reads from non-daemon processes |
| FR-39 | `ci status` → `GET /prs`. Renders open PRs + job states + activity mode |
| FR-40 | `ci log <repo> <pr#>` → `GET /prs/<owner>/<repo>/<num>`. Shows chronological `ci_events` |
| FR-41 | `ci history <repo>` → `GET /prs?closed=true&repo=<owner>/<repo>`. Shows closed PRs with outcomes and durations |
| FR-42 | `ci usage` → `GET /status`. Shows API point consumption per account |
| FR-43 | `ci poll` → `POST /poll`. Triggers immediate poll cycle, returns fresh data |
| FR-44 | CLI discovers daemon port from `$CONTINUITY_HOME/daemon.port` |

**[OBSOLESCENCE]** Direct SQLite reads from CLI path are deprecated. Mark
`_query_prs()`, `_query_pr_detail()` in CLI module with `# OBSOLETE` once
HTTP RPC is live. Remove after task 4 verification.

### 4.5 Module: `hooks` — Post-Push Hook

| ID | Requirement |
|---|---|
| FR-45 | Installed automatically by `ci register` |
| FR-46 | Single cross-platform hook script — `curl -s -X POST http://localhost:$PORT/poll` |
| FR-47 | Port discovered from `$CONTINUITY_HOME/daemon.port` (written at daemon startup) |
| FR-48 | Hook is fire-and-forget — backgrounded, does not block git push |

### 4.6 Module: `timer` — Per-Repo Timer Calculator

Pure function. No I/O.

| ID | Requirement |
|---|---|
| FR-31 | `calc_next_poll(state, rate_limit_remaining) → seconds` |
| FR-31a | Returns 30s (PR_CHANGED / UNKNOWN mergeable), 300s (ACTIVE / CI running), 1200s (INACTIVE) |
| FR-36 | Rate limit backoff: when `remaining < LOW_WATER` (1,000), interval × 2; when `remaining < LOW_WATER / 2`, interval × 4. Restore to state-determined interval when remaining recovers |

### 4.7 Module: `interceptor` — Per-Repo Daemon Wake

| ID | Requirement |
|---|---|
| FR-50 | Interceptor calls `POST /poll?repo=owner/repo` on daemon HTTP RPC after logging `gh pr create` |
| FR-51 | Wake is fire-and-forget — interceptor does not block on daemon response |
| FR-52 | Wake uses the canonical `POST /poll?repo=` endpoint. No platform-specific signal code in the interceptor |
| FR-53 | Wake is per-repo: only the new PR's repo timer is reset. Other repos are unaffected |

### 4.8 Module: `db` — SQLite Write Safety

| ID | Requirement |
|---|---|
| FR-54 | Only the daemon's poll thread writes to SQLite. All other components (HTTPD, notify, interceptor) open their own read connections |
| FR-55 | No `check_same_thread=False` anywhere. Each thread owns its connection |
| FR-56 | Daemon commits (`.commit()`) before dispatching notifications. Notification thread always sees committed state |
| NF-14 | No subprocesses in the hot path (poll loop). Only `gh auth token` at startup |
| NF-15 | Trigger callbacks fire in spawned tasks. Main poll loop is never blocked by a trigger |
| NF-16 | SQLite in WAL mode. Daemon writes; readers use own connections |

## 5. Phase 5 — Resilience (Slow/Timeout Detection + EMA)

**Note:** Two complementary paths:
- **Daemon poll** — updates EMA on CI completion. Does NOT emit slow/timeout alerts.
- **Interceptor** (Sprint 5) — detects slow/timeout when agents run `gh pr view`/`gh pr checks`. Emits `CiSlow`/`CiTimeout` events.

### 5.1 DAEMON: EMA Tracking on CI Completion

| ID | Requirement |
|---|---|
| FR-57 | `avg_ci_duration` updated as EMA (α=0.2) on `conclusion = SUCCESS` only, by daemon poll |
| FR-58 | Minimum 3 successful runs before thresholds apply |
| FR-59 | EMA persisted in `repos.avg_ci_duration` column |

### 5.2 INTERCEPTOR: Slow/Timeout Detection (Sprint 5)

| ID | Requirement |
|---|---|
| FR-60 | `CiSlow` trigger fires when elapsed > 2× avg_ci_duration (non-fatal, CI continues) — detected by interceptor on `gh pr view`/`gh pr checks` |
| FR-61 | `CiTimeout` trigger fires when elapsed > max_ci_duration (or 2× avg if NULL) — detected by interceptor |
| FR-62 | Slow/timeout triggers fire at most once per CI run |
| FR-63 | Interceptor reads `avg_ci_duration` and `max_ci_duration` from `repos` table |
| FR-64 | Interceptor captures `ATM_IDENTITY` for notification routing

## 6. Phase 6 — Extension Points (ATM / sc-mux)

### 6.1 Shared Trigger Infrastructure

| ID | Requirement |
|---|---|
| FR-65 | Trigger events emitted as structured log entries with action, level, and typed fields |
| FR-66 | Extension consumers have no dependency on continuity internals |

### 6.2 ATM Notifications — see [requirements-atm.md](requirements-atm.md)

The ATM module is a self-contained adapter that consumes trigger events
and routes notifications to ATM team members. Full requirements including
the notification routing matrix, fallback chain, identity model, and
designated member mechanism are defined in the ATM-specific requirements
document. Architectural decisions are recorded in
[ADR 001](adr/001-atm-notifications.md).

Key design points:
- Module is a no-op when `ATM_TEAM` or `ATM_IDENTITY` is unset
- Notifications route to the requesting member, falling back to `team-lead`
- CI completion, slow, and timeout events always route to `team-lead`
- Files causing merge conflicts are listed in unmergable notifications
  (capped at 6, with total count)
- `ci` is a permanent team member registered via `atm team member add`;
  messages include the requesting identity in the body
- Transient ATM failures retry 3× with backoff; permanent failures fall
  back immediately

### 6.3 sc-mux Dashboard

| ID | Requirement |
|---|---|
| FR-67 | sc-mux dashboard reads continuity SQLite for CI status per registered repo/session |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-18 | ATM message delivery is fire-and-forget. Failed delivery does not block the poll loop |
| NF-19 | Dashboard reads are non-critical. A slow query does not affect `atm send` or daemon operation |

## 8. Active Implementation Tasks

Prioritized from architecture §15. Obsolescence convention: old code is
marked `# OBSOLETE` but NOT deleted until new path is verified.

| # | Task | Effort | Depends On | Makes Obsolete |
|:---:|:---|:---:|:---:|:---|
| 0 | Per-repo timer model — replace global `ActivityMode` with per-repo `_timers` | Large | — | Global `ActivityMode`, `_recalculate_mode()`, `_next_interval()`, `_poll_lock` |
| 1 | SQLite write safety — per-thread connections, remove `check_same_thread=False` | Medium | — | `check_same_thread=False` hack, shared `self.db` |
| 2 | Rate limit cost logging + tune `LOW_WATER` | Small | — | — |
| 3 | Dead code cleanup — remove global mode infrastructure | Small | 0 | Global mode code (post task 0 verification) |
| 4 | CLI commands use HTTP RPC | Medium | — | Direct SQLite reads from CLI |
| 5 | `CiSlow`/`CiTimeout` in interceptor parse path | Medium | — | Daemon-side slow/timeout detection |
| 6 | End-to-end integration test | Medium | 0, 4, 5 | — |
| 7 | Timer startup recovery | Small | — | Startup race window |

### Obsolescence Convention

1. Add `# OBSOLETE: replaced by <mechanism> — remove after task <#> verified` above old code.
2. Keep old code alongside new until tests validate the replacement.
3. Remove in a dedicated cleanup commit after verification.
4. Move completed task from this table to Done.

## 9. CLI Command Summary

| Command | Calls | Description |
|---|---|---|
| `ci daemon` | Auth at startup only | Start the poll daemon. Blocks. |
| `ci register <owner/repo> --account <name>` | 1 (auth verify) | Add repo to tracking. Install post-push hook. |
| `ci status` | `GET /prs` | Show all open PRs + current job states via HTTP RPC. |
| `ci log <repo> <pr#>` | `GET /prs/<owner>/<repo>/<num>` | Show all ci_events for a PR via HTTP RPC. |
| `ci history <repo> [--limit N]` | `GET /prs?closed=true` | Show closed PRs with CI outcomes via HTTP RPC. |
| `ci usage [--account <name>]` | `GET /status` | Show API point consumption via HTTP RPC. |
| `ci poll` | `POST /poll` | Trigger immediate poll cycle. |
