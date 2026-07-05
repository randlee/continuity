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
| FR-15 | `repos` table schema: owner_repo (unique), gh_account, last_synced, avg_ci_duration, max_ci_duration |
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

### 4.3 Module: `daemon` — Poll Loop + Lifecycle

Orchestrates polling. Manages lifecycle, mode transitions, signal handlers.
Depends on `gh/client`, `diff`, `db`. Testable with mocked `GhClient`.

| ID | Requirement |
|---|---|
| FR-31 | ACTIVE (30s), WATCHFUL (5m), IDLE (30m) adaptive modes |
| FR-32 | Mode re-evaluated after each poll cycle |
| FR-33 | SIGUSR1 triggers immediate unscheduled poll |
| FR-36 | If rate limit remaining < LOW_WATER, increase interval regardless of mode |
| FR-43 | `continuity register` adds repo + installs post-push hook |

**Singleton guarantees** (see Architecture §11):
- PID file at `$CONTINUITY_HOME/daemon.pid`
- Exclusive file lock at `$CONTINUITY_HOME/daemon.lock`
- Second instance detects lock → reads PID → error if alive, clears if stale

### 4.4 Module: `cli/daemon_cmd` — CLI Commands

Read-only SQLite queries. No `gh` calls on the read path.

| ID | Requirement |
|---|---|
| FR-38 | All CLI commands read SQLite only — no `gh` calls |
| FR-39 | `continuity status` renders open PRs + job states + activity mode |
| FR-40 | `continuity log <repo> <pr#>` shows chronological `ci_events` |
| FR-41 | `continuity history <repo>` shows closed PRs with outcomes and durations |
| FR-42 | `continuity usage` shows API point consumption per account |

### 4.5 Module: `hooks` — Post-Push Hook

| ID | Requirement |
|---|---|
| FR-34 | Installed automatically by `continuity register` |
| FR-33 | Sends SIGUSR1 to daemon on `git push` to origin |

### 4.6 Module: `poll` — Adaptive Interval Calculator

Pure function. No I/O.

| ID | Requirement |
|---|---|
| FR-31 | `next_interval(mode, rate_limit_remaining)` → seconds |
| FR-36 | Rate limit backoff: double interval when remaining < LOW_WATER, up to cap |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-11 | Daemon is the only component that talks to GitHub. CLI reads SQLite exclusively |
| NF-12 | `ci_events` is append-only. No UPDATE or DELETE on that table |
| NF-13 | Bearer tokens never written to disk by continuity |
| NF-14 | No subprocesses in the hot path (poll loop). Only `gh auth token` at startup |
| NF-15 | Trigger callbacks fire in spawned tasks. Main poll loop is never blocked by a trigger |
| NF-16 | SQLite in WAL mode. Daemon writes; CLI reads concurrently without blocking |
| NF-17 | `CONTINUITY_DB` overrides database path for all components |

## 5. Phase 5 — Resilience (Slow/Timeout Detection + EMA)

### Functional

| ID | Requirement |
|---|---|
| FR-44 | `avg_ci_duration` updated as EMA (α=0.2) on `conclusion = SUCCESS` only |
| FR-45 | Minimum 3 successful runs before thresholds apply |
| FR-46 | `CiSlow` trigger fires when elapsed > 2× avg_ci_duration (non-fatal, CI continues) |
| FR-47 | `CiTimeout` trigger fires when elapsed > max_ci_duration (or 2× avg if NULL) |
| FR-48 | Slow/timeout triggers fire at most once per CI run |

## 6. Phase 6 — Extension Points (ATM / sc-mux)

### Functional

| ID | Requirement |
|---|---|
| FR-49 | Trigger events emitted as structured log entries with action, level, and typed fields |
| FR-50 | ATM adapter consumes trigger events and sends `atm send <agent> "CI: PR #42 — checks passed"` |
| FR-51 | sc-mux dashboard reads continuity SQLite for CI status per registered repo/session |
| FR-52 | Extension consumers have no dependency on continuity internals |

### Non-Functional

| ID | Requirement |
|---|---|
| NF-18 | ATM message delivery is fire-and-forget. Failed delivery does not block the poll loop |
| NF-19 | Dashboard reads are non-critical. A slow query does not affect `atm send` or daemon operation |

## 7. CLI Command Summary

| Command | gh calls | Description |
|---|---|---|
| `continuity daemon` | Auth at startup only | Start the poll daemon. Blocks. |
| `continuity register <owner/repo> --account <name>` | 1 (auth verify) | Add repo to tracking. Install post-push hook. |
| `continuity status` | None | Show all open PRs + current job states from SQLite. |
| `continuity log <repo> <pr#>` | None | Show all ci_events for a PR in order. |
| `continuity history <repo> [--limit N]` | None | Show closed PRs with CI outcomes and durations. |
| `continuity usage [--account <name>]` | None | Show api_usage summary per account. |
