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
    """Send to a team member. Returns False if member not in roster."""

def atm_get_designated_member() -> str:
    """Read .continuity.toml, default to 'team-lead'."""

def atm_notify(target: str | None, subject: str, body: str) -> bool:
    """
    Resolve target through the fallback chain and send.
    Returns True if message was delivered (to target or fallback).
    """
```

## 2. Identity Model

Continuity sends ATM messages **as** the read-only `ci` team member, but
carries the requesting member's identity in the message body. This mirrors
the `sudo` / `SUDO_USER` pattern.

| ID | Requirement |
|---|---|
| FR-ATM-04 | `ci` is a permanent read-only member of the ATM team (`.atm.toml`) |
| FR-ATM-05 | ATM messages are sent with `ATM_IDENTITY=ci` |
| FR-ATM-06 | Message body includes the requesting identity: `From: ci (on behalf of <identity>)` |
| FR-ATM-07 | `ATM_IDENTITY` env var carries the requesting member — set by the CLI caller or daemon context |

**Identity sources for `ATM_IDENTITY`:**

| Source | How set |
|---|---|
| `ci pr check --atm-identity=<member>` | CLI flag |
| Agent invokes `gh pr create` | Captured by interceptor from agent's `ATM_IDENTITY` env |
| Agent invokes `git push` to PR branch | Captured by interceptor from agent's `ATM_IDENTITY` env |
| Manual `git push` (human) | Not set → triggers designated-member fallback |
| Daemon poll (automated) | Not set → triggers designated-member fallback |

## 3. Notification Routing

### 3.1 Routing Rules

Notifications follow a single principle: **whoever can fix the problem gets
notified. If they can't be reached, the designated member handles it.**

| Trigger | Target | Fallback |
|---|---|---|
| PR created (unmergable) | `ATM_IDENTITY` (creator) | Designated member |
| Commit pushed → PR becomes unmergable | `ATM_IDENTITY` (pusher) | Designated member |
| PR-A merges → PR-B becomes unmergable | Designated member | — (terminal) |
| `ATM_IDENTITY` not set (manual push, cron) | — | Designated member |
| `ATM_IDENTITY` set but not in roster (`atm send` fails) | — | Designated member |

| ID | Requirement |
|---|---|
| FR-ATM-08 | Notification target resolves through the matrix above — no ad-hoc routing at call sites |
| FR-ATM-09 | When `ATM_IDENTITY` is set, attempt delivery to that member first |
| FR-ATM-10 | When `atm send` fails (member not in roster), retry with designated member |
| FR-ATM-11 | Cascade notifications (PR merges → other PRs become unmergable) always route to designated member |
| FR-ATM-12 | Success notifications (PR merges, CI passes) route to the requesting member only |

### 3.2 Edge Cases

| Scenario | Behavior |
|---|---|
| `ATM_IDENTITY` is `ci` itself | Treat as unset — route to designated member |
| Designated member is not in roster | Log error, skip notification (no infinite fallback) |
| Multiple PRs become unmergable in same poll | Single message to designated member with summary |
| Trigger fires but ATM not configured | Silent no-op (FR-ATM-01) |

| ID | Requirement |
|---|---|
| FR-ATM-13 | `ATM_IDENTITY=ci` is treated as unset (prevents self-send loops) |
| FR-ATM-14 | If designated member is also not in roster, log error and skip — no recursive fallback |
| FR-ATM-15 | Batch cascade notifications: one message for N PRs, not N messages |

## 4. Designated Member

A single team member receives notifications when the requesting member
cannot be identified or reached. Defaults to `team-lead`.

| ID | Requirement |
|---|---|
| FR-ATM-16 | Default designated member is `team-lead` |
| FR-ATM-17 | `ci atm set-notify <member>` pins a different designated member |
| FR-ATM-18 | `ci atm set-notify --reset` restores the default (`team-lead`) |
| FR-ATM-19 | Designated member is persisted in `.continuity.toml` at the repo root |
| FR-ATM-20 | `ci atm show-notify` prints the current designated member |

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
| PR-A merges → PR-B unmergable | `PR #N now unmergable` | `PR #<A> merged. PR #<B> (by <identity>) is now unmergable — merge conflict in:\n  <file list>` |
| CI completed (success) | `PR #N CI passed` | `PR #N (by <identity>) — all checks passed` |
| CI completed (failure) | `PR #N CI failed` | `PR #N (by <identity>) — <failed_job> failed` |

## 6. CLI Commands

| Command | Description |
|---|---|
| `ci atm set-notify <member>` | Pin designated member |
| `ci atm set-notify --reset` | Restore default (`team-lead`) |
| `ci atm show-notify` | Print current designated member |
| `ci atm status` | Check ATM configuration (env vars set, team exists, `ci` in roster) |

| ID | Requirement |
|---|---|
| FR-ATM-26 | `ci atm status` exits 0 when fully configured, non-zero otherwise |
| FR-ATM-27 | `ci atm status` reports which preconditions are missing |

## 7. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NF-ATM-01 | ATM message delivery is fire-and-forget. Failed delivery does not block the poll loop |
| NF-ATM-02 | ATM send timeout ≤ 5 seconds. On timeout, treat as delivery failure and fall back |
| NF-ATM-03 | ATM module has no import-time side effects — env vars checked at call time, not import time |
| NF-ATM-04 | `atm` CLI is never invoked in the poll loop hot path for non-notification work |
| NF-ATM-05 | Notification formatting is testable without an ATM installation |
