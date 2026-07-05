# ADR 001 — ATM Notification Routing

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-07-05 |
| **Author** | alpha-prime (harness PM) |
| **Supersedes** | — |
| **References** | [requirements-atm.md](../requirements-atm.md), [architecture.md](../architecture.md) §9.1 |

## Context

Continuity detects state changes in PRs and CI jobs. When a state change
requires human or agent attention, Continuity must notify the right person
via ATM (Agent Team Mail). The notification routing must handle:

1. **Agent-driven actions:** an agent creates a PR or pushes a commit that
   makes a PR unmergable. The agent that caused the state change should be
   notified — they are the only one who can fix it.
2. **Cascade effects:** a PR merges successfully but causes other PRs to
   become unmergable. The affected PR authors didn't cause the breakage,
   but they must rebase.
3. **Daemon-detected events:** CI completion, slow detection, and timeout
   detection happen in the daemon poll loop with no associated agent
   identity.
4. **Anonymous actions:** a human pushes manually with no ATM identity set.
5. **Roster gaps:** an ATM identity is set but that member is not in the
   team roster — `atm send` will fail.
6. **Transient failures:** `atm send` can fail due to socket timeouts,
   lock contention, or process crashes — distinct from permanent roster
   failures.

## Decision

### Routing Principle

> **Whoever can fix the problem gets notified. If they can't be reached,
> `team-lead` handles it.**

### Notification Matrix

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

### Designated Member

The designated member is stored in continuity's SQLite database (a
key-value config table), not in a separate config file. When no value is
stored, the default is `team-lead`. At notification time, if the stored
member is not in the ATM roster, the system falls back to `team-lead`.

The `team-lead` handle resolves through ATM's team roster to the actual
agent (e.g., `hendrix` for the `hermes` team). `team-lead` is a required
role in every ATM team — no new concept to teach.

```
Effective fallback: stored member → team-lead → (log error)
```

**CLI surface:**
```
ci atm set-notify <member>     # store in continuity DB
ci atm set-notify --reset       # remove stored value → team-lead default
ci atm show-notify              # print current state (stored or "team-lead (default)")
```

`set-notify` validates the member name is a well-formed ATM identity but
does not validate roster membership — that happens at send time when the
ATM CLI can authoritatively report the error. This avoids a TOCTOU race
between set-notify and the actual notification.

### Identity Model

Continuity sends ATM messages **as** the read-only `ci` team member, but
includes the requesting identity in the message body:

```
From: ci (on behalf of rand)
Subject: PR #42 unmergable
```

This separation ensures:
- All continuity-originated messages have a consistent sender (`ci`)
- Recipients know who triggered the notification
- The `ci` member never self-notifies (treated as unset)

The `ci` member is registered in the ATM team via `atm team member add`
(stored in ATM's SQLite database). ATM is the authority on roster
membership — continuity does not maintain its own member list.

### Transient Failure Handling

ATM send failures fall into two categories:

| Category | Examples | Behavior |
|---|---|---|
| **Permanent** | Member not in roster | Fall back to `team-lead` immediately |
| **Transient** | Socket timeout, lock contention, `atm` crash | Retry 3× with exponential backoff (1s, 2s, 4s), then fall back to `team-lead` |

Each send attempt times out at 5 seconds. Total worst-case: ~22 seconds
before fallback (5s × 3 attempts + 7s cumulative backoff). The poll loop
is not blocked — notifications are sent in a spawned task.

### Batching

When multiple events in a single poll cycle resolve to the same target,
they are batched into one message. This prevents notification storms when,
for example, a merge cascade affects 5 PRs simultaneously.

### Fallback Chain

The fallback chain is linear and terminates:

```
stored designated member → team-lead → (log error)
```

If the stored member is not set, the chain starts at `team-lead`. If
`team-lead` is also not in the roster, continuity logs the error and
drops the notification. No recursive fallback, no broadcast.

### Module Boundary

ATM integration is a self-contained module (`continuity/atm.py` or
`continuity/atm/`). The rest of Continuity calls a narrow public interface.
If `ATM_TEAM` and `ATM_IDENTITY` are not both set, every public call is a
no-op.

This keeps ATM optional without polluting call sites with conditionals.

`ATM_TEAM` is a per-repo environment variable — different repos can use
different ATM teams. The module reads it from the environment at call
time, not at import time.

## Alternatives Considered

### A: Always notify team-lead

Rejected. Team-lead becomes a bottleneck for agent-driven events. Agents
who cause breakage should be notified directly — they have the context
to fix it.

### B: Notify all team members on every event

Rejected. Noise. When everyone is notified, no one is responsible. Does
not scale beyond 3–4 members.

### C: Derive identity from git log

Rejected. Unreliable — the last committer may not be the ATM team member
who requested the PR. Squash merges and rebases obscure authorship.

### D: Per-PR notification preferences

Rejected. Over-engineered for v1. `team-lead` with direct-agent
notification covers the cases that matter.

### E: Per-repo designated member via `.continuity.toml`

Rejected as a config-file approach. The same functionality (per-repo
designated member) is instead stored in continuity's own SQLite database,
which already exists for the event log. No new config file, no new file
format — just a key-value row in an existing database.

## Consequences

### Positive

- Agents get direct feedback when their actions cause a problem
- `team-lead` provides a safety net for unowned events
- Module boundary keeps ATM optional — continuity works without it
- No polling overhead — ATM is invoked only when a trigger fires
- No config file — zero setup beyond `ci` team registration
- Batching prevents notification storms during cascades

### Negative

- `team-lead` is a single point of triage for all daemon-detected events
- ATM message delivery is fire-and-forget after retries — no read
  receipts, no acknowledgment loop
- If `ci` is misconfigured (not in roster), all ATM notifications
  silently fail
- Transient failure retries add up to ~22s worst-case latency before
  fallback

### Mitigations

- `ci atm status` validates ATM configuration at setup time
- Failed deliveries are logged, not swallowed — operators can monitor
  the log for ATM delivery failures
- Notifications are sent in spawned tasks — retry latency does not
  block the poll loop
