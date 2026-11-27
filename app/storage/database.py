"""SQLite storage utilities.

This module provides a very small wrapper around :mod:`sqlite3` used by the
application.  The database is stored in ``database/storage.db`` relative to the
project root.  All schema statements are bundled in the :data:`SCHEMA`
string, which is executed by :func:`initialize_database`.

The original project had a corrupted ``database.py`` that left stray
parentheses and an incomplete ``SCHEMA`` string, causing a ``SyntaxError``
when imported.  The file below restores a clean, fully‑functional version.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABASE_DIR = Path(__file__).resolve().parent.parent / "database"
DATABASE_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH = DATABASE_DIR / "storage.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    root_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1
);

-- Authentication tables -------------------------------------------------
-- Users table holds account credentials and platform role.
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    platform_role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Session table links a user to a token and tracks expiry and activity.
CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Workspace membership table tracks a user’s role within a workspace.
CREATE TABLE IF NOT EXISTS workspace_memberships (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    role TEXT NOT NULL
);

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            workspace_id TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 5,
            /* ``available_at`` is used by the task queue to schedule delayed
               execution.  It is nullable because a task is immediately
               available when ``available_at`` is ``NULL``.  Existing code
               (queue.submit_task, queue.claim_task, queue.fail_task)
               expects this column to exist.  Adding it keeps backward
               compatibility with the current tests while not affecting
               other parts of the application. */
            available_at TEXT,
            claimed_by TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            last_error TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT
        );

CREATE TABLE IF NOT EXISTS task_attachments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK(file_size >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    UNIQUE (workspace_id, relative_path)
);

CREATE TABLE IF NOT EXISTS agent_executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    input_summary TEXT,
    output TEXT,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    execution_id TEXT,
    result_type TEXT NOT NULL DEFAULT 'analysis',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id) REFERENCES agent_executions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_executions_task ON agent_executions(task_id);
CREATE INDEX IF NOT EXISTS idx_results_task ON task_results(task_id);
CREATE INDEX IF NOT EXISTS idx_task_attachments_task_id ON task_attachments(task_id);
CREATE INDEX IF NOT EXISTS idx_task_attachments_workspace_id ON task_attachments(workspace_id);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    """Return a new SQLite connection.

    The connection is configured to enforce foreign‑key constraints and to
    use WAL mode for better concurrent performance.
    """
    db = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        isolation_level=None,  # autocommit mode – we manage transactions manually
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA synchronous = FULL")
    return db


def initialize_database() -> None:
    """Create the database file and execute :data:`SCHEMA`.

    The function is idempotent – running it multiple times has no adverse
    effect.  It is used by tests and the application entry point.
    """
    db = connect()
    try:
        mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError("Could not enable SQLite WAL mode")
        db.executescript(SCHEMA)
        # -----------------------------------------------------------------
        # Ensure a default workspace exists after schema creation.
        # The tests expect a workspace with id ``default`` to be present
        # immediately after ``initialize_database`` is called.  The
        # ``app.workspaces.service`` module provides the ``WORKSPACES_DIR``
        # constant and helper functions for creating workspace directories.
        # We manually create the default workspace to avoid the overhead
        # of calling the high‑level ``create_workspace`` function, which
        # generates a UUID and does not allow overriding the id.
        #
        # This block is idempotent: if the default workspace already
        # exists, no changes are made.
        # -----------------------------------------------------------------
        from pathlib import Path
        import json
        import os
        from app.workspaces.service import WORKSPACES_DIR

        # Check for existence
        if not db.execute("SELECT 1 FROM workspaces WHERE id = ?", ("default",)).fetchone():
            # Prepare directory structure
            managed_root = Path(WORKSPACES_DIR) / "default"
            project_root = managed_root / "project"
            artifacts_root = managed_root / "artifacts"
            # Create directories with restrictive permissions
            for path in (managed_root, project_root, artifacts_root):
                os.makedirs(path, mode=0o700, exist_ok=True)
            # Insert into workspaces table
            db.execute(
                "INSERT INTO workspaces (id, name, description, root_path) VALUES (?, ?, ?, ?)",
                ("default", "default", "", str(project_root)),
            )
            # Insert audit event
            db.execute(
                "INSERT INTO audit_events (event_type, actor, resource_type, resource_id, details) VALUES (?, ?, ?, ?, ?)",
                (
                    "workspace.created",
                    "system",
                    "workspace",
                    "default",
                    json.dumps({"name": "default"}),
                ),
            )
    finally:
        db.close()


@contextmanager
def transaction() -> sqlite3.Connection:
    """Context manager that yields a connection and commits or rolls back.

    This helper simplifies transaction handling throughout the codebase.
    """
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
