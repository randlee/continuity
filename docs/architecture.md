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

| Mode | Interval | Entry | Exit |
|---|---|---|---|
| PR_CHANGED | 30s | SIGUSR1 (push or PR create) | `is_mergeable` ≠ UNKNOWN |
| ACTIVE | 5 min | CI job QUEUED or IN_PROGRESS | All CI jobs complete |
| INACTIVE | 20 min | No CI, no recent push | SIGUSR1 → PR_CHANGED |

**PR_CHANGED (30s):** The critical post-push inspection window. Entered
immediately on SIGUSR1 from a post-push hook or PR create event. Exits
when GitHub has computed the mergeable state (no longer UNKNOWN). This
replaces the previous ACTIVE=30s mode — the old 30s interval was too
aggressive for CI monitoring (minutes-long runs) but too slow for the
post-push mergeability check (seconds matter).

**POST_PUSH_DELAY = 1 min (ADR-20):** On SIGUSR1, the daemon does not
poll immediately. It schedules the first PR_CHANGED poll at `now + 1 min`.
GitHub takes approximately 1 minute to compute `is_mergeable` after a
push. Polling immediately returns UNKNOWN reliably. The 1-minute delay
gives high confidence of getting the computed state on the first try.
Multiple SIGUSR1 within the delay window: first-wins — subsequent signals
do not push the scheduled poll further out.

**ACTIVE = 5 min (ADR-21):** The original ACTIVE=30s was too aggressive.
CI runs take minutes; 30s provides no useful new information for most of
the run. PR_CHANGED at 30s handles the critical post-push window. ACTIVE
at 5 min provides reasonable status cadence without wasting API budget.

**INACTIVE = 20 min:** No CI running, no recent push. This is not "no
open PRs" — open PRs can exist. It simply means nothing is happening
right now. SIGUSR1 from a post-push hook immediately transitions to
PR_CHANGED.

**CLI cache:** If `last_polled_at` is within 30s of now, `continuity
status` and similar read commands return SQLite state without an API
call. If ≥ 30s, execute a GraphQL query and reset the poll timer.

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
Agent-driven events (PR create, push) route to the requesting agent;
daemon-detected events (CI completion, slow, timeout) and cascades always
route to `team-lead`. Full routing matrix and fallback chain in
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

## 12. Key Design Decisions

| Decision | Rationale |
|---|---|
| CLI interception before polling | Catches agent actions immediately. No polling infrastructure needed for day-one value. |
| Append-only ci_events | Immutable audit log. Current state is derivable. No UPDATE logic. |
| GraphQL batch query per account | One API call covers all repos and all PRs. O(accounts), not O(repos). |
| Persistent daemon | Holds tokens in memory. Eliminates per-poll auth subprocess overhead. |
| Adaptive poll interval | Conserves API tokens. Three modes: PR_CHANGED (30s post-push), ACTIVE (5 min CI running), INACTIVE (20 min no activity) |
| POST_PUSH_DELAY (ADR-20) | GitHub takes ~1 min to compute is_mergeable. Delay first poll after push to avoid UNKNOWN. First-wins: multiple signals within window don't push further |
| ACTIVE=5 min (ADR-21) | 30s was too aggressive for CI monitoring. PR_CHANGED handles the critical post-push window at 30s. ACTIVE at 5 min provides reasonable cadence |
| SQLite (not Postgres/MySQL) | Single binary, no server. WAL mode for concurrent reads. |
| CLI reads SQLite only | Status is always instant. No API calls on the read path. |
| Three-source architecture | Interception + polling + structured log. Each adds data without replacing the others. |
