# Continuity — PRD Updates (Nov 2026)

| Field | Value |
|---|---|
| **Status** | Draft — for review |
| **Date** | 2026-11-05 |
| **Author** | Rand Lee (with Claude) |
| **Amends** | [continuity-design-v3.docx](continuity-design-v3.docx) (Apr 2026), [requirements.md](../requirements.md), [architecture.md](../architecture.md), [ADR 001](../adr/001-atm-notifications.md) |
| **Input** | Review of [max-sixty/worktrunk](https://github.com/max-sixty/worktrunk) @ `79f19f6` (v0.81.0) |

This document is a **delta**. Everything in the v3 design and `requirements.md`
stands unless amended here. New requirement IDs continue from the current
`develop` numbering (FR-63 / NF-19). Proposed ADRs continue from ADR 001.

---

## 1. Summary of Changes

| # | Change | MVP? |
|---|---|---|
| U1 | Statusline output (Claude Code native; tmux/scmux for every other harness) | Yes |
| U2 | `continuity worktree` — worktree creation with **native stack management** (no `gh stack` dependency) | Yes |
| U3 | Cascade engine — push-triggered **and** merge-triggered restacking | Yes |
| U4 | Deferred "pull when convenient" nudge via the ATM task queue | Yes |
| U5 | Forge abstraction — **GitHub + Azure DevOps in MVP**; others post-MVP | Yes |
| U6 | Caching / poll-suppression patterns | Yes |
| U7 | Rate-limit **forecasting against a budget**, with attribution to repo / team / agent | Yes |

---

## 2. Background — What worktrunk Showed Us

worktrunk (`wt`) is a well-built git-worktree CLI for parallel agent workflows.
It overlaps Continuity only at the edges. Notable for Continuity:

| Area | worktrunk | Continuity (v3) | Takeaway |
|---|---|---|---|
| Forge API | `gh pr list --head <branch>` subprocess **per branch**; no batching | One GraphQL query per account | Continuity's model is correct; keep it |
| Rate limits | None (no headers, no 429/backoff). 30–60s jittered file cache is the only damper | `api_usage` table, adaptive modes, LOW_WATER backoff | Keep; add forecasting (U7) |
| Storage | JSON files under `.git/wt/cache/`, LRU-swept | SQLite, append-only `ci_events` | Keep SQLite |
| Stacks | Not native. `--base` used only at create time, **no parent pointer persisted**. Rebases pass `--no-update-refs` so children are never moved. `wt list` diffs everything against default branch | Cascade planned (Sep 2026), relied on `gh stack` | Continuity must own parent tracking + restacking (U2, U3) |
| Agent integration | Claude Code statusline; hooks write per-branch 🤖/💬 activity marker; Codex gets markers only | ATM triggers via LogSink | Adopt statusline (U1) and activity awareness (U4) |
| Forges | GitHub, GitLab, Gitea, Azure DevOps (`ForgeKind`) | GitHub only | Add forge trait (U5) |
| Small patterns | Jittered TTL, negative caching of errors, skip PR lookup for never-pushed branches, HEAD-SHA invalidation | — | Adopt (U6) |

**Dependency decision:** do **not** depend on the `worktrunk` crate. It is a
single lib+bin crate whose library API is explicitly declared unstable; the
CI/LLM/summary code lives in the binary, not the library; MSRV is 1.98 and the
crate moves fast (reverse deps pin `^0.81`). If interop with `wt` is wanted,
integrate at the CLI boundary (`wt list --format=json`, `wt` hooks). Patterns
may be re-implemented (worktrunk is MIT/Apache-2.0).

---

## 3. U1 — Statusline

### Context

- **Claude Code** supports a command-backed `statusLine` that receives session
  JSON on stdin.
- **Codex CLI** (as of Nov 2026) does **not**: `tui.status_line` accepts only a
  fixed list of built-in item identifiers; command-backed statuslines are an
  open feature request.
- The fleet runs under tmux (scmux), which supports `status-right` commands
  for any harness.

### Requirements

| ID | Requirement |
|---|---|
| FR-64 | `continuity statusline [--format claude-code\|tmux\|plain\|json]` renders CI/PR state for the current worktree's branch |
| FR-65 | `statusline` reads SQLite only — **zero forge API calls**, zero `git fetch` (inherits FR-38 / NF-11 read-path rule) |
| FR-66 | `--format claude-code` reads Claude Code session JSON from stdin (`workspace.current_dir`); missing fields are tolerated |
| FR-67 | `--format tmux` emits tmux format strings; installable into scmux `status-right` for Codex and any other harness |
| FR-68 | Statusline shows: PR #, mergeable state, CI rollup, stack position (e.g. `2/4`), pending-restack indicator, and budget pace warning (from U7) |
| NF-20 | p99 render time < 50 ms on a warm SQLite (statusline is called on every redraw) |
| NF-21 | If the daemon is not running, statusline prints a stale/offline marker rather than falling back to network calls |

---

## 4. U2 — Worktree Creation with Native Stack Management

### Context

The Sep 2026 cascade design assumed `gh stack`. That ties stacking to GitHub
and to a separately-pinned CLI extension. With ADO in MVP scope (U5), stacking
must be forge-neutral. This **supersedes** the earlier "gh-stack chosen as the
stacking tool" decision for Continuity.

Also addresses **worktree sprawl**: agents creating ad-hoc worktrees with no
coordination. Continuity becomes the sanctioned creator.

### Requirements

| ID | Requirement |
|---|---|
| FR-69 | `continuity worktree create <branch> [--base <ref>\|--stack-on <branch>]` creates branch + worktree and registers it |
| FR-70 | Default behavior: new branch is stacked on the **current branch** unless `--base` names the trunk |
| FR-71 | New `branches` table persists the stack: `owner_repo, branch, parent_branch, parent_base_sha, worktree_path, owner_identity (ATM_IDENTITY), created_at` |
| FR-72 | `continuity worktree list` shows each worktree with stack position, ahead/behind **relative to its parent** (not trunk), PR state, owner |
| FR-73 | `continuity worktree remove <branch>` refuses if the branch has live children unless `--reparent` (children re-parented to this branch's parent) |
| FR-74 | `continuity stack show [<branch>]` renders the stack tree |
| FR-75 | PR creation for a stacked branch targets `parent_branch` as base via the forge adapter (GitHub and ADO both support arbitrary target branches) |
| FR-76 | Stack operations use **plain git only** (`rebase --onto`, `push --force-with-lease`); no `gh stack`, jj, GitButler, or forge-specific stacking APIs |
| FR-77 | `parent_base_sha` (the parent commit the branch was last rebased onto) is recorded on create and after every restack — this is what makes `rebase --onto` exact |
| NF-22 | Worktree paths follow a single configurable template (e.g. `<repo>-worktrees/<branch>`); ad-hoc paths are discouraged by a warning, not blocked |

### Interop

Agents may continue to use `git worktree` or `wt`. An optional
`continuity worktree adopt <path>` (post-MVP) registers an existing worktree and
infers its parent. A `wt` `post-start` hook calling `continuity worktree adopt`
is a documented integration, not a dependency.

---

## 5. U3 — Cascade Engine

### Definition

Given a stack `trunk ← A ← B ← C`, a change to a lower branch leaves every
branch above it built on stale commits. A **cascade** restacks the branches
above, in topological order.

### Two Triggers

| Trigger | Example | Action |
|---|---|---|
| **Push-triggered** | Agent pushes a fix to A | Rebase B onto new A (`git rebase --onto A <B.parent_base_sha> B`), then C onto new B; `push --force-with-lease` each |
| **Merge-triggered** | A merges (often squash) into trunk | Retarget B's PR base to trunk via forge API; `git rebase --onto trunk <B.parent_base_sha> B`; update `branches.parent_branch = trunk`; continue up the stack |

**Change from Sep 2026 design:** merge-triggered cascades were previously
considered GitHub's job (server-side, via native stacked PRs / `gh stack`).
Without `gh stack`, and on ADO, nothing does this automatically — Continuity
must own both triggers.

### Requirements

| ID | Requirement |
|---|---|
| FR-78 | Cascades run in a **Continuity-owned clone**, never in an agent's worktree |
| FR-79 | Push-triggered cascade starts on push detection (post-push hook wake or poll) for any branch with registered children |
| FR-80 | Merge-triggered cascade starts when a PR transitions to MERGED and its branch has registered children |
| FR-81 | Restack order is topological; a failure halts the cascade at that branch and leaves branches above it untouched |
| FR-82 | On conflict: record `cascade_events` row (status=CONFLICT), route to the git agent via ATM per ADR 001 (fix or escalate to branch owner) |
| FR-83 | On success: record `cascade_events`, update `parent_base_sha`, enqueue a deferred nudge (U4) for each affected branch owner |
| FR-84 | Per-stack lock: only one cascade per stack at a time; concurrent triggers coalesce |
| FR-85 | Pushes use `--force-with-lease=<branch>:<expected_sha>` — compatible with FR-21 (blocks `--force`, allows `--force-with-lease`) |
| FR-86 | `git rerere` enabled in the cascade clone |
| FR-87 | `continuity cascade status [<branch>]` / `continuity cascade log` read SQLite only |
| NF-23 | `cascade_events` is append-only (same rule as `ci_events`) |
| NF-24 | A cascade never rewrites a branch whose remote head moved since the cascade began (lease protects; record ABORTED_STALE) |

---

## 6. U4 — Deferred "Pull When Convenient" Nudge

| ID | Requirement |
|---|---|
| FR-88 | After a successful restack, the branch owner (`branches.owner_identity`) receives a **deferred** nudge via the ATM task queue — not an immediate interrupting message |
| FR-89 | Nudge payload: branch, old head, new head, reason (push- or merge-cascade), and the exact command to sync (`git fetch && git reset --keep origin/<branch>` or equivalent) |
| FR-90 | Nudges for the same branch coalesce — only the latest restack is delivered |
| FR-91 | If the owner has unpushed local commits on the branch, nudge content says so and recommends `git rebase origin/<branch>` instead of reset |
| FR-92 | Owner unknown or not in roster → fallback per ADR 001 (`team-lead`) |
| NF-25 | Nudge delivery is fire-and-forget; failure never blocks the cascade (consistent with NF-18) |

**Open question:** confirm the ATM task-queue interface (enqueue API, "deliver
when idle" semantics, coalescing key). Requirements above assume it supports
deferral and keyed replacement.

---

## 7. U5 — Forge Abstraction (GitHub + Azure DevOps MVP)

### Requirements

| ID | Requirement |
|---|---|
| FR-93 | A `Forge` interface in core with adapters; core has no forge-specific code (extends NF-02) |
| FR-94 | Forge interface covers: batch poll (PRs + checks + mergeability), create PR (with base), retarget PR base, PR merged/closed detection, rate-limit/budget readout |
| FR-95 | **GitHub adapter:** GraphQL batch per account (existing v3 design) |
| FR-96 | **Azure DevOps adapter:** REST (pull requests, PR statuses/policy evaluations, builds); batched per organization/project where the API allows |
| FR-97 | Forge resolved per repo from remote URL host, overridable in `repos` table (`forge` column) |
| FR-98 | Auth: GitHub via `gh auth token --account`; ADO via `az` CLI token or PAT from OS keychain. Tokens in memory only (NF-13 applies) |
| FR-99 | `repos.gh_account` generalizes to `forge_account` |

### Post-MVP Forge List

GitLab (`glab`), Gitea/Forgejo (`tea`), Bitbucket.

**Open question:** ADO rate limiting is usage-based (TSTU) with its own
throttling response headers, not a points budget like GitHub GraphQL. The ADO
budget adapter (U7) must be designed against the current ADO docs — to be
verified before implementation.

---

## 8. U6 — Caching and Poll Suppression

All MVP.

| ID | Requirement |
|---|---|
| FR-100 | Per-repo poll timers include deterministic jitter (hash of `owner_repo`) so repos and multiple daemons don't align |
| FR-101 | **Negative caching:** forge errors (network, 5xx, throttled) are recorded and suppress re-polling that repo for a backoff window, rather than retrying every cycle |
| FR-102 | Branches with no upstream / never pushed are excluded from PR lookups |
| FR-103 | Branch-level CI state is invalidated when the local or remote HEAD SHA changes (push wakes poll immediately; FR-33) |
| FR-104 | SHA-keyed git computations (merge-base, ahead/behind, conflict probes) cached by commit pair with no TTL, LRU-bounded |
| FR-105 | Repos with no open PRs and no registered stacked branches drop to the slowest timer (extends per-repo timer design) |
| NF-26 | Cache state lives in SQLite (not side-files); `continuity cache clear [--repo]` resets it |

---

## 9. U7 — Budget Forecasting and Attribution

### Context

LOW_WATER is a fixed threshold that reacts after the fact. With many repos,
two forges, and multiple agents (some calling `gh`/`az` directly), we need to
**predict** exhaustion and **identify who is spending**.

### Requirements

| ID | Requirement |
|---|---|
| FR-106 | Per-account **budget** configured as a fraction of the forge limit (e.g. Continuity may use ≤ 60% of GitHub GraphQL points per window) |
| FR-107 | **Forecast:** after each poll, project spend to window reset from recent burn rate; if projected spend > budget, stretch timers proportionally *before* LOW_WATER is reached |
| FR-108 | LOW_WATER remains as a hard floor (FR-36 unchanged) |
| FR-109 | **Attribution — daemon spend:** each batched query's cost is apportioned to repos by returned node count; stored in `api_usage_by_repo` |
| FR-110 | **Attribution — agent spend:** the `gh`/`git` interceptor (Phase 1) records caller identity (`ATM_IDENTITY`, `ATM_TEAM`) on every `cli_events` row |
| FR-111 | `continuity usage [--by account\|repo\|team\|agent] [--window <dur>]` reports consumption, top spenders, and forecast |
| FR-112 | `budget_pace` trigger event (Warn) when forecast exceeds budget; payload names the top offending repo/team/agent |
| FR-113 | Forecast shown in statusline (FR-68) only when at risk |
| NF-27 | Forecasting is computed from SQLite; no additional API calls |

**Note:** agent-side `gh` calls made outside the interceptor are invisible to
attribution; GitHub's `rateLimit.remaining` will still reflect them, so the
report shows an "unattributed" bucket = observed spend − attributed spend.

---

## 10. Schema Additions (summary)

```sql
-- U2
CREATE TABLE branches (
  id INTEGER PRIMARY KEY,
  owner_repo TEXT NOT NULL,
  branch TEXT NOT NULL,
  parent_branch TEXT,            -- NULL = trunk
  parent_base_sha TEXT,
  worktree_path TEXT,
  owner_identity TEXT,           -- ATM_IDENTITY at create time
  created_at INTEGER NOT NULL,
  UNIQUE(owner_repo, branch)
);

-- U3 (append-only)
CREATE TABLE cascade_events (
  id INTEGER PRIMARY KEY,
  cascade_id TEXT NOT NULL,
  owner_repo TEXT NOT NULL,
  branch TEXT NOT NULL,
  trigger TEXT NOT NULL,         -- PUSH | MERGE
  status TEXT NOT NULL,          -- STARTED | RESTACKED | CONFLICT | ABORTED_STALE | DONE
  old_sha TEXT, new_sha TEXT,
  recorded_at INTEGER NOT NULL
);

-- U5
ALTER TABLE repos ADD COLUMN forge TEXT;          -- github | azure-devops
-- repos.gh_account → forge_account (migration)

-- U7
CREATE TABLE api_usage_by_repo (
  id INTEGER PRIMARY KEY,
  api_usage_id INTEGER NOT NULL REFERENCES api_usage(id),
  owner_repo TEXT NOT NULL,
  cost_share REAL NOT NULL
);
ALTER TABLE cli_events ADD COLUMN atm_identity TEXT;
ALTER TABLE cli_events ADD COLUMN atm_team TEXT;
```

---

## 11. Proposed ADRs

| ADR | Decision | Rationale |
|---|---|---|
| 002 | **Native stack management; no `gh stack` dependency** | Forge-neutral (ADO MVP); removes a separately-pinned extension; Continuity already owns state. Supersedes the Sep 2026 gh-stack choice for Continuity |
| 003 | **Continuity owns both push- and merge-triggered cascades** | Without `gh stack`/native GitHub stacks, and on ADO, no server-side retarget/restack exists |
| 004 | **Forge trait with GitHub + ADO adapters in MVP** | Radiant work is on ADO; core stays forge-agnostic |
| 005 | **Statusline is a pure SQLite read; tmux is the cross-harness surface** | Claude Code supports command statuslines; Codex does not (fixed item list). tmux works for all |
| 006 | **Budget forecasting with spend attribution** | Fixed LOW_WATER reacts too late; attribution is required to act on overspend |
| 007 | **No dependency on the `worktrunk` crate; CLI-boundary interop only** | Library API declared unstable; needed code is binary-only; high MSRV and churn |

---

## 12. Open Questions

1. **Implementation language** — v3 says Rust; `develop` has a Python
   implementation; Go was discussed (Sep 2026). Not decided here.
2. **sc-runtime** — should U2/U3 state and the cascade worker live on
   sc-runtime primitives (daemon + SQLite + RPC)?
3. **ATM task queue** — confirm deferral and coalescing semantics (U4).
4. **ADO rate-limit model** — verify current throttling headers/limits (U5, U7).
5. **Squash-merge detection** for merge-triggered cascade — rely on PR MERGED
   state + recorded `parent_base_sha` (preferred) vs. patch-id matching.
6. **Default stacking (FR-70)** — stack on current branch by default, or
   require explicit `--stack-on`? Defaulting to current branch is convenient
   but can surprise when run from an unrelated feature branch.

---

## 13. Post-MVP

- GitLab, Gitea/Forgejo, Bitbucket adapters
- `continuity worktree adopt` + `wt` hook integration
- Activity-aware nudge delivery (deliver only when agent is idle, using
  harness activity markers)
- Codex native statusline, if/when Codex adds command-backed statuslines
