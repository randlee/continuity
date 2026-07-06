# Continuity Sprint Plan — HTTP RPC Migration + Cleanup

**Date**: 2026-07-06
**Architecture refs**: `docs/architecture.md` §§6, 9.3, 12, 14; `docs/requirements.md` §§4.4–4.7, 8

## Overview

Six tasks in dependency order. The core architectural change is task 4 — moving
CLI commands from direct SQLite reads to daemon HTTP RPC. Every other task
either depends on or is simplified by this.

**Execution order**: 4 → 3 → 1 → 2 → 5 → 6

---

## Sprint 1 — Task 4: CLI Commands Use HTTP RPC

**Effort**: Medium
**Depends on**: nothing
**Unblocks**: Task 3 (wake), simplifies Task 5 (interceptor detection)

### Goal

All `ci status`, `ci log`, `ci history`, `ci usage` commands call the daemon's
HTTP RPC endpoints instead of reading SQLite directly. `ci poll` command added
(`POST /poll`). The CLI becomes a thin HTTP client. The daemon is the sole
SQLite writer and the sole data-access authority.

### What already exists

- **`python/httpd.py`** — Full HTTP RPC server with `/status`, `/prs`,
  `/prs/<owner>/<repo>/<num>`, `/poll` endpoints. Returns JSON with
  `{"status": "ok", ...}` wrapper. Cache-aware: stale data triggers on-demand
  GraphQL poll. Own SQLite connection, thread-safe via WAL.
- **`python/cli/daemon_cmd.py`** — CLI command implementations reading SQLite
  directly. Six functions: `cmd_status`, `cmd_log`, `cmd_history`, `cmd_usage`,
  `cmd_register`, plus ATM helpers.
- **`python/daemon.py`** — Already starts `httpd` at line 107 in `start()`.
  Writes PID file but NOT a port file yet.

### Changes

#### 1.1 Daemon: Write port file at startup

**File**: `python/daemon.py`, `start()` method (~line 107)

After `self._httpd = start_httpd(self, db_path)`, write the port to
`$CONTINUITY_HOME/daemon.port`:

```python
port_file = self.home / "daemon.port"
port_file.write_text(str(9119))
```

Clean up `daemon.port` in `_cleanup()` alongside `daemon.pid`. Add a constant
`DEFAULT_PORT = 9119` to `constants.py`.

#### 1.2 CLI: HTTP Client helper

**New file**: `python/cli/http_client.py`

Small module with:
- `_data_dir() -> Path` — platform-appropriate data directory (same logic as
  `continuity-gh`'s `_data_dir()`). Reuse from `db.py` or extract to shared
  location.
- `_read_port() -> int` — reads `$CONTINUITY_HOME/daemon.port`, returns port
- `_get(endpoint: str) -> dict` — `urllib.request.urlopen`, parse JSON
- `_post(endpoint: str) -> dict` — same for POST
- `_daemon_url(endpoint: str) -> str` — constructs `http://127.0.0.1:{port}{endpoint}`
- Error handling: connection refused → "daemon not running" message,
  timeout → "daemon not responding", JSON parse error → "invalid response"

Zero external dependencies — `urllib.request` is stdlib.

#### 1.3 CLI: Rewrite `daemon_cmd.py`

**File**: `python/cli/daemon_cmd.py`

Replace direct SQLite queries with HTTP client calls:

| Command | Old | New |
|:---|:---|:---|
| `cmd_status(db)` | Query repos + prs + jobs from SQLite | `GET /prs`, format JSON into table display |
| `cmd_log(db, owner_repo, pr_number)` | Query ci_events from SQLite | `GET /prs/<owner>/<repo>/<num>`, format events from JSON |
| `cmd_history(db, owner_repo, limit)` | Query closed PRs from SQLite | `GET /prs?closed=true&repo=<owner>/<repo>`, format from JSON |
| `cmd_usage(db, account)` | Query api_usage from SQLite | `GET /status`, extract rate_limit_remaining |
| `cmd_poll()` | (new) | `POST /poll` |
| `cmd_register(db, ...)` | Direct SQLite INSERT | Keep direct SQLite — register also installs hook, so it runs before daemon is up |

**Function signatures change**: All `cmd_*` functions lose the `db` parameter
(except `cmd_register`). They call `_read_port()` + `_get()` / `_post()`.

**Keep for now**: The old SQLite-based display/formatting logic. Do NOT delete
it — mark with `# OBSOLETE: replaced by HTTP RPC — remove after sprint 1
verification`. The HTTP client parses JSON and formats the same table output.

#### 1.4 CLI: Update entry point

**File**: The CLI entry point (likely `python/continuity` or a `__main__.py`).
Change how `cmd_status`, `cmd_log`, `cmd_history`, `cmd_usage` are called —
pass no `db` argument. Add `ci poll` subcommand routing.

#### 1.5 Daemon: Remove POST_PUSH_DELAY timer and SIGUSR1

**File**: `python/daemon.py`

Remove:
- `post_push_delay` from `DaemonConfig` (line 60)
- `_setup_signals()` method (lines 177–180)
- `_wake()` method (lines 123–135) — SIGUSR1 handler
- `_wake_event` flag (line 87)
- `_scheduled_wake_at` timestamp (line 88)
- `import signal` — keep for SIGTERM handler. Only remove `signal.SIGUSR1` registration from `_setup_signals()`. Do NOT remove the import.
- `_sleep_interruptible()` wake checking logic (lines 206–213)

Simplify:
- `_sleep_interruptible()` — just sleep, checking only `_shutdown_flag`
- `_run_loop()` — remove `self._wake_event = False` / `self._scheduled_wake_at = 0.0`
  reset before poll cycle
- `_recalculate_mode()` — remove `if self._wake_event: ...` block (lines 488–490)
- `shutdown()` — keep SIGTERM handler, that's still needed

**SIGTERM handler stays**: `signal.signal(signal.SIGTERM, self.shutdown)`.
SIGUSR1 is what goes.

#### 1.6 HTTPD: Wake daemon on POST /poll

**File**: `python/httpd.py`, `_handle_poll()` (~line 149)

After successful poll, set daemon mode to PR_CHANGED:
```python
from daemon import ActivityMode  # added at top of httpd.py
d.mode = ActivityMode.PR_CHANGED
```

This replaces the `_wake()` SIGUSR1 handler — `POST /poll` now does both:
trigger immediate poll AND enter PR_CHANGED. No separate wake path needed.

#### 1.7 HTTPD: Add `closed` query param to `/prs`

Update `_handle_prs()` to check for `?closed=true&repo=<owner>/<repo>` query
params. When `closed=true`, query closed/merged PRs instead of open.
When `repo=<owner>/<repo>`, filter by repo. Use `urllib.parse.parse_qs`.

#### 1.8 Hook: Remove SIGUSR1, use HTTP

**File**: `python/hooks.py`

Replace `HOOK_SCRIPT` constant with cross-platform curl-based script:

```sh
#!/bin/sh
# continuity post-push hook — wakes daemon on push to origin
[ "$1" = "origin" ] || exit 0
PORT=$(cat "${CONTINUITY_HOME:-$HOME/.local/share/continuity}/daemon.port" 2>/dev/null)
[ -n "$PORT" ] && curl -s -X POST "http://localhost:$PORT/poll" >/dev/null 2>&1 &
exit 0
```

Remove the old `kill -SIGUSR1` variant. Single script, works on macOS (curl
ships by default) and Linux. Windows hook is a separate `.bat` file (or
deferred — see Sprint 1.9).

#### 1.9 Windows hook script (deferred)

Windows post-push hook (`post-push.bat`) can be deferred to a follow-up.
The interceptor path (Sprint 2) covers the critical cross-platform wake
surface. Windows git hooks are uncommon in practice.

### Test Changes

**Files**: `python/tests/test_daemon.py`, `python/tests/test_httpd.py`,
`python/tests/test_cli.py`, `python/tests/test_hooks.py`

#### test_daemon.py — Remove SIGUSR1/wake tests, add port file test

Remove these tests:
- `test_sigusr1_sets_wake_event` — SIGUSR1 is gone
- `test_wake_sets_pr_changed_mode` — wake via POST /poll now
- `test_first_wins_multiple_wakes` — POST_PUSH_DELAY is gone
- `test_recalculate_mode_overridden_by_pending_wake` — `_wake_event` is gone
- `test_sleep_interruptible_wakes_at_scheduled_time` — `_scheduled_wake_at` is gone
- `test_sleep_interruptible_ignores_past_wake` — same
- `test_sleep_interruptible_no_wake_sleeps_full` — update to check `_shutdown_flag` only

Add:
- `test_port_file_written_on_startup` — verify `daemon.port` exists after `start()`
- `test_port_file_cleaned_on_shutdown` — verify `daemon.port` removed in `_cleanup()`

Update:
- `test_mode_recalculation` — remove `_wake_event` assertions
- `_make_daemon` fixture — update `DaemonConfig` to drop `post_push_delay` field
- Any mock setup that references the removed fields

#### test_httpd.py — Add POST /poll mode transition test

Add:
- `test_poll_sets_pr_changed_mode` — `POST /poll` → daemon mode becomes PR_CHANGED

#### test_cli.py — Rewrite for HTTP RPC

Old tests passed a `db` connection to `cmd_status()` etc. New tests should:
- Mock `urllib.request.urlopen` to return synthetic HTTP responses
- OR: start the actual HTTPD in a test daemon and call CLI functions against it
- Prefer mocking for unit tests, integration for `test_integration.py`

Remove tests for old SQLite-query internal helpers. Add:
- `test_status_gets_prs_from_http` — mock HTTP, verify display output
- `test_log_gets_detail_from_http` — mock HTTP, verify event timeline
- `test_cli_handles_daemon_down` — connection refused → error message
- `test_poll_command_posts_to_daemon` — verify POST behavior

#### test_hooks.py — Update for curl-based hook

Current tests (line 102) assert `SIGUSR1` in hook content. Change to assert
`curl` and `/poll`. Remove `sys.platform == "win32"` skip — one hook for all.

### Acceptance

- [ ] `ci status` returns data via HTTP RPC, same table format
- [ ] `ci log o/r 42` returns event timeline via HTTP RPC
- [ ] `ci history o/r` returns closed PRs via HTTP RPC
- [ ] `ci poll` triggers immediate poll and returns result
- [ ] `daemon.port` written at startup, cleaned at shutdown
- [ ] `POST /poll` transitions daemon to PR_CHANGED mode
- [ ] SIGUSR1 handler, `_wake`, `_wake_event`, `_scheduled_wake_at`, `post_push_delay` all removed
- [ ] Hook script uses `curl POST /poll`, no SIGUSR1 reference
- [ ] All tests pass (SIGUSR1 tests removed; port file + HTTP tests added)
- [ ] Old SQLite code in CLI marked `# OBSOLETE` but not deleted. Specific
  functions to mark: `cmd_status`, `cmd_log`, `cmd_history`, `cmd_usage`,
  `_pr_mode`, `_job_symbol`, `_activity_mode`, `_format_ts`

---

## Sprint 2 — Task 3: PR Create → Daemon Wake

**Effort**: Small
**Depends on**: Sprint 1 (needs `daemon.port` + `POST /poll`)

### Goal

When the interceptor logs a `gh pr create` event, it wakes the daemon via
`POST /poll` so the new PR immediately enters PR_CHANGED mode.

### Changes

#### 2.1 Interceptor: POST /poll after gh pr create

**File**: `python/continuity-gh`, `intercept()` function (~line 186)

After `gh.parse(args, proc.stdout, db, ...)` for `gh pr create`, add:

```python
# Wake daemon on PR create (Sprint 2)
if command == "gh" and len(args) >= 2 and args[0] == "pr" and args[1] == "create":
    _wake_daemon()
```

New helper function `_wake_daemon()`:

```python
def _wake_daemon():
    """POST /poll to daemon to enter PR_CHANGED for new PR."""
    try:
        port_file = _data_dir() / "daemon.port"
        port = int(port_file.read_text().strip())
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/poll",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # fire-and-forget — daemon may not be running
```

Zero dependencies beyond stdlib `urllib`. Fire-and-forget — failure is silent,
the daemon will pick up the new PR on its next scheduled poll anyway.

Note: `args[1] == "create"` is an exact match — not `"create" in args` which
would incorrectly match `gh pr update --title "re-create feature"`.

#### 2.2 ONLY if gh pr create

Do NOT wake on every `gh` command — only `gh pr create`. Don't wake on
`gh pr checks`, `gh pr view`, etc. Those don't create new PRs.

### Test Changes

**File**: `python/tests/test_integration.py` (or new file)

- `test_pr_create_wakes_daemon` — interceptor calls `gh pr create`, verify
  daemon receives `POST /poll` (mock the HTTPD or check daemon mode change)

### Acceptance

- [ ] `gh pr create` → interceptor POSTs to daemon `/poll`
- [ ] Other `gh` commands do NOT trigger wake
- [ ] Daemon down → fire-and-forget, no error, no crash
- [ ] Tests pass

---

## Sprint 3 — Task 1: Dead Code Cleanup

**Effort**: Small
**Depends on**: Sprint 1 (POST_PUSH_DELAY/SIGUSR1 removal creates dead code)

### Out of Scope

- **sc-mux dashboard** (§9.2): continues to read SQLite directly. This is
  intentional — the dashboard is a separate process, not part of the CLI.
  Not changed in this sprint plan.

### Goal

Remove code that Sprint 1 made dead: POST_PUSH_DELAY config, wake-event
plumbing.

### Changes

#### 3.1 `_check_monitor` — NOT in codebase

Confirmed: `_check_monitor` does NOT exist in current `daemon.py`. May have
been removed in a prior cleanup. Strike from task description.

#### 3.2 Remove unused POST_PUSH_DELAY remnants

If Sprint 1 didn't catch them, check for residual references:
- `post_push_delay` anywhere in the codebase → remove
- `_wake_event` / `_scheduled_wake_at` → remove

#### 3.3 Remove unused event types in notify.py

**File**: `python/notify.py`

`CiSlow` and `CiTimeout` are defined (lines 109, 124) and have formatters
(`_format_ci_slow`, `_format_ci_timeout`), but are not actually emitted by
the daemon. The slow/timeout detection was moved to task 5 (interceptor path).

Decision: keep the types for now since task 5 will rewire them. Add comment:
`# NOTE: CiSlow/CiTimeout are defined for task 5 (interceptor detection path).
# Do not remove — they will be wired to interceptor parse phase.`

Mark the daemon-side code that would have emitted them (if any) as OBSOLETE.

#### 3.4 Remove `import signal` if unused

After Sprint 1, only SIGTERM handler remains. Check if `signal` module is still
imported — if `_setup_signals()` was the only caller and it's gone, remove the
import.

### Test Changes

- Update `test_notify.py` — remove tests for emitter-side CiSlow/CiTimeout
  (not the formatting tests, keep those for task 5)
- Verify no test references `post_push_delay`, `_wake_event`, `_scheduled_wake_at`

### Acceptance

- [ ] No `post_push_delay`, `_wake_event`, `_scheduled_wake_at` in codebase
- [ ] CiSlow/CiTimeout types preserved with task 5 note
- [ ] Unused signal import removed (SIGTERM import is still needed)
- [ ] All tests pass

---

## Sprint 4 — Task 2: Rate Limit Cost Logging

**Effort**: Small
**Depends on**: nothing (independent)

### Goal

Log GraphQL query cost on first poll run so `LOW_WATER` can be tuned against
real data. Currently the daemon tracks `ApiUsage` but doesn't log per-query
cost in an actionable way.

### Changes

#### 4.1 Log poll cost on first run

**File**: `python/daemon.py`, `_poll_cycle()` or wherever poll results arrive

After the first successful poll (`self._first_poll` flag), log:

```python
if self._first_poll:
    self._first_poll = False
    total_cost = sum(c.rate_limit.cost for c in self.clients.values())
    logger.info("first poll cost: %d points (LOW_WATER=1000, hourly budget=5000)", total_cost)
```

This gives a one-time calibration log at daemon startup.

#### 4.2 Log per-cycle cost at DEBUG level

```python
logger.debug("poll cost: %d points, remaining: %d",
    total_cost, min(r.remaining for r in [c.rate_limit for c in self.clients.values()]))
```

#### 4.3 Review LOW_WATER

`LOW_WATER = 1000` is conservative. Typical burn is estimated at ~425/hr.
After dogfooding, tune based on actual cost logs. Document the current value
and tuning criteria in a comment on the `DaemonConfig.low_water` field.

### Test Changes

- `test_first_poll_logs_cost` — verify log message emitted on first poll
- `test_cost_logged_at_info_level` — verify log level

### Acceptance

- [ ] `logger.info("first poll cost: %d points...")` appears once on startup
- [ ] `logger.debug("poll cost: ...")` on each cycle
- [ ] LOW_WATER value and tuning criteria documented

---

## Sprint 5 — Task 5: CiSlow/CiTimeout Wired to Interceptor

**Effort**: Medium
**Depends on**: Sprint 1 (HTTP RPC in CLI), reuses `notify.py` types from Sprint 3

### Goal

Move slow/timeout detection from daemon polling to the interceptor parse phase.
When `gh pr view` or `gh pr checks` returns CI job data, the interceptor
compares elapsed time against EMA and emits `CiSlow`/`CiTimeout` events.

### Why interceptor, not daemon

- Interceptor fires on explicit user/agent action — `gh pr view --json
  statusCheckRollup` is the moment someone is looking at CI status. That's
  when a slow/failed notification is most actionable.
- Daemon polling is background — a `CiSlow` event discovered 4 minutes into
  a 5-minute poll interval is stale by the time it fires.
- Interceptor already captures `ATM_IDENTITY` — notifications route to the
  right person naturally.

### Changes

#### 5.1 EMA tracking accessible to interceptor

**File**: `python/monitor.py`

The interceptor `continuity-gh` already has a `db` connection and can read
`repos.avg_ci_duration` and `repos.max_ci_duration`. The EMA is updated by the
daemon on CI completion. The interceptor reads the current EMA value from the
`repos` table and compares against elapsed time.

#### 5.2 `gh/pr_view.py` — detect slow/timeout

**File**: `python/gh/pr_view.py`, `parse()` function

`gh pr view --json statusCheckRollup` returns an array of check runs:
```json
[
  {"name": "build", "status": "IN_PROGRESS", "startedAt": "2026-07-06T12:00:00Z"},
  {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
]
```

After extracting CI job statuses from `statusCheckRollup`:

1. For each `IN_PROGRESS` job, look up the job's start time from `ci_events`
   (the most recent `IN_PROGRESS` event for that job_name on this PR)
2. Compute `elapsed = now - start_time`
3. Read `avg_ci_duration` and `max_ci_duration` from `repos` table
4. If `elapsed > 2 * avg_ci_duration` → emit `CiSlow` event
5. If `elapsed > max_ci_duration` → emit `CiTimeout` event
6. Both events go to `notify.dispatch_notifications()` via the ATM adapter

Minimum 3 successful runs (FR-45) before thresholds apply — check
`ci_events` count for that `(repo, job_name)` with `conclusion = SUCCESS`.

**Duplication note**: `gh/pr_checks.py` does the same thing as
`gh/pr_view.py` for CI data. Abstract the slow/timeout detection into a
shared helper in `gh/__init__.py` or a new `gh/monitor_check.py`.

#### 5.3 Daemon: Remove unused detection code

**File**: `python/daemon.py`

After Sprint 3 kept `CiSlow`/`CiTimeout` types, Sprint 5 now wires them in
the interceptor. Any daemon-side slow/timeout detection code that exists
(grep for `CiSlow`/`CiTimeout` instantiation in daemon code) should now be
removed. The daemon only emits `CiCompleted`, `PrCreatedUnmergable`, etc.

#### 5.4 OBSOLETE markers

Add `# OBSOLETE: detection moved to interceptor (task 5)` above any daemon-side
slow/timeout code before deleting it. Verify interceptor path works, then
remove in cleanup commit.

### Test Changes

- `test_gh_pr_view_detects_slow` — mock ci_events with old start time, verify
  CiSlow event emitted
- `test_gh_pr_view_detects_timeout` — same for CiTimeout
- `test_threshold_requires_3_runs` — verify no alert before 3 successful runs
- Remove daemon-side slow/timeout detection tests

### Acceptance

- [ ] `gh pr view` emits CiSlow when elapsed > 2× EMA
- [ ] `gh pr view` emits CiTimeout when elapsed > max_ci_duration
- [ ] `gh pr checks` same behavior (shared code path)
- [ ] Minimum 3 successful runs before thresholds apply
- [ ] Daemon no longer has slow/timeout detection code
- [ ] Tests pass

---

## Sprint 6 — Task 6: Integration Test

**Effort**: Medium
**Depends on**: Sprints 1–5

### Goal

End-to-end test: interceptor captures identity → daemon detects conflict →
ATM notification fires.

### Test Scenario

```
1. Start daemon (temp CONTINUITY_HOME)
2. Register test repo
3. Agent creates PR (continuity-gh pr create)
   → interceptor logs to SQLite
   → interceptor POST /poll to daemon
4. Daemon polls GitHub → PR is UNKNOWN (just created)
   → daemon enters PR_CHANGED mode
5. Feed mock poll results directly (no real waits): UNKNOWN → CONFLICTING
   → daemon emits PrCreatedUnmergable event
   → ATM notification dispatched to requested_by agent
6. Verify ATM notification contains: PR number, conflict files,
   requested_by identity
```

### Changes

#### 6.1 Test file

**New/update**: `python/tests/test_integration.py`

Use mock `GhClient` to return synthetic poll results with controlled timing
(stale → fresh → CONFLICTING). Use mock `atm_notify` to verify notification
content. Use temp dirs for isolation.

```python
def test_pr_create_to_conflict_notification(temp_home, mock_gh, mock_atm):
    # 1. Start daemon with mock GhClient
    # 2. Simulate gh pr create via interceptor
    # 3. Verify daemon entered PR_CHANGED (mode check)
    # 4. Feed mock poll: UNKNOWN → CONFLICTING
    # 5. Verify atm_notify called with PrCreatedUnmergable
    # 6. Verify notification body has PR # and requested_by
```

### Acceptance

- [ ] Full lifecycle: interceptor → daemon → notification
- [ ] ATM notification includes requested_by identity
- [ ] Conflict files present in notification body
- [ ] Test is repeatable (clean temp dirs, no state leakage)
- [ ] Timeout-safe (mock clock, no real 30s waits)

---

## Post-Sprint Tasks

### Review and Cleanup

After all six sprints:
1. Remove all `# OBSOLETE` code in a dedicated cleanup PR
2. Verify `post_push_delay`, `_wake_event`, `_scheduled_wake_at`, SIGUSR1 code
   completely purged from codebase
3. Run full test suite: `python -m pytest python/tests/ -v`
4. Smoke test: `ci daemon` → `ci status` → `ci poll` → `ci log` on
   `randlee/continuity-test` repo

### Documentation Finalization

- Update `docs/architecture.md` §14 — move completed tasks to "Done" table
- Update `docs/requirements.md` §8 — same
- Update any FR IDs that shifted during implementation

---

## File Change Summary

| Sprint | Files Changed | Files Added | Files Removed |
|:---:|---|---|---|
| 1 | `daemon.py`, `hooks.py`, `httpd.py`, `cli/daemon_cmd.py`, `constants.py`, test files | `cli/http_client.py` | — |
| 2 | `continuity-gh` | — | — |
| 3 | `daemon.py`, `notify.py` (comments only) | — | — |
| 4 | `daemon.py` | — | — |
| 5 | `gh/pr_view.py`, `gh/pr_checks.py`, `gh/__init__.py` | `gh/monitor_check.py` (maybe) | daemon-side detection code |
| 6 | `test_integration.py` | — | — |
| Cleanup | Remove `# OBSOLETE` blocks | — | Marked dead code |
