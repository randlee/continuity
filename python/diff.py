"""State diffing engine. Pure functions — no I/O, no side effects.

Compares incoming poll data against current DB state and produces
lists of changed events. Only changes are emitted.

Public API:
    diff_jobs(incoming, current)  → list[CiEvent]
    diff_prs(incoming, current)   → PrDiff
"""

from dataclasses import dataclass, field

from gh.client import PrSnapshot, CheckRun


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CiEvent:
    """An immutable CI state transition to be recorded."""
    owner_repo: str
    pr_number: int
    job_name: str
    status: str
    conclusion: str | None = None


@dataclass
class PrState:
    """Current PR state from the database."""
    number: int
    state: str = "OPEN"
    mergeable: str = "UNKNOWN"


@dataclass
class PrDiff:
    """Result of diffing incoming PRs against current state."""
    added: list[PrState] = field(default_factory=list)
    updated: list[PrState] = field(default_factory=list)
    closed: list[PrState] = field(default_factory=list)


# Status mapping: incoming CheckRun statuses → canonical CiEvent statuses
STATUS_MAP = {
    "QUEUED": "QUEUED",
    "IN_PROGRESS": "IN_PROGRESS",
    "COMPLETED": "COMPLETED",
    "PENDING": "QUEUED",
    "REQUESTED": "QUEUED",
    "WAITING": "QUEUED",
}


# ═══════════════════════════════════════════════════════════════════════════
# Job diffing (FR-29, FR-30)
# ═══════════════════════════════════════════════════════════════════════════

def diff_jobs(
    incoming: list[tuple[str, int, list[CheckRun]]],  # (owner_repo, pr_number, checks)
    current: dict[tuple[str, int, str], CiEvent],     # (repo, pr, job_name) → last event
) -> list[CiEvent]:
    """Compare incoming poll checks against current DB state.

    Returns only jobs where status or conclusion differs from the
    latest recorded event. Identical polls return an empty list.
    """
    events: list[CiEvent] = []
    for owner_repo, pr_number, checks in incoming:
        for check in checks:
            key = (owner_repo, pr_number, check.name)
            last = current.get(key)

            status = _map_status(check.status)
            conclusion = check.conclusion.upper() if check.conclusion else None

            changed = (
                last is None
                or last.status != status
                or last.conclusion != conclusion
            )
            if changed:
                events.append(CiEvent(
                    owner_repo=owner_repo,
                    pr_number=pr_number,
                    job_name=check.name,
                    status=status,
                    conclusion=conclusion,
                ))
    return events


def _map_status(raw: str) -> str:
    """Map GitHub status to canonical CiEvent status."""
    return STATUS_MAP.get(raw.upper(), raw.upper())


# ═══════════════════════════════════════════════════════════════════════════
# PR diffing (FR-37)
# ═══════════════════════════════════════════════════════════════════════════

def diff_prs(
    incoming: list[PrSnapshot],
    current: dict[int, PrState],
) -> PrDiff:
    """Compare incoming PRs against current DB state.

    Returns added, updated, and closed PRs.
    Use diff_conflicts() for mergeability change detection (FR-37).
    """
    diff = PrDiff()
    incoming_numbers = set()

    for pr in incoming:
        incoming_numbers.add(pr.number)
        existing = current.get(pr.number)

        if existing is None:
            diff.added.append(PrState(
                number=pr.number, state=pr.state, mergeable=pr.mergeable,
            ))
        elif existing.state != pr.state or existing.mergeable != pr.mergeable:
            diff.updated.append(PrState(
                number=pr.number, state=pr.state, mergeable=pr.mergeable,
            ))

    # PRs in current but not in incoming → closed
    for num in current:
        if num not in incoming_numbers:
            diff.closed.append(current[num])

    return diff


def diff_conflicts(
    owner_repo: str,
    incoming: list[PrSnapshot],
    current: dict[int, PrState],
) -> list[tuple[str, int]]:  # (owner_repo, pr_number)
    """FR-37: detect PRs where mergeable switched to CONFLICTING."""
    conflicts = []
    for pr in incoming:
        existing = current.get(pr.number)
        if existing and existing.mergeable != "CONFLICTING" and pr.mergeable == "CONFLICTING":
            conflicts.append((owner_repo, pr.number))
    return conflicts