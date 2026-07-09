# Continuity Sprint Plan — Per-Repo Timer Model

**Date**: 2026-07-08
**Branch**: `sc-git-worktree`
**Architecture refs**: `docs/architecture.md` §§5.2, 5.3, 6, 12, 14; `docs/requirements.md` §§4.3, 4.6, 4.7, 4.8

## Overview

Replace the global `ActivityMode` poll loop with per-repo timers. Each
`owner/repo` gets its own `next_poll_at` timestamp; the daemon polls only
repos whose timers have expired. Wake events (`POST /poll?repo=`) reset
only the target repo's timer. SQLite write safety is fixed by giving each
component its own connection.

**Execution order**: 0 → 1 → 3 → 4 → 5 → 6 → 7

Task 0 (per-repo timers) is the core change. Task 1 (SQLite safety) is
independent but should follow immediately. Task 3 (dead code cleanup)
happens after both are verified.

---

## Sprint 0 — Task 0: Per-Repo Timer Model

**Effort**: Large
**Depends on**: nothing
**Unblocks**: Tasks 1, 3, 4, 5, 6

### Goal

Replace the global `ActivityMode` enum, `_recalculate_mode()`, and
`_next_interval()` with per-repo timer state. The daemon tracks
`_timers: dict[str, float]` mapping `owner_repo → next_poll_at`.
Only repos whose timers have expired are polled.

### What already exists

- **`python/daemon.py`** — `Daemon` class with `_run_loop()`, `_poll_cycle()`,
  `_recalculate_mode()`, `_next_interval()`, `ActivityMode` enum, `_poll_lock`
- **`python/httpd.py`** — `POST /poll` handler sets `d.mode = ActivityMode.PR_CHANGED`
- **`python/gh/client.py`** — `GhClient.poll(repos)` takes a list of repos
- **`python/constants.py`** — interval constants, status constants

### Changes

#### 0.1 `daemon.py` — Replace ActivityMode with per-repo timer state

**Remove:**
- `ActivityMode` enum (lines 52–55)
- `self.mode` field (line 88)
- `DaemonConfig.pr_changed_interval`, `active_interval`, `inactive_interval` (lines 60–62) — keep config for timer values, but rename

**Add:**
```python
# Timer state: owner_repo → next_poll_at (unix timestamp)
self._timers: dict[str, float] = {}

# Timer intervals (seconds)
PR_CHANGED_INTERVAL = 30
ACTIVE_INTERVAL = 300      # 5 min
INACTIVE_INTERVAL = 1200   # 20 min
```

#### 0.2 `daemon.py` — Replace `_run_loop()` with per-repo poll loop

**Remove:** `_run_loop()` (lines 178–189), `_sleep_interruptible()` (lines 191–197)

**Replace with:**
```python
def _run_loop(self):
    """Main poll loop. Sleeps until next timer expiry, polls only due repos."""
    # Initialize all repo timers to now (first poll is immediate)
    self._init_timers()

    while not self._shutdown_flag:
        now = time.time()
        due = [repo for repo, next_at in self._timers.items()
               if now >= next_at]

        if not due:
            if not self._timers:
                # No repos registered yet — sleep and retry
                time.sleep(self.INACTIVE_INTERVAL)
                continue
            sleep = min(self._timers.values()) - now
            if sleep > 0:
                self._sleep_until_shutdown(sleep)
            continue

        # Group due repos by account for batched GraphQL queries
        by_account = self._group_by_account(due)
        for account, repos in by_account.items():
            if account not in self.clients:
                continue
            try:
                result = self.clients[account].poll(repos)
            except Exception:
                logger.exception("poll failed for account %s", account)
                # Back off failed repos: set next poll to now + 5 min
                for repo in repos:
                    self._timers[repo] = now + 300
                continue

            notify_events = []
            for owner_repo in repos:
                events = self._apply_result_for_repo(owner_repo, result, now)
                notify_events.extend(events)

            self.db.commit()

            if notify_events:
                dispatch_notifications(self.db, notify_events)

        self._shutdown_check()
```

#### 0.3 `daemon.py` — Add timer helper methods

```python
def _init_timers(self):
    """Set all registered repos to poll immediately on startup."""
    self._timers.clear()
    rows = self.db.execute("SELECT owner_repo FROM repos").fetchall()
    now = time.time()
    for (owner_repo,) in rows:
        self._timers[owner_repo] = now

def _recalculate_timer(self, owner_repo: str):
    """Recalculate next_poll_at for a repo based on its current state."""
    now = time.time()
    interval = self._calc_repo_interval(owner_repo)
    self._timers[owner_repo] = now + interval

def _calc_repo_interval(self, owner_repo: str) -> int:
    """Determine poll interval for a repo based on its state."""
    # Check for UNKNOWN mergeable (PR_CHANGED → 30s)
    unknown_count = self.db.execute(
        "SELECT COUNT(*) FROM pull_requests "
        "WHERE owner_repo = ? AND mergeable = ? AND state = ?",
        (owner_repo, MERGEABLE_UNKNOWN, PR_STATE_OPEN),
    ).fetchone()[0]
    if unknown_count > 0:
        return self.PR_CHANGED_INTERVAL

    # Check for active CI (ACTIVE → 5 min)
    rows = self.db.execute(
        "SELECT status FROM ci_events "
        "WHERE owner_repo = ? "
        "GROUP BY pr_number, job_name "
        "HAVING recorded_at = MAX(recorded_at)",
        (owner_repo,),
    ).fetchall()
    has_active_ci = any(status in ACTIVE_STATUSES for (status,) in rows)
    if has_active_ci:
        return self.ACTIVE_INTERVAL

    return self.INACTIVE_INTERVAL

def _group_by_account(self, repos: list[str]) -> dict[str, list[str]]:
    """Group repos by their gh_account."""
    by_account: dict[str, list[str]] = {}
    for repo in repos:
        row = self.db.execute(
            "SELECT gh_account FROM repos WHERE owner_repo = ?", (repo,)
        ).fetchone()
        if row:
            by_account.setdefault(row[0], []).append(repo)
    return by_account

def _sleep_until_shutdown(self, seconds: float):
    """Sleep for up to seconds, checking shutdown flag every 0.5s."""
    deadline = time.time() + seconds
    while time.time() < deadline and not self._shutdown_flag:
        time.sleep(min(deadline - time.time(), 0.5))

def _shutdown_check(self):
    """Check shutdown flag between account batches."""
    pass  # _run_loop checks at top of while
```

#### 0.4 `daemon.py` — Replace `_poll_cycle()` with `_apply_result_for_repo()`

**Remove:** `_poll_cycle()` (lines 207–229), `_get_repos()` (lines 231–236)

**Refactor** `_apply_result()` into `_apply_result_for_repo()` that takes a
single repo and extracts only that repo's data from the poll result:

```python
def _apply_result_for_repo(
    self, owner_repo: str, result, now: int
) -> list[NotificationEvent]:
    """Diff poll result for a single repo, write events, return notifications."""
    notify_events: list[NotificationEvent] = []

    prs = result.repos.get(owner_repo, [])
    if not prs:
        # No PRs in result — still update last_synced and recalculate
        self._recalculate_timer(owner_repo)
        return notify_events

    current_jobs = self._load_current_jobs(owner_repo)
    current_prs = self._load_current_prs(owner_repo)
    merged_prs: list[int] = []

    # Diff jobs (same logic as current _apply_result, scoped to one repo)
    incoming_jobs = [(owner_repo, pr.number, pr.checks) for pr in prs]
    events = diff_jobs(incoming_jobs, current_jobs)
    for e in events:
        self.db.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, "
            "status, conclusion, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (e.owner_repo, e.pr_number, e.job_name, e.status,
             e.conclusion, now),
        )
        if e.status == STATUS_COMPLETED:
            _add_ci_completion(notify_events, owner_repo, e.pr_number,
                               self.db, current_jobs)
            if e.conclusion == CONCLUSION_SUCCESS:
                self._update_ema(owner_repo, e.job_name, now)

    # Diff PRs (same logic, scoped)
    pr_diff = diff_prs(prs, current_prs)
    for pr in pr_diff.added:
        self.db.execute(
            "INSERT OR REPLACE INTO pull_requests "
            "(owner_repo, pr_number, branch, state, mergeable, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (owner_repo, pr.number, "", pr.state,
             pr.mergeable or MERGEABLE_UNKNOWN, now),
        )
        if pr.mergeable == MERGEABLE_CONFLICTING:
            identity = resolve_pr_identity(self.db, owner_repo, pr.number)
            if identity is None:
                identity = resolve_push_identity(self.db, owner_repo)
            notify_events.append(PrCreatedUnmergable(
                owner_repo, pr.number, identity, [],
            ))

    for pr in pr_diff.updated:
        self.db.execute(
            "UPDATE pull_requests SET state=?, mergeable=?, updated_at=? "
            "WHERE owner_repo=? AND pr_number=?",
            (pr.state, pr.mergeable or MERGEABLE_UNKNOWN, now,
             owner_repo, pr.number),
        )
        prev = current_prs.get(pr.number)
        if pr.state in (PR_STATE_MERGED, PR_STATE_CLOSED) and prev and prev.state == PR_STATE_OPEN:
            merged_prs.append(pr.number)
        if (prev and prev.mergeable != MERGEABLE_CONFLICTING
                and pr.mergeable == MERGEABLE_CONFLICTING
                and prev.state == PR_STATE_OPEN):
            identity = resolve_push_identity(self.db, owner_repo)
            if identity is None:
                identity = resolve_pr_identity(self.db, owner_repo, pr.number)
            notify_events.append(PrBecameUnmergable(
                owner_repo, pr.number, identity,
            ))

    for pr in pr_diff.closed:
        self.db.execute(
            "UPDATE pull_requests SET state = ?, updated_at=? "
            "WHERE owner_repo=? AND pr_number=?",
            (PR_STATE_CLOSED, now, owner_repo, pr.number),
        )

    # Conflict detection
    conflicts = diff_conflicts(owner_repo, prs, current_prs)
    for repo_name, pr_num in conflicts:
        self.db.execute(
            "INSERT INTO ci_events (owner_repo, pr_number, job_name, "
            "status, conclusion, recorded_at) "
            "VALUES (?, ?, 'merge', 'COMPLETED', 'CONFLICT', ?)",
            (repo_name, pr_num, now),
        )

    # Update last_synced
    self.db.execute(
        "UPDATE repos SET last_synced = ? WHERE owner_repo = ?",
        (now, owner_repo),
    )

    # Cascade detection
    if merged_prs:
        for repo_name, pr_num in conflicts:
            for merged_num in merged_prs:
                if pr_num != merged_num:
                    notify_events.append(CascadeUnmergable(
                        repo_name, pr_num, merged_num,
                    ))
                    break

    # Recalculate this repo's timer
    self._recalculate_timer(owner_repo)

    return notify_events
```

#### 0.5 `daemon.py` — Remove `_recalculate_mode()` and `_next_interval()`

**Remove:**
- `_recalculate_mode()` (lines 465–491)
- `_next_interval()` (lines 445–463)
- `DaemonConfig` fields for mode intervals (keep `low_water`, `max_backoff`, `backoff_multiplier`)

**Mark as OBSOLETE** — do not delete yet. Tests may reference these.

#### 0.6 `daemon.py` — Add rate limit backoff to `_calc_repo_interval()`

Update `_calc_repo_interval()` to apply rate limit backoff:

```python
def _calc_repo_interval(self, owner_repo: str) -> int:
    """Determine poll interval for a repo based on state + rate limits."""
    # State-based interval
    unknown_count = self.db.execute(
        "SELECT COUNT(*) FROM pull_requests "
        "WHERE owner_repo = ? AND mergeable = ? AND state = ?",
        (owner_repo, MERGEABLE_UNKNOWN, PR_STATE_OPEN),
    ).fetchone()[0]
    if unknown_count > 0:
        interval = self.PR_CHANGED_INTERVAL
    else:
        rows = self.db.execute(
            "SELECT status FROM ci_events "
            "WHERE owner_repo = ? "
            "GROUP BY pr_number, job_name "
            "HAVING recorded_at = MAX(recorded_at)",
            (owner_repo,),
        ).fetchall()
        has_active_ci = any(status in ACTIVE_STATUSES for (status,) in rows)
        interval = self.ACTIVE_INTERVAL if has_active_ci else self.INACTIVE_INTERVAL

    # Rate limit backoff (per account)
    row = self.db.execute(
        "SELECT gh_account FROM repos WHERE owner_repo = ?", (owner_repo,)
    ).fetchone()
    if row:
        account = row[0]
        remaining = self._get_rate_limit_remaining(account)
        if remaining is not None and remaining < self.config.low_water:
            if remaining < self.config.low_water / 2:
                interval *= 4
            else:
                interval *= 2

    return interval
```

#### 0.7 `httpd.py` — Accept `?repo=` query parameter on `POST /poll`

Update `_handle_poll()` to accept optional `?repo=owner/repo` query param:

```python
def _handle_poll(self):
    """POST /poll — trigger immediate poll for a specific repo or all repos."""
    parsed = urlparse(self.path)
    params = parse_qs(parsed.query)
    target_repo = params.get("repo", [None])[0]

    d = self.daemon_ref
    if target_repo:
        # Reset specific repo's timer to now
        d._timers[target_repo] = time.time()
        self._json_ok({
            "message": f"timer reset for {target_repo}",
            "repo": target_repo,
        })
    else:
        # Reset all repo timers to now (manual ci poll)
        now = time.time()
        for repo in d._timers:
            d._timers[repo] = now
        self._json_ok({
            "message": f"timer reset for {len(d._timers)} repos",
            "repo_count": len(d._timers),
        })
```

**Remove:** `from daemon import ActivityMode` import and `d.mode = ActivityMode.PR_CHANGED` assignment.

#### 0.8 `httpd.py` — Update `/status` response

Replace `"mode"` field with `"timers"` showing per-repo next poll times:

```python
def _handle_status(self):
    d = self.daemon_ref
    now = time.time()
    timers = {
        repo: {
            "next_poll_at": ts,
            "seconds_until": max(0, int(ts - now)),
        }
        for repo, ts in d._timers.items()
    }
    self._json_ok({
        "repo_count": len(d._timers),
        "timers": timers,
        "rate_limit_remaining": d._min_rate_limit_remaining(),
    })
```

#### 0.9 `hooks.py` — Update post-push hook to include `?repo=`

Update `HOOK_SCRIPT` constant to derive repo name from git remote:

```python
HOOK_SCRIPT = """#!/bin/sh
# continuity post-push hook — wakes daemon for this repo
[ "$1" = "origin" ] || exit 0
PORT=$(cat "${CONTINUITY_HOME:-$HOME/.local/share/continuity}/daemon.port" 2>/dev/null)
REPO=$(git remote get-url origin | sed 's|.*[:/]\\([^/]*/[^/]*\\)\\.git|\\1|')
[ -n "$PORT" ] && curl -s -X POST "http://localhost:$PORT/poll?repo=$REPO" >/dev/null 2>&1 &
exit 0
"""
```

### Test Changes

**Files**: `python/tests/test_daemon.py`, `python/tests/test_httpd.py`,
`python/tests/test_hooks.py`

#### test_daemon.py — Replace mode tests with timer tests

**Remove:**
- `test_mode_recalculation` — global mode is gone
- `test_mode_transitions` — global mode is gone
- `test_next_interval_*` — replaced by `_calc_repo_interval`

**Add:**
- `test_timers_initialized_on_startup` — all registered repos get `now` timestamp
- `test_only_due_repos_polled` — mock two repos, one due, one not → only due repo polled
- `test_timer_recalculated_after_poll` — poll → timer updated based on repo state
- `test_pr_changed_when_unknown_mergeable` — repo with UNKNOWN mergeable → 30s interval
- `test_active_when_ci_running` — repo with IN_PROGRESS job → 300s interval
- `test_inactive_when_no_ci_no_unknown` — idle repo → 1200s interval
- `test_shortest_interval_wins` — UNKNOWN mergeable + active CI → 30s (PR_CHANGED wins)
- `test_rate_limit_doubles_interval` — remaining < LOW_WATER → interval × 2
- `test_rate_limit_quadruples_interval` — remaining < LOW_WATER/2 → interval × 4
- `test_account_batching` — repos under same account batched into one GraphQL call
- `test_timer_reset_on_poll_post` — `POST /poll?repo=foo` → `_timers["foo"]` ≈ now
- `test_poll_all_on_post_no_repo` — `POST /poll` → all timers reset to now

#### test_httpd.py — Update for per-repo wake

**Remove:**
- `test_poll_sets_pr_changed_mode` — no more global mode

**Add:**
- `test_poll_with_repo_resets_timer` — `POST /poll?repo=owner/repo` → timer reset
- `test_poll_without_repo_resets_all` — `POST /poll` → all timers reset
- `test_status_shows_timers` — `GET /status` → `"timers"` key with per-repo data

#### test_hooks.py — Update for `?repo=` in hook

**Update:**
- `test_hook_script_contains_curl_post_poll` — assert `?repo=` in hook content
- `test_hook_derives_repo_from_remote` — assert `git remote get-url origin` in hook

### Acceptance

- [ ] `ActivityMode` enum removed from `daemon.py`
- [ ] `_recalculate_mode()` and `_next_interval()` marked `# OBSOLETE`
- [ ] `_timers` dict tracks per-repo `next_poll_at`
- [ ] `_run_loop()` polls only due repos, batched by account
- [ ] `_calc_repo_interval()` returns correct interval per state + rate limit
- [ ] `POST /poll?repo=owner/repo` resets only that repo's timer
- [ ] `POST /poll` (no repo) resets all timers
- [ ] Hook script includes `?repo=$REPO` parameter
- [ ] `GET /status` returns `"timers"` instead of `"mode"`
- [ ] All existing tests updated; new timer tests pass
- [ ] Old code marked `# OBSOLETE` but not deleted

---

## Sprint 1 — Task 1: SQLite Write Safety

**Effort**: Medium
**Depends on**: Task 0 (new poll loop structure)

### Goal

Remove `check_same_thread=False` and shared `self.db` across threads. Every
component that reads SQLite opens its own connection. Only the daemon's poll
thread writes.

### Changes

#### 1.1 `httpd.py` — Create per-request read connections

**Current:** `DaemonHandler.db_conn` is created once in `start_httpd()` and
shared across all requests with `check_same_thread=False`.

**Replace with:** Each handler method opens its own connection:

```python
def _get_db(self):
    """Open a read-only SQLite connection for this request."""
    conn = sqlite3.connect(self.db_path)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn
```

Add `self.db_path` to `DaemonHandler` (set by `start_httpd`). Remove
`db_conn` class attribute. Every handler method calls `db = self._get_db()`
and closes it when done (use context manager `with self._get_db() as db:`).

#### 1.2 `httpd.py` — Remove `check_same_thread=False`

Remove from `start_httpd()` (lines 351–353).

#### 1.3 `notify.py` — Open own read connection

**Current:** `dispatch_notifications(db, events)` takes the daemon's `self.db`
and passes it to a spawned thread.

**Replace with:** `dispatch_notifications(db_path, events)` takes the
database path and opens its own connection in the notification thread:

```python
def dispatch_notifications(db_path: str, events: list[NotificationEvent]) -> None:
    if not events or not atm_configured():
        return
    # Group events (same logic)
    groups = ...
    batched = ...
    thread = threading.Thread(
        target=_dispatch_batched,
        args=(db_path, batched),
        daemon=True,
        name="continuity-notify",
    )
    thread.start()

def _dispatch_batched(db_path, batched):
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA query_only=ON")
    try:
        for owner_repo, requested_by, notifications in batched:
            atm_notify(db, owner_repo, requested_by, notifications)
    finally:
        db.close()
```

#### 1.4 `daemon.py` — Pass `db_path` instead of `db` to notify

Update line 229: `dispatch_notifications(db_path, notify_events)` instead of
`dispatch_notifications(self.db, notify_events)`.

#### 1.5 `daemon.py` — Remove `_poll_lock`

No longer needed — only the poll thread writes to SQLite. The HTTPD no longer
triggers `_poll_cycle()` directly; it just resets a timer. The poll loop is
the sole writer.

Remove:
- `self._poll_lock = threading.Lock()` (line 96)
- `import threading` (if no longer used — check: `threading.Thread` is still
  used in notify, but that's in notify.py, not daemon.py)

### Test Changes

#### test_httpd.py — Per-request connections

- `test_handler_opens_own_connection` — handler creates + closes connection
- `test_handler_connection_is_read_only` — `PRAGMA query_only=ON` is set
- Remove tests that mock `db_conn` class attribute

#### test_daemon.py — Remove `_poll_lock` references

- Remove `test_poll_lock_acquired_during_cycle` — no lock
- Remove `test_poll_lock_prevents_concurrent_write` — no lock

#### test_notify.py — Own connection

- `test_notify_opens_own_connection` — verify `sqlite3.connect` called in thread
- `test_notify_closes_connection` — verify `.close()` called

### Acceptance

- [ ] No `check_same_thread=False` anywhere in codebase
- [ ] HTTPD opens per-request read connections, closes them after
- [ ] `dispatch_notifications` takes `db_path` (str), not `db` (Connection)
- [ ] Notification thread opens + closes its own connection
- [ ] `_poll_lock` removed from daemon
- [ ] `self.db` still exists in daemon (poll thread's writer connection) but
  is never passed to other threads
- [ ] All tests pass

---

## Sprint 3 — Task 3: Dead Code Cleanup

**Effort**: Small
**Depends on**: Tasks 0, 1

### Goal

Remove code marked `# OBSOLETE` after verification. Delete:
- `ActivityMode` enum
- `_recalculate_mode()`
- `_next_interval()`
- `DaemonConfig` mode interval fields
- Any `_poll_lock` references
- Old `import threading` if unused in daemon.py

### Changes

#### 3.1 Remove OBSOLETE blocks

Search for `# OBSOLETE` in codebase, verify replacement is working, delete.

#### 3.2 Remove old mode-related tests

From `test_daemon.py`:
- `test_mode_*` — all mode tests
- `test_next_interval_*` — all interval tests
- `test_poll_lock_*` — all lock tests (if not already removed)

#### 3.3 Verify no dead references

```bash
rg "ActivityMode|_recalculate_mode|_next_interval|_poll_lock|_wake_event|_scheduled_wake" python/
```

Should return zero results (or only in OBSOLETE blocks that are being removed).

### Acceptance

- [ ] Zero references to `ActivityMode`, `_recalculate_mode`, `_next_interval`
- [ ] Zero references to `_poll_lock` (except in docs)
- [ ] All tests pass
- [ ] `rg` confirms no dead code remaining

---

## Sprint 4 — Task 4: CLI Commands Use HTTP RPC

**Effort**: Medium
**Depends on**: Task 0 (HTTPD `/status` now returns timers)

### Goal

All CLI commands (`ci status`, `ci log`, `ci history`, `ci usage`) call the
daemon's HTTP RPC endpoints instead of reading SQLite directly.

### Changes

Follow the existing sprint plan (`sprint-plan-http-rpc-migration.md`)
Sprint 1 sections 1.1–1.4, 1.6–1.9, adapted for the new `/status` response
format (timers instead of mode).

### Acceptance

- [ ] `ci status` returns data via HTTP RPC
- [ ] `ci log o/r 42` returns event timeline via HTTP RPC
- [ ] `ci history o/r` returns closed PRs via HTTP RPC
- [ ] `ci poll` triggers `POST /poll` (all repos)
- [ ] Old SQLite code in CLI marked `# OBSOLETE`

---

## Sprint 5 — Task 5: CiSlow/CiTimeout Wired to Interceptor

**Effort**: Medium
**Depends on**: Task 0 (timer model doesn't affect this)

### Goal

Move slow/timeout detection from daemon to interceptor parse path.

### Changes

Follow the existing sprint plan (`sprint-plan-http-rpc-migration.md`)
Sprint 5 sections, adapted for per-repo timer context.

### Acceptance

- [ ] `gh pr view` emits `CiSlow` when elapsed > 2× EMA
- [ ] `gh pr view` emits `CiTimeout` when elapsed > max_ci_duration
- [ ] Minimum 3 successful runs before thresholds apply
- [ ] Daemon no longer has slow/timeout detection code

---

## Sprint 6 — Task 6: Integration Test

**Effort**: Medium
**Depends on**: Tasks 0, 4, 5

### Goal

End-to-end test: interceptor captures identity → daemon detects conflict →
ATM notification fires.

### Test Scenario

```
1. Start daemon with temp CONTINUITY_HOME, mock GhClient
2. Register test repo
3. Simulate gh pr create via interceptor → POST /poll?repo=test/repo
4. Verify only test/repo timer was reset (other repos unchanged)
5. Feed mock poll result: UNKNOWN → CONFLICTING
6. Verify PrCreatedUnmergable notification dispatched
7. Verify notification body has PR number, requested_by identity
```

### Acceptance

- [ ] Full lifecycle: interceptor → per-repo timer → poll → notification
- [ ] Only the target repo's timer is affected
- [ ] ATM notification includes requested_by identity
- [ ] Test is repeatable (clean temp dirs, no state leakage)

---

## Sprint 7 — Task 7: Timer Startup Recovery

**Effort**: Small
**Depends on**: Task 0

### Goal

On daemon restart, all repos default to PR_CHANGED (30s) for the first poll,
then recalculate. No timer persistence needed.

### Changes

`_init_timers()` already sets all repos to `now` (immediate poll). This is
the correct behavior — a restart means the daemon may have missed events.
After the first poll, `_recalculate_timer()` sets the appropriate interval.

### Test Changes

- `test_timers_initialized_to_now_on_startup` — verify all timers are `now`
- `test_timer_recalculated_after_first_poll` — verify interval changes after poll

### Acceptance

- [ ] All timers set to `now` on startup
- [ ] First poll recalculates to correct interval
- [ ] No timer persistence files (by design)

---

## Post-Sprint Tasks

### Review and Cleanup

1. Remove all `# OBSOLETE` code in a dedicated cleanup PR
2. Verify `ActivityMode`, `_recalculate_mode`, `_next_interval`, `_poll_lock`
   completely purged from codebase
3. Run full test suite: `python -m pytest python/tests/ -v`
4. Smoke test: `ci daemon` → `ci status` → `ci poll` → `ci log` on
   `randlee/continuity-test` repo

### Documentation Finalization

- Update `docs/architecture.md` §15 — move completed tasks to "Done" table
- Update `docs/requirements.md` §8 — same
- Remove `docs/sprint-plan-http-rpc-migration.md` (fully superseded)
- Update any FR IDs that shifted during implementation

---

## File Change Summary

| Sprint | Files Changed | Files Added | Files Removed |
|:---:|---|---|---|
| 0 | `daemon.py`, `httpd.py`, `hooks.py`, test files | — | — |
| 1 | `httpd.py`, `notify.py`, `daemon.py`, test files | — | — |
| 3 | `daemon.py`, `constants.py`, test files | — | `ActivityMode` enum, mode methods |
| 4 | `cli/daemon_cmd.py`, `cli/http_client.py` | — | Old SQLite query helpers |
| 5 | `gh/pr_view.py`, `gh/pr_checks.py`, `gh/__init__.py` | `gh/monitor_check.py` (maybe) | Daemon-side detection |
| 6 | `test_integration.py` | — | — |
| 7 | `daemon.py` (startup) | — | — |
| Cleanup | Remove `# OBSOLETE` blocks | — | Marked dead code |