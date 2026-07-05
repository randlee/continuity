"""Dangerous command blocking. Runs BEFORE delegation.

Public API:
    check_dangerous(command, args) → str | None
        Returns block message if command should be blocked, None if safe.
        Respects CONTINUITY_ALLOW_DANGEROUS=1 override.
"""

import os

# Patterns: (command, subcommand, ...) -> block message
_PATTERNS: dict[tuple[str, ...], str] = {
    ("gh", "pr", "merge"): (
        "continuity: pr merge blocked. Use --auto flag or review via web UI.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 gh pr merge ...\n"
    ),
    ("gh", "repo", "delete"): (
        "continuity: repo delete blocked. Use GitHub web UI to delete repos.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 gh repo delete ...\n"
    ),
    ("gh", "api"): (
        "continuity: destructive gh api call blocked.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 gh api ...\n"
    ),
    ("git", "push", "--force"): (
        "continuity: force push blocked. Use --force-with-lease instead.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 git push --force ...\n"
    ),
    ("git", "push", "-f"): (
        "continuity: force push blocked. Use --force-with-lease instead.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 git push -f ...\n"
    ),
    ("git", "branch", "-D"): (
        "continuity: force delete branch blocked.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 git branch -D ...\n"
    ),
    ("git", "push", "--delete"): (
        "continuity: delete remote branch blocked.\n"
        "Override: CONTINUITY_ALLOW_DANGEROUS=1 git push --delete ...\n"
    ),
}


def _check_api_method(args: list[str]) -> str | None:
    """Check if gh api call uses a destructive HTTP method."""
    for i, a in enumerate(args):
        if a in ("--method", "-X") and i + 1 < len(args):
            if args[i + 1].upper() in ("DELETE", "PATCH", "PUT"):
                return _PATTERNS[("gh", "api")]
    return None


def check_dangerous(command: str, args: list[str]) -> str | None:
    """Return block message if command is dangerous, None if safe.
    Respects CONTINUITY_ALLOW_DANGEROUS=1 override."""
    if os.environ.get("CONTINUITY_ALLOW_DANGEROUS") == "1":
        return None

    if not args:
        return None

    # Gh commands: strip -R <repo> prefix, then check patterns
    if command == "gh":
        idx = 0
        while idx < len(args) - 1 and args[idx] == "-R":
            idx += 2
        effective = args[idx:] if idx < len(args) else []

        for depth in range(1, min(len(effective) + 1, 4)):
            pattern = (command,) + tuple(effective[:depth])
            if pattern in _PATTERNS:
                if pattern == ("gh", "pr", "merge") and "--auto" in effective:
                    return None
                if pattern == ("gh", "api"):
                    return _check_api_method(effective)
                return _PATTERNS[pattern]

        if effective and effective[0] == "api":
            return _check_api_method(effective)
        return None

    # Git commands: strip -C <path> prefix, then check patterns + flags
    if command == "git":
        # Skip -C <path> prefix (git -C /some/path push ...)
        idx = 0
        while idx < len(args) - 1 and args[idx] == "-C":
            idx += 2
        effective = args[idx:] if idx < len(args) else []

        for pattern, msg in _PATTERNS.items():
            if pattern[0] != "git":
                continue
            pat_args = list(pattern[1:])
            if len(effective) >= len(pat_args):
                if all(effective[i] == pat_args[i] for i in range(len(pat_args))):
                    return msg

        if effective and effective[0] == "push" and "--force-with-lease" not in effective:
            for a in effective:
                if a in ("--force", "-f"):
                    return _PATTERNS[("git", "push", "--force")]

        if effective and effective[0] == "push" and "--delete" in effective:
            return _PATTERNS[("git", "push", "--delete")]

    return None
