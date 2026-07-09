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

A persistent daemon that polls GitHub GraphQL on per-repo adaptive intervals.

**Lifecycle:**
1. Startup: open SQLite connections (see §5.4), authenticate via `gh auth token`
   (tokens in memory only). Start HTTP RPC server.
2. Loop: compute `sleep = min(all_repo_next_poll_at) - now`. Sleep until
   `sleep` elapses or shutdown. Wake and poll ONLY the repos whose timers
   have expired. Diff against SQLite. Write changes. Recalculate each
   polled repo's timer independently.
3. Shutdown: SIGTERM graceful exit. Wake: HTTP `POST /poll` for a specific
   repo (see §12) resets that repo's timer to `now`.

**Per-repo timer model** (§6): Each `owner/repo` owns its interval,
recalculated independently after each poll. A push to `foo` resets foo's
timer to 30s without affecting `bar`'s 20 min interval. The sleep loop
picks the minimum; only due repos are polled.

**State diffing:** Incoming job states are compared against the latest
`ci_events` row per (repo, PR, job). A new row is inserted only when status
or conclusion differs. This prevents the event log from becoming a heartbeat.

### 5.3 SQLite Database

Single database at `~/.local/share/continuity/continuity.db`.
Override: `CONTINUITY_DB` environment variable.

**Write model:** Only the daemon's poll thread writes to SQLite. Every other
component (HTTPD handlers, notification dispatch threads, interceptor
processes) opens its own read-only connection in WAL mode. No
`check_same_thread=False` — each thread owns its connection. Write
serialization is implicit: only the poll thread writes, so no lock is needed
for SQLite access.

**Connection lifecycle:**
- **Writer connection:** Owned by the daemon's poll thread. Created at
  startup via `ensure_db()`. Never shared.
- **HTTPD read connections:** Created per-request by the HTTP handler
  (short-lived). Reads via WAL are non-blocking.
- **Interceptor writes:** The interceptor is a separate process with its
  own connection. WAL mode handles concurrent process access natively —
  no coordination needed between daemon and interceptor.
- **Notification thread:** Opens its own read connection. The daemon
  commits before dispatching notifications — the notification thread
  always sees committed state.

**WAL mode** for concurrent reads across threads and processes. SQLite
handles reader-writer concurrency natively in WAL mode: readers see a
consistent snapshot from the start of their transaction, writers append
to the WAL without blocking readers.

## 6. Per-Repo Timer Model

The daemon uses per-repo timers instead of a single global polling mode.
Each `owner/repo` tracks its own `next_poll_at` timestamp and re-evaluates
its interval independently after every poll.

### 6.1 Why Per-Repo

The old global mode model (PR_CHANGED/ACTIVE/INACTIVE) applied a single
interval to ALL repos. A push to `randlee/foo` forced every repo under
every account to poll at 30s. This burned API tokens on repos that hadn't
changed. Per-repo timers isolate the blast radius: a push to `foo` resets
only `foo`'s timer; `rand-lee/bar` continues at its 20 min interval.

### 6.2 Timer States

Each repo's timer is recalculated after its poll cycle based on the repo's
current state:

| State | Interval | Condition |
|---|---|---|
| PR_CHANGED | 30s | Any PR has `mergeable = UNKNOWN` (post-push, still computing) |
| ACTIVE | 5 min | Any PR has a CI job in QUEUED or IN_PROGRESS |
| INACTIVE | 20 min | No UNKNOWN mergeable, no active CI (open PRs may exist) |

A repo in ACTIVE state with CI running AND UNKNOWN mergeable (push
landed while CI was running) stays at 30s (PR_CHANGED wins — shortest
interval always applies).

### 6.3 Timer Reset

A repo's timer is reset to `now` (immediate poll) when:

1. **Interceptor detects `gh pr create`** → `POST /poll?repo=owner/repo`
2. **Post-push hook fires** → `POST /poll?repo=owner/repo`
3. **Manual `ci poll <repo>`** → `POST /poll?repo=owner/repo`

Resetting to `now` means the daemon's next sleep boundary polls that repo
immediately. The repo then runs its first poll at PR_CHANGED (30s for
subsequent polls) to handle the mergeability computation window.

### 6.4 Poll Loop Algorithm

```python
class Daemon:
    _timers: dict[str, float]  # owner_repo → next_poll_at (unix timestamp)

    def _run_loop(self):
        while not self._shutdown_flag:
            now = time.time()
            due = [repo for repo, next_at in self._timers.items()
                   if now >= next_at]

            if not due:
                sleep = min(self._timers.values()) - now
                time.sleep(max(sleep, 0.5))
                continue

            for repo in due:
                self._poll_repo(repo)
                self._recalculate_timer(repo)
```

**Key properties:**
- Only repos whose timers have expired are polled. Polling one repo does
  not poll other repos under the same account — each repo gets its own
  GraphQL query or is batched efficiently with other due repos under the
  same account.
- `min(self._timers.values())` is O(n) but n is small (dozens of repos,
  not thousands). Premature optimization.
- The sleep checks the shutdown flag every 0.5s for responsiveness.

### 6.5 Account Batching

When multiple repos under the same account are due simultaneously, they
are batched into a single GraphQL query (as today). The batching is
opportunistic — repos under different accounts are polled sequentially.

```python
def _poll_cycle(self):
    now = time.time()
    due = [r for r, t in self._timers.items() if now >= t]
    by_account = group_by_account(due)

    for account, repos in by_account.items():
        result = self.clients[account].poll(repos)
        for owner_repo in repos:
            self._apply_result_for_repo(owner_repo, result)
            self._recalculate_timer(owner_repo)
```

### 6.6 Rate Limit Per Account

Rate limit tracking is per-account, not per-repo. When
`rateLimit.remaining < LOW_WATER` for an account, ALL repos under that
account double their interval regardless of their individual state.
When remaining recovers, each repo returns to its state-determined
interval.

| Condition | Effect |
|---|---|
| `remaining < LOW_WATER` (1,000) | All repos under account: interval × 2 |
| `remaining < LOW_WATER / 2` | All repos under account: interval × 4 |
| `remaining` recovers above threshold | Restore to per-repo state interval |

### 6.7 Timer Persistence

Timers are NOT persisted across daemon restarts. On startup, all repos
default to PR_CHANGED (30s) for the first poll, then recalculate after
that poll. This is safe: a restart means the daemon was down and may have
missed events — aggressive initial polling is the conservative choice.

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

Installed by `continuity register`. Single cross-platform script —
`curl` to the daemon's HTTP RPC `/poll` endpoint with `?repo=` query
param targeting the specific repo. Port discovered from
`$CONTINUITY_HOME/daemon.port` (written at startup).

**Unix:**
```sh
#!/bin/sh
# .git/hooks/post-push
[ "$1" = "origin" ] || exit 0
PORT=$(cat "$CONTINUITY_HOME/daemon.port" 2>/dev/null)
REPO=$(git remote get-url origin | sed 's|.*[:/]\([^/]*/[^/]*\)\.git|\1|')
curl -s -X POST "http://localhost:$PORT/poll?repo=$REPO" >/dev/null 2>&1 &
```

**Windows:**
```bat
@echo off
REM .git\hooks\post-push.bat
if /I not "%1"=="origin" exit /b 0
set /p PORT=<"%CONTINUITY_HOME%\daemon.port"
curl -s -X POST http://localhost:%PORT%/poll >nul 2>&1
```

Single wake path, per-repo targeting, cross-platform, debuggable.

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
| `GET` | `/status` | Daemon state, rate limits, repo count, timers |
| `GET` | `/prs` | All open PRs with current CI job states |
| `GET` | `/prs/<owner>/<repo>/<num>` | Single PR detail + ci_events log |
| `POST` | `/poll` | Trigger immediate poll for a specific repo |
| `POST` | `/poll?repo=owner/repo` | Reset specific repo's timer to now |

### 12.2 Per-Repo Wake

The `POST /poll` endpoint accepts an optional `?repo=owner/repo` query
parameter. When provided, only that repo's timer is reset to `now` —
the daemon polls it on the next sleep boundary. When omitted (`POST /poll`
with no query param), all repos are polled (useful for manual `ci poll`).

**Wake sources:**
- **Interceptor (gh pr create):** `POST /poll?repo=owner/repo` → resets
  only the new PR's repo
- **Post-push hook:** `POST /poll?repo=owner/repo` → resets only the
  pushed repo (see §9.3 for repo detection in hook)
- **Manual `ci poll`:** `POST /poll` (no repo) → polls all repos

### 12.3 Port Discovery

The daemon writes its port to `$CONTINUITY_HOME/daemon.port` at startup.
CLI commands and hooks read this file to discover the endpoint.

### 12.4 CLI as Thin Client

CLI commands (`ci status`, `ci log`, `ci history`) are thin HTTP clients
that call the daemon's RPC endpoints. They do not read SQLite directly.
The daemon owns cache staleness logic: stale data triggers an on-demand
poll; fresh data returns immediately.

**Design rationale:**
- **Single writer**: Only the daemon's poll thread writes to SQLite.
  Eliminates WAL contention and stale-read edge cases.
- **Cross-platform**: HTTP works everywhere. No signal imports, no
  platform-specific hooks.
- **Debuggable**: `POST /poll` returns JSON with poll outcome and
  rate-limit info.
- **Unified wake**: Interceptor, post-push hook, and manual `ci poll`
  all use the same `POST /poll` endpoint with optional repo targeting.
- **Isolated blast radius**: A push to `foo` resets only `foo`'s timer.
  Repos under idle accounts are unaffected.

## 13. Key Design Decisions

| Decision | Rationale |
|---|---|
| CLI interception before polling | Catches agent actions immediately. No polling infrastructure needed for day-one value. |
| Append-only ci_events | Immutable audit log. Current state is derivable. No UPDATE logic. |
| GraphQL batch query per account | One API call covers all repos and all PRs. O(accounts), not O(repos). |
| Persistent daemon | Holds tokens in memory. Eliminates per-poll auth subprocess overhead. |
| Per-repo adaptive poll intervals | Each repo owns its timer, independently recalculated. Isolates blast radius — a push to `foo` doesn't accelerate `bar`'s polling. Replaces global mode model (ADR-22). |
| ACTIVE=5 min (ADR-21) | 30s was too aggressive for CI monitoring. PR_CHANGED handles the critical post-push window at 30s. ACTIVE at 5 min provides reasonable cadence. |
| SQLite (not Postgres/MySQL) | Single binary, no server. WAL mode for concurrent reads. Single writer thread — no lock needed. |
| CLI as HTTP thin client | All CLI commands read via daemon HTTP RPC. Daemon owns data access and cache staleness. Cross-platform, debuggable, single-writer. Replaces direct SQLite reads. |
| Per-repo wake via `POST /poll?repo=` | Single wake path for hook, interceptor, and manual `ci poll`. HTTP works everywhere, returns acknowledgement. No SIGUSR1, no platform branches. Isolated to target repo. |
| Single writer thread | Only the daemon's poll thread writes to SQLite. Notification, HTTPD, and interceptor threads open their own read connections. Eliminates `check_same_thread=False` hack. |
| Three-source architecture | Interception + polling + structured log. Each adds data without replacing the others. |

## 14. Rust Migration Path

The Python POC is structured with module boundaries that map directly to
Rust traits and crates under `sc-runtime`.

### 14.1 Module → Trait Mapping

| Python Module | Rust Equivalent | sc-runtime Integration |
|---|---|---|
| `gh/client.py` | `GhClient` trait | sc-runtime HTTP client + retry |
| `diff.py` | Pure functions, `fn diff_jobs(...) -> Vec<CiEvent>` | No runtime dependency (pure) |
| `daemon.py` | `PollOrchestrator` struct | sc-runtime shutdown, SQLite pool, config |
| `httpd.py` | `axum::Router` or `warp::Filter` | sc-runtime shutdown integration |
| `notify.py` | `Notify` trait | Optional: sc-runtime spawn for async dispatch |
| `atm.py` | `AtmNotifier` impl `Notify` trait | — |
| `db.py` | `rusqlite::Connection` + migrations | sc-runtime connection pool |
| `monitor.py` | `EmaTracker` struct | No runtime dependency |
| `constants.py` | `const` module | — |
| `interceptor` | Standalone binary (same pattern) | — |

### 14.2 Per-Repo Timer in Rust

The per-repo timer model maps cleanly to `tokio`:

```rust
use std::collections::HashMap;
use tokio::time::{sleep_until, Instant};

struct PollScheduler {
    timers: HashMap<String, Instant>,  // owner_repo → next poll time
    shutdown: watch::Receiver<bool>,   // from sc-runtime
}

impl PollScheduler {
    async fn run(&mut self) {
        loop {
            tokio::select! {
                _ = shutdown.changed() => break,
                _ = self.sleep_until_next() => {},
            }
            self.poll_due_repos().await;
        }
    }

    async fn sleep_until_next(&self) {
        if let Some(next) = self.timers.values().min() {
            sleep_until(*next).await;
        }
    }

    fn reset_timer(&mut self, repo: &str) {
        self.timers.insert(repo.into(), Instant::now());
    }
}
```

### 14.3 SQLite Connection Model in Rust

```rust
use rusqlite::Connection;
use std::sync::{Arc, Mutex};

// Writer: owned exclusively by the poll thread
let writer: Connection = Connection::open(db_path)?;
writer.execute_batch("PRAGMA journal_mode=WAL")?;

// Reader pool: per-thread connections, created on demand
fn open_reader() -> Connection {
    let conn = Connection::open(db_path).unwrap();
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA query_only=ON").unwrap();
    conn
}

// Alternatively: Mutex<Connection> if single-writer-thread is guaranteed.
// rusqlite::Connection is Send but not Sync — move, don't share.
```

**Key constraint:** `rusqlite::Connection` is `Send` but not `Sync`.
You can move it to another thread, but you can't share a `&Connection`
across threads. The Python POC's single-writer-thread model avoids this
entirely: the writer connection stays in the poll thread forever. Readers
open their own connections. This is the simplest correct model and ports
directly to Rust.

### 14.4 Migration Order

| Order | Component | Rationale |
|---|---|---|
| 1 | `sc-runtime` (db, shutdown, retry, config) | Foundation — everything depends on it |
| 2 | `diff` + `constants` | Pure, no runtime deps, easy to test |
| 3 | `gh/client` | Depends on sc-runtime HTTP + retry |
| 4 | `db` (schema, migrations) | Depends on sc-runtime connection pool |
| 5 | `daemon` (poll orchestrator) | Depends on all of the above |
| 6 | `httpd` (axum/warp) | Depends on daemon for state |
| 7 | `notify` + `atm` (traits) | Depends on daemon for events; traits for pluggability |
| 8 | `monitor` (EMA) | Pure, last |
| 9 | `interceptor` (standalone binary) | Last — same pattern as Python POC |

## 15. Remaining Implementation Tasks

Prioritized. Each task includes obsolescence markers: code that should be
flagged for removal when replaced.

| # | Task | Effort | Makes Obsolete |
|:---:|:---|:---:|:---|
| 0 | **Per-repo timer model** — replace global `ActivityMode` / `_poll_cycle` with per-repo `_timers` dict + `_recalculate_timer()`. Wire `POST /poll?repo=` to reset specific repo. Update `httpd.py` to accept `?repo=` query param. | Large | Global `ActivityMode`, `_recalculate_mode()`, `_next_interval()`, old `POST /poll` (no-repo) semantics |
| 1 | **SQLite write safety** — remove `check_same_thread=False`. Open per-thread read connections. Ensure notification thread opens its own connection. | Medium | `check_same_thread=False` hack, shared `self.db` across threads |
| 2 | Rate limit cost logging — `logger.info("poll cost: %d points", rl.cost)` on first run, tune `LOW_WATER` | Small | — |
| 3 | Dead code cleanup — remove global mode `_recalculate_mode()`, `_next_interval()`, `ActivityMode` enum, `_poll_lock` | Small | Global mode infrastructure (post task 0 verification) |
| 4 | CLI commands use HTTP — `ci status` → `GET /prs`, `ci log` → `GET /prs/:repo/:num`. Daemon owns data; CLI is thin client. | Medium | Direct SQLite reads from CLI |
| 5 | `CiSlow`/`CiTimeout` wired to interceptor — move detection from daemon poll to `gh pr view`/`gh pr checks` interceptor path | Medium | Daemon-side slow/timeout detection |
| 6 | Integration test — end-to-end: interceptor captures identity → daemon detects conflict → ATM notification fires | Medium | — |
| 7 | Per-repo timer persistence / startup recovery — on restart, all repos default to 30s, then recalculate | Small | Startup race window (already minimal) |

### Obsolescence Convention

When code is replaced by a new implementation:
1. Add `# OBSOLETE: replaced by <mechanism> — remove after task <#> verified` above the old code.
2. Do NOT delete the old code in the same commit that adds its replacement — let tests validate the new path first.
3. Once the new path is verified (tests pass, CI green), remove the old code in a dedicated cleanup commit.
4. Update this section: move the completed task to a "Done" table and drop its obsolescence marker.
