# Continuity — ATM Notification Requirements

Language-agnostic functional and non-functional requirements for ATM
(Agent Team Mail) integration in Continuity.

References:
- [Continuity Requirements](requirements.md) — primary requirements document
- [ADR 001 — ATM Notifications](adr/001-atm-notifications.md) — architectural decisions

## 1. Module Boundary

The ATM adapter is a self-contained module (`continuity/atm.py` or
`continuity/atm/`). The rest of Continuity calls a narrow public interface;
the module internally owns ATM CLI invocation, team resolution, and message
formatting.

If `ATM_TEAM` and `ATM_IDENTITY` are not both set in the environment, every
public call is a no-op — no stubs, no fallback message generation, no error.
The call sites do not branch on ATM availability.

| ID | Requirement |
|---|---|
| FR-ATM-01 | Module is a no-op when `ATM_TEAM` or `ATM_IDENTITY` is unset |
| FR-ATM-02 | Callers never check ATM availability — the module handles it internally |
| FR-ATM-03 | Module exposes a narrow interface; ATM CLI details are private |

### Public API

```python
def atm_configured() -> bool:
    """True if ATM_TEAM and ATM_IDENTITY are both set."""

def atm_send(to: str, subject: str, body: str) -> bool:
    """
    Send to a team member within ATM_TEAM.
    Returns False if member not in roster (permanent failure).
    May raise transient failures (timeout, lock contention) — caller retries.
    """

def atm_notify(target: str | None, subject: str, body: str) -> bool:
    """
    Resolve target through the fallback chain and send.
    target=None means route directly to designated member (team-lead).
    Retries transient failures up to 3× with backoff.
    Returns True if message was delivered (to target or fallback).
    """
```

## 2. Identity Model

Continuity sends ATM messages **as** the read-only `ci` team member, but
carries the requesting member's identity in the message body. This mirrors
the `sudo` / `SUDO_USER` pattern.

The `ci` member is registered in the ATM team via `atm team member add`
(stored in ATM's SQLite database). It is a permanent member — never a
runtime temporary member.

| ID | Requirement |
|---|---|
| FR-ATM-04 | `ci` is a permanent member of the ATM team, registered via `atm team member add` |
| FR-ATM-05 | ATM messages are sent with `ATM_IDENTITY=ci` |
| FR-ATM-06 | Message body includes the requesting identity: `From: ci (on behalf of <identity>)` |
| FR-ATM-07 | `ATM_IDENTITY` env var carries the requesting member — set by the CLI caller or captured from agent environment |

**Identity sources for `ATM_IDENTITY`:**

| Source | How set |
|---|---|
| `ci pr check --atm-identity=<member>` | CLI flag |
| Agent invokes `gh pr create` | Captured by interceptor from agent's `ATM_IDENTITY` env |
| Agent invokes `git push` to PR branch | Captured by interceptor from agent's `ATM_IDENTITY` env |
| Manual `git push` (human) | Not set → routes to designated member |
| Daemon poll (automated) | Not set → routes to designated member |

## 3. Notification Routing

### 3.1 Routing Rules

Notifications follow a single principle: **whoever can fix the problem gets
notified. If they can't be reached, the designated member (`team-lead`)
handles it.**

Events the daemon detects independently (CI completion, slow/timeout)
always route to `team-lead` — there is no requesting member in the poll
path.

| Trigger | Target | Fallback |
|---|---|---|
| PR created (unmergable) | `ATM_IDENTITY` (creator) | `team-lead` |
| Commit pushed → PR becomes unmergable | `ATM_IDENTITY` (pusher) | `team-lead` |
| PR-A merges → PR-B becomes unmergable | `team-lead` | — (terminal) |
| CI completed (success) | `team-lead` | — (terminal) |
| CI completed (failure) | `team-lead` | — (terminal) |
| CI slow (elapsed > 2× EMA) | `team-lead` | — (terminal) |
| CI timeout (elapsed > max) | `team-lead` | — (terminal) |
| `ATM_IDENTITY` not set (manual push, cron) | — | `team-lead` |
| `ATM_IDENTITY` set but not in roster (`atm send` fails) | — | `team-lead` |

| ID | Requirement |
|---|---|
| FR-ATM-08 | Notification target resolves through the matrix above — no ad-hoc routing at call sites |
| FR-ATM-09 | When `ATM_IDENTITY` is set, attempt delivery to that member first |
| FR-ATM-10 | When `atm send` fails with permanent error (not in roster), retry with `team-lead` |
| FR-ATM-11 | Cascade notifications (PR merges → other PRs become unmergable) always route to `team-lead` |
| FR-ATM-12 | CI completion, slow, and timeout events always route to `team-lead` |

### 3.2 Transient Failure Handling

ATM send can fail transiently (socket timeout, lock contention, `atm`
process crash). These are retried before falling back.

| ID | Requirement |
|---|---|
| FR-ATM-12a | Transient failures retry up to 3 times with exponential backoff (1s, 2s, 4s) |
| FR-ATM-12b | After retries exhausted, fall back to `team-lead` |
| FR-ATM-12c | Permanent failures (member not in roster) do not retry — fall back immediately |

### 3.3 Edge Cases

| Scenario | Behavior |
|---|---|
| `ATM_IDENTITY` is `ci` itself | Treat as unset — route to `team-lead` |
| `team-lead` is not in roster | Log error, skip notification (terminal — no further fallback) |
| Multiple PRs become unmergable in same poll cycle | Single batched message to the resolved target |
| Trigger fires but ATM not configured | Silent no-op (FR-ATM-01) |

| ID | Requirement |
|---|---|
| FR-ATM-13 | `ATM_IDENTITY=ci` is treated as unset (prevents self-send loops) |
| FR-ATM-14 | If `team-lead` is also not in roster, log error and skip — no recursive fallback |
| FR-ATM-15 | Batch notifications: when multiple events resolve to the same target in one poll cycle, send one message summarizing all |

## 4. Designated Member

The designated member is stored in continuity's SQLite database (a
key-value config table). If no value is stored, or the stored member is
not in the ATM team roster, notifications fall back to `team-lead`.

`team-lead` resolves to the team leader for the ATM team identified by
`ATM_TEAM`. For the `hermes` team, this is `hendrix`.

| ID | Requirement |
|---|---|
| FR-ATM-16 | Designated member defaults to `team-lead` when no value is stored |
| FR-ATM-17 | `ci atm set-notify <member>` stores a designated member in the continuity DB |
| FR-ATM-18 | `ci atm set-notify --reset` removes the stored value, reverting to `team-lead` |
| FR-ATM-19 | `ci atm show-notify` prints the current designated member, noting whether it is stored or the default |
| FR-ATM-20 | At notification time, if the stored member is not in the ATM roster, fall back to `team-lead` |

### Effective Fallback Chain

```
stored designated member → team-lead → (log error)
```

## 5. Message Content

### 5.1 Unmergable Status

Messages for unmergable PRs include the conflicting file list to help
recipients assess severity and act without additional queries.

| ID | Requirement |
|---|---|
| FR-ATM-21 | Unmergable notifications include a list of conflicting files |
| FR-ATM-22 | Display at most 6 files. When more than 6, show first 6 + `(N more)` |
| FR-ATM-23 | Total file count is always included, even when ≤6 |

**Format (≤6 files):**
```
PR #42 (by rand) is unmergable — merge conflict in:
  src/core/pipeline.rs
  src/core/config.rs
  tests/integration/test_pipeline.py
```

**Format (>6 files):**
```
PR #42 (by rand) is unmergable — merge conflict in 14 files including:
  src/core/pipeline.rs
  src/core/config.rs
  src/core/engine.rs
  src/core/scheduler.rs
  src/core/metrics.rs
  src/core/tracing.rs
  (8 more)
```

### 5.2 Daemon Status Snapshots

When the daemon returns a PR status snapshot, unmergable PRs include their
file list in the structured output.

| ID | Requirement |
|---|---|
| FR-ATM-24 | Daemon status snapshots include `unmergable_files` and `unmergable_file_count` for unmergable PRs |
| FR-ATM-25 | File list is capped at 6 in displayed output; full list available in structured data |

**Structured format:**
```yaml
pr:
  number: 42
  status: unmergable
  unmergable_files:
    - src/core/pipeline.rs
    - src/core/config.rs
  unmergable_file_count: 2
```

### 5.3 Trigger Notification Templates

| Trigger | Subject | Body |
|---|---|---|
| PR created (unmergable) | `PR #N unmergable` | `PR #N (by <identity>) is unmergable — merge conflict in:\n  <file list>` |
| Commit makes PR unmergable | `PR #N unmergable after push` | `PR #N (by <identity>) became unmergable after push <sha> — merge conflict in:\n  <file list>` |
| PR-A merges → PR-B unmergable | `PR #N now unmergable` | `PR #<A> merged. PR #<N> (by <identity>) is now unmergable — merge conflict in:\n  <file list>` |
| CI completed (success) | `PR #N CI passed` | `PR #N — all checks passed` |
| CI completed (failure) | `PR #N CI failed` | `PR #N — <failed_job> failed` |
| CI slow | `PR #N CI slow` | `PR #N — <job> running for <elapsed>, 2× normal (<ema>)` |
| CI timeout | `PR #N CI timeout` | `PR #N — <job> exceeded max duration (<max>), may be hung` |
| Batch (N events, same target) | `PR status update` | Summary of all events in one message |

## 6. CLI Commands

| Command | Description |
|---|---|
| `ci atm set-notify <member>` | Store designated member in continuity DB |
| `ci atm set-notify --reset` | Remove stored member, revert to `team-lead` default |
| `ci atm show-notify` | Print current designated member (stored or default) |
| `ci atm status` | Check ATM configuration: `ATM_TEAM` and `ATM_IDENTITY` set, team exists, `ci` in roster |

| ID | Requirement |
|---|---|
| FR-ATM-26 | `ci atm status` exits 0 when fully configured, non-zero otherwise |
| FR-ATM-27 | `ci atm status` reports which preconditions are missing |
| FR-ATM-28 | `ci atm set-notify <member>` validates the member is a valid ATM identity string but does not validate roster membership (roster validation happens at send time) |

## 7. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NF-ATM-01 | ATM message delivery is fire-and-forget after retries. Failed delivery does not block the poll loop |
| NF-ATM-02 | ATM send timeout ≤ 5 seconds per attempt. On timeout, treat as transient failure |
| NF-ATM-03 | ATM module has no import-time side effects — env vars checked at call time, not import time |
| NF-ATM-04 | `atm` CLI is never invoked in the poll loop hot path for non-notification work |
| NF-ATM-05 | Notification formatting is testable without an ATM installation |
| NF-ATM-06 | Transient `atm send` failures retry 3× with exponential backoff (1s / 2s / 4s). Permanent failures (not in roster) are not retried |
