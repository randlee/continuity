"""Shared constants for continuity.

Single source of truth for CI status strings, PR states, and
mergeable values used across multiple modules.

Import from here instead of scattering string literals.
"""

# CI job statuses (canonical)
STATUS_QUEUED = "QUEUED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_PENDING = "PENDING"

# CI job conclusions
CONCLUSION_SUCCESS = "SUCCESS"
CONCLUSION_FAILURE = "FAILURE"
CONCLUSION_CANCELLED = "CANCELLED"
CONCLUSION_TIMED_OUT = "TIMED_OUT"
CONCLUSION_SKIPPED = "SKIPPED"
CONCLUSION_CONFLICT = "CONFLICT"

# PR states
PR_STATE_OPEN = "OPEN"
PR_STATE_MERGED = "MERGED"
PR_STATE_CLOSED = "CLOSED"

# PR mergeable states
MERGEABLE_MERGEABLE = "MERGEABLE"
MERGEABLE_CONFLICTING = "CONFLICTING"
MERGEABLE_UNKNOWN = "UNKNOWN"

# ATM identities
ATM_TEAM_LEAD = "team-lead"
ATM_CI_IDENTITY = "ci"

# Active terminal statuses
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED})
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_IN_PROGRESS})

# Status mapping: incoming GitHub statuses → canonical statuses
STATUS_MAP = {
    "QUEUED": STATUS_QUEUED,
    "IN_PROGRESS": STATUS_IN_PROGRESS,
    "COMPLETED": STATUS_COMPLETED,
    "PENDING": STATUS_QUEUED,
    "REQUESTED": STATUS_QUEUED,
    "WAITING": STATUS_QUEUED,
}
