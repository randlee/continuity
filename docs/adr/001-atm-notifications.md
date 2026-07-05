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
3. **Anonymous actions:** a human pushes manually, or the daemon detects a
   change during polling with no associated agent identity.
4. **Roster gaps:** an ATM identity is set but that member is no longer in
   the team roster — `atm send` will fail.

## Decision

### Routing Principle

> **Whoever can fix the problem gets notified. If they can't be reached,
> the designated member handles it.**

### Notification Matrix

| Trigger | Target | Fallback |
|---|---|---|
| PR created (unmergable) | `ATM_IDENTITY` (creator) | Designated member |
| Commit pushed → PR becomes unmergable | `ATM_IDENTITY` (pusher) | Designated member |
| PR-A merges → PR-B becomes unmergable | Designated member | — (terminal) |
| `ATM_IDENTITY` not set (manual push, cron) | — | Designated member |
| `ATM_IDENTITY` set but not in roster (`atm send` fails) | — | Designated member |

### Designated Member

A single team member receives notifications when the requesting member
cannot be identified or reached. Defaults to `team-lead`. Persisted in
`.continuity.toml` at the repo root so it travels with the repo.

Rationale: broadcast notifications don't scale — no one feels responsible.
A single triage point mirrors the incident on-call pattern. The designated
member can be changed at runtime via `ci atm set-notify <member>`.

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
- `ci` is a permanent member of the ATM team (`.atm.toml`), not a runtime
  temporary member

### Fallback Chain

The fallback chain is linear and terminates:

```
ATM_IDENTITY → designated member → (log error)
```

It never loops. If the designated member is also not in the roster,
Continuity logs the error and drops the notification. No recursive
fallback, no broadcast.

### Module Boundary

ATM integration is a self-contained module (`continuity/atm.py` or
`continuity/atm/`). The rest of Continuity calls a narrow public interface.
If `ATM_TEAM` and `ATM_IDENTITY` are not both set, every public call is a
no-op.

This keeps ATM optional without polluting call sites with conditionals.

## Alternatives Considered

### A: Always notify team-lead

Rejected. Team-lead becomes a bottleneck and single point of failure.
Agents who cause breakage should be notified directly — they have the
context to fix it.

### B: Notify all team members on every event

Rejected. Noise. When everyone is notified, no one is responsible. Does
not scale beyond 3–4 members.

### C: Derive identity from git log

Rejected. Unreliable — the last committer may not be the ATM team member
who requested the PR. Squash merges and rebases obscure authorship.

### D: Per-PR notification preferences

Rejected. Over-engineered for v1. A single designated member with
direct-agent notification covers the cases that matter.

## Consequences

### Positive

- Agents get direct feedback when their actions cause a problem
- Designated member provides a safety net for unowned events
- Module boundary keeps ATM optional — continuity works without it
- No polling overhead — ATM is invoked only when a trigger fires

### Negative

- Designated member is a single point of triage for cascade events
- ATM message delivery is fire-and-forget — no read receipts, no
  acknowledgment loop
- If `ci` is misconfigured (not in roster), all ATM notifications
  silently fail

### Mitigations

- `ci atm status` validates ATM configuration at setup time
- Designated member can be changed at runtime via CLI
- Failed deliveries are logged, not swallowed — operators can monitor
  the log for ATM delivery failures
