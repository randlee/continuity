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

Both paths write to the same SQLite event log. Agents receive notifications
via ATM; the CLI and sc-mux dashboard read through the daemon's HTTP RPC
server (§12) or SQLite directly (sc-mux only).

## 2. Goals

- Single source of truth for "what is the CI state of this repo/PR"
- Agents never poll GitHub directly — they receive ATM notifications
  or query the daemon's HTTP RPC
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
                 │  │ (intercept) │  │     (wakes daemon on pr create)
                 │  └──────┬──────┘  │
                 │         │         │
                 │  ┌──────▼──────┐  │
                 │  │ Poll daemon │  │  ← GraphQL polling + HTTP RPC
                 │  │  + HTTPD    │  │
                 │  └──────┬──────┘  │
                 │         │         │
                 │  ┌──────▼──────┐  │
                 │  │   SQLite    │  │  ← append-only ci_events
                 │  │   + JSONL   │  │
                 │  └─────────────┘  │
                 └─────────┬─────────┘
                           │ HTTP RPC (CLI) / SQLite (sc-mux)
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

| Command | Structured data extracted | Daemon wake |
|---|---|---|
| `gh pr create` | PR number, branch, repo | Yes — `POST /poll` |
| `gh pr merge` | PR merged event | — |
| `gh pr checks` | CI job statuses | — |
| `gh pr view --json` | PR metadata + CI status rollup | — |
| `git push` | Push event (potential CI trigger) | Yes (post-push hook) |

After logging, the interceptor wakes the daemon on `gh pr create` via the
HTTP RPC `/poll` endpoint (see §12). This gives new PRs immediate
PR_CHANGED entry without waiting for the next scheduled poll.

### 5.2 Poll Daemon (Phase 3+)

A persistent daemon that polls GitHub GraphQL on an adaptive interval.

**Lifecycle:**
1. Startup: open SQLite, authenticate via `gh auth token` (tokens in memory only)
2. Loop: sleep → query all repos per account → diff against SQLite → record
   changes → re-evaluate interval
3. Shutdown: SIGTERM graceful exit. Wake: HTTP `POST /poll` (see §12).

**State diffing:** Incoming job states are compared against the latest
`ci_events` row per (repo, PR, job). A new row is inserted only when status
or conclusion differs. This prevents the event log from becoming a heartbeat.

### 5.3 SQLite Database

Single database at `~/.local/share/continuity/continuity.db`.
Override: `CONTINUITY_DB` environment variable.

WAL mode for concurrent reads (CLI) and writes (daemon/interceptor).

## 6. Activity Modes

The poll daemon operates in three modes, re-evaluated after each cycle:

| Mode | Interval | Entry | Exit |
|---|---|---|---|
| PR_CHANGED | 30s | Daemon wake (push or PR create) | `is_mergeable` ≠ UNKNOWN |
| ACTIVE | 5 min | CI job QUEUED or IN_PROGRESS | All CI jobs complete |
| INACTIVE | 20 min | No CI, no recent push | Daemon wake → PR_CHANGED |

**Daemon wake mechanism:** `POST /poll` on the HTTP RPC server (see §12)
immediately transitions the daemon to PR_CHANGED mode. The 30s poll interval
handles timing naturally — the first poll may return UNKNOWN (GitHub can
take up to ~60s to compute mergeability after a push), but the second poll
30s later catches it. No separate delay timer is needed; the mode's own
interval is the pacing mechanism.

**PR_CHANGED (30s):** The critical post-push inspection window. Entered
immediately on daemon wake from a post-push hook or PR create event. Exits
when GitHub has computed the mergeable state (no longer UNKNOWN).

**ACTIVE = 5 min (ADR-21):** The original ACTIVE=30s was too aggressive.
CI runs take minutes; 30s provides no useful new information for most of
the run. PR_CHANGED at 30s handles the critical post-push window. ACTIVE
at 5 min provides reasonable status cadence without wasting API budget.

**INACTIVE = 20 min:** No CI running, no recent push. This is not "no
open PRs" — open PRs can exist. It simply means nothing is happening
right now. A daemon wake immediately transitions to PR_CHANGED.

**CLI cache:** The HTTPD handler checks `last_synced` staleness internally.
If data is fresh (< 30s), the response returns immediately from SQLite.
If stale, the handler triggers an on-demand GraphQL poll before responding.

### Rate Limit Model

- **Primary limit:** 5,000 points/hour per authenticated user (independent
  per account)
- **LOW_WATER = 1,000 remaining:** When `rateLimit.remaining < 1000`,
  double the current mode's interval. Restore to mode default when
  remaining recovers above the threshold
- **Typical burn:** ~425 points/hour at a realistic mix of modes — well
  within budget
- **Action on first run:** Log `rateLimit.cost` from each response
  prominently to calibrate actual cost; tune LOW_WATER if needed

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
| `designated_member` | ATM team member for fallback notifications (NULL = `team-lead`) |

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

| Event | Condition | Source |
|---|---|---|
| `PrConflictDetected` | mergeable = CONFLICTING detected | Daemon poll |
| `CiStarted` | First job transitions to QUEUED or IN_PROGRESS | Daemon poll |
| `CiJobChanged` | Any job status or conclusion transition | Daemon poll |
| `CiCompleted` | All jobs in terminal state | Daemon poll |
| `CiSlow` | Elapsed > 2× avg_ci_duration (non-fatal) | Interceptor (Sprint 5) |
| `CiTimeout` | Elapsed > max_ci_duration | Interceptor (Sprint 5) |

## 9. Extension Points

### 9.1 ATM Integration

Trigger events are consumed by the ATM adapter module
(`continuity/atm.py`). The module owns ATM CLI invocation, notification
routing, and fallback logic. The rest of Continuity calls a narrow public
interface — call sites never branch on ATM availability.

**Identity model:** Continuity sends ATM messages as the `ci` team member
(a permanent member registered via `atm team member add`). The requesting
identity (agent or human who triggered the event) is included in the
message body:

```
From: ci (on behalf of rand)
Subject: PR #42 unmergable — merge conflict in 3 files
```

**Notification routing** follows a single principle: whoever can fix the
problem gets notified. If they can't be reached, `team-lead` handles it.
Agent-driven events (PR create, push, slow/timeout) route to the
requesting agent; daemon-detected events (CI completion, conflict
detection) route to `team-lead`. Full routing matrix and fallback chain in
[ADR 001](adr/001-atm-notifications.md).

**Transient failures** (socket timeout, lock contention) retry 3× with
exponential backoff (1s/2s/4s) before falling back. Permanent failures
(member not in roster) fall back immediately. Notifications are sent in
spawned tasks — retries never block the poll loop.

**Unmergable file lists:** notifications for merge conflicts include the
conflicting file paths (≤6 displayed, total count always included). This
is grounded in the GitHub merge API's 409 response body, which includes
`files` with conflict details — no diff parsing needed.

**Module no-op behavior:** if `ATM_TEAM` or `ATM_IDENTITY` is unset, every
public call returns immediately. No stubs, no fallback message generation,
no errors. `ATM_TEAM` is per-repo — set via environment variable, read at
call time.

**CLI surface:**
```
ci atm set-notify <member>    # set designated_member on repos table
ci atm set-notify --reset     # set to NULL → team-lead default
ci atm show-notify            # print current designated member
ci atm status                 # validate ATM configuration
```

### 9.2 sc-mux Dashboard

The dashboard queries continuity's SQLite directly for CI status.
Read path is non-critical — a slow dashboard load does not block
`atm send` or daemon operation.

### 9.3 Post-Push Hook

Installed by `continuity register`. Single cross-platform script — `curl`
to the daemon's HTTP RPC `/poll` endpoint. Port discovered from
`$CONTINUITY_HOME/daemon.port` (written at startup).

**Unix:**
```sh
#!/bin/sh
# .git/hooks/post-push
[ "$1" = "origin" ] || exit 0
PORT=$(cat "$CONTINUITY_HOME/daemon.port" 2>/dev/null)
curl -s -X POST "http://localhost:$PORT/poll" >/dev/null 2>&1 &
```

**Windows:**
```bat
@echo off
REM .git\hooks\post-push.bat
if /I not "%1"=="origin" exit /b 0
set /p PORT=<"%CONTINUITY_HOME%\daemon.port"
curl -s -X POST http://localhost:%PORT%/poll >nul 2>&1
```

Single wake path, cross-platform, debuggable.

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

## 11. Daemon Singleton Guarantee

Only one daemon process may run per `CONTINUITY_HOME`. Enforced at three
levels:

| Level | Mechanism | Failure mode |
|---|---|---|
| 1 | PID file (`daemon.pid`) | Fast user-facing check |
| 2 | Lock directory (`daemon.lock`) | `os.mkdir` is atomic; cross-platform |
| 3 | `CONTINUITY_HOME` isolation | Tests use temp dirs; no collision |

**Startup sequence:**
1. `mkdir $CONTINUITY_HOME/daemon.lock` (atomic, cross-platform)
2. If `EEXIST` → read `daemon.pid` → `_is_pid_alive()` → error if alive, `rmdir` + retry if stale
3. Write PID to `daemon.pid`
4. On SIGTERM: `rmdir daemon.lock`, remove `daemon.pid`, flush logs, exit 0
5. On crash: stale lock directory remains; next startup detects stale PID and recovers

**Test fixture contract:**
```python
@pytest.fixture
def daemon():
    home = tempfile.mkdtemp()
    # Start daemon, wait for ready signal (socket or health check)
    proc = subprocess.Popen(["python", "continuity", "daemon"],
                            env={"CONTINUITY_HOME": home})
    wait_for_ready(proc, timeout=5)
    yield DaemonHandle(proc.pid, home)

    # Teardown (guaranteed, in order):
    proc.terminate()           # SIGTERM
    try:
        proc.wait(timeout=5)   # graceful shutdown
    except TimeoutExpired:
        proc.kill()            # SIGKILL
        proc.wait()
    assert not is_pid_alive(proc.pid), f"PID {proc.pid} still alive"
    assert not Path(home, "daemon.pid").exists(), "PID file not cleaned"
```

## 12. HTTP RPC Daemon Interface

The daemon exposes a minimal HTTP RPC server on `localhost`. This is the
canonical interface for CLI commands and external wake events. Direct
SQLite reads from non-daemon processes are deprecated — the daemon owns
all data access.

### 12.1 Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/status` | Daemon mode, rate limits, repo count, last synced |
| `GET` | `/prs` | All open PRs with current CI job states |
| `GET` | `/prs/<owner>/<repo>/<num>` | Single PR detail + ci_events log |
| `POST` | `/poll` | Trigger immediate poll cycle, return fresh data |

### 12.2 Wake-on-Create

The interceptor calls `POST /poll` after logging a `gh pr create` event.
This immediately transitions the daemon to PR_CHANGED mode so the new PR
gets 30s polling without waiting for the next scheduled cycle.

### 12.3 Port Discovery

The daemon writes its port to `$CONTINUITY_HOME/daemon.port` at startup.
CLI commands and hooks read this file to discover the endpoint.

### 12.4 CLI as Thin Client

CLI commands (`ci status`, `ci log`, `ci history`) are thin HTTP clients
that call the daemon's RPC endpoints. They do not read SQLite directly.
The daemon owns cache staleness logic: stale data triggers an on-demand
poll; fresh data returns immediately.

**Design rationale:**
- **Single writer**: Only the daemon writes to SQLite. Eliminates WAL
  contention and stale-read edge cases.
- **Cross-platform**: HTTP works everywhere. No signal imports, no
  platform-specific hooks.
- **Debuggable**: `POST /poll` returns JSON with poll outcome and
  rate-limit info.
- **Unified wake**: Interceptor, post-push hook, and manual `ci poll`
  all use the same `POST /poll` endpoint.

## 13. Key Design Decisions

| Decision | Rationale |
|---|---|
| CLI interception before polling | Catches agent actions immediately. No polling infrastructure needed for day-one value. |
| Append-only ci_events | Immutable audit log. Current state is derivable. No UPDATE logic. |
| GraphQL batch query per account | One API call covers all repos and all PRs. O(accounts), not O(repos). |
| Persistent daemon | Holds tokens in memory. Eliminates per-poll auth subprocess overhead. |
| Adaptive poll interval | Conserves API tokens. Three modes: PR_CHANGED (30s post-push), ACTIVE (5 min CI running), INACTIVE (20 min no activity) |
| ACTIVE=5 min (ADR-21) | 30s was too aggressive for CI monitoring. PR_CHANGED handles the critical post-push window at 30s. ACTIVE at 5 min provides reasonable cadence |
| SQLite (not Postgres/MySQL) | Single binary, no server. WAL mode for concurrent reads. |
| CLI as HTTP thin client | All CLI commands read via daemon HTTP RPC. Daemon owns data access and cache staleness. Cross-platform, debuggable, single-writer. Replaces direct SQLite reads. |
| Wake via `POST /poll` | Single wake path for hook, interceptor, and manual `ci poll`. HTTP works everywhere, returns acknowledgement. No SIGUSR1, no platform branches. |
| Three-source architecture | Interception + polling + structured log. Each adds data without replacing the others. |

## 14. Remaining Implementation Tasks

Prioritized. Each task includes obsolescence markers: code that should be
flagged for removal when replaced.

| # | Task | Effort | Makes Obsolete |
|:---:|:---|:---:|:---|
| 1 | Dead code cleanup — `_check_monitor` (~460 lines in `daemon.py`), `CiSlow`/`CiTimeout` event types in `notify.py` | Small | Remove `_check_monitor` and unused event types |
| 2 | Rate limit cost logging — `logger.info("poll cost: %d points", rl.cost)` on first run, tune `LOW_WATER` | Small | — |
| 3 | PR create → daemon wake — interceptor sends `POST /poll` on `gh pr create`. (Requires task 4; becomes a one-liner once HTTP RPC is the CLI path.) | Small | — |
| 4 | CLI commands use HTTP — `ci status` → `GET /prs`, `ci log` → `GET /prs/:repo/:num`. Daemon owns data; CLI is thin client. | Medium | Deprecate direct SQLite reads from CLI |
| 5 | `CiSlow`/`CiTimeout` wired to interceptor — move detection from daemon poll to `gh pr view`/`gh pr checks` interceptor path | Medium | Remove daemon-side slow/timeout detection |
| 6 | Integration test — end-to-end: interceptor captures identity → daemon detects conflict → ATM notification fires | Medium | — |

### Obsolescence Convention

When code is replaced by a new implementation:
1. Add `# OBSOLETE: replaced by <mechanism> — remove after <task #> verified` above the old code.
2. Do NOT delete the old code in the same commit that adds its replacement — let tests validate the new path first.
3. Once the new path is verified (tests pass, CI green), remove the old code in a dedicated cleanup commit.
4. Update this section: move the completed task to a "Done" table and drop its obsolescence marker.
