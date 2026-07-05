"""Database schema and connection management for continuity.

Public API:
    SCHEMA        — all CREATE TABLE statements
    ensure_db()   — open/create database, run migrations, return connection
"""

import sqlite3
import sys
from pathlib import Path

SCHEMA = """
    -- Phase 1: raw audit log (Phase 3 adds blocked column)
    CREATE TABLE IF NOT EXISTS cli_events (
        id          INTEGER PRIMARY KEY,
        command     TEXT    NOT NULL,
        args_json   TEXT    NOT NULL,
        exit_code   INTEGER,
        duration_ms INTEGER,
        blocked     INTEGER DEFAULT 0,
        recorded_at INTEGER NOT NULL
    );

    -- Phase 2: tracked repos (matches design doc)
    CREATE TABLE IF NOT EXISTS repos (
        id              INTEGER PRIMARY KEY,
        owner_repo      TEXT    UNIQUE NOT NULL,
        gh_account      TEXT    NOT NULL,
        provider        TEXT    DEFAULT 'github',
        designated_member TEXT,
        last_synced     INTEGER,
        avg_ci_duration INTEGER,
        max_ci_duration INTEGER
    );

    -- Phase 2: PR state (matches design doc)
    CREATE TABLE IF NOT EXISTS pull_requests (
        id          INTEGER PRIMARY KEY,
        owner_repo  TEXT    NOT NULL,
        pr_number   INTEGER NOT NULL,
        branch      TEXT    NOT NULL,
        head_sha    TEXT,
        mergeable   TEXT,
        state       TEXT,
        updated_at  INTEGER,
        UNIQUE(owner_repo, pr_number)
    );

    -- Phase 2: immutable CI event log (matches design doc)
    CREATE TABLE IF NOT EXISTS ci_events (
        id          INTEGER PRIMARY KEY,
        owner_repo  TEXT    NOT NULL,
        pr_number   INTEGER NOT NULL,
        job_name    TEXT    NOT NULL,
        status      TEXT    NOT NULL,
        conclusion  TEXT,
        recorded_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ci_lookup
        ON ci_events(owner_repo, pr_number, job_name, recorded_at DESC);

    -- Phase 4: API usage tracking (polling daemon)
    CREATE TABLE IF NOT EXISTS api_usage (
        id          INTEGER PRIMARY KEY,
        gh_account  TEXT    NOT NULL,
        queried_at  INTEGER NOT NULL,
        cost        INTEGER NOT NULL,
        remaining   INTEGER NOT NULL,
        reset_at    TEXT    NOT NULL
    );
"""


def ensure_db(db_path: Path) -> sqlite3.Connection:
    """Open/create database at db_path, run schema migrations, return connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=2000")
    db.executescript(SCHEMA)
    _run_migrations(db)
    db.commit()
    return db


def _run_migrations(db: sqlite3.Connection) -> None:
    """Apply schema migrations for existing databases."""
    # Migration 001: add designated_member column to repos
    cols = [row[1] for row in db.execute("PRAGMA table_info(repos)").fetchall()]
    if "designated_member" not in cols:
        db.execute("ALTER TABLE repos ADD COLUMN designated_member TEXT")
