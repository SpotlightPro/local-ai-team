import sqlite3
from contextlib import contextmanager
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_DIR = BASE_DIR / "data" / "database"
DATABASE_PATH = DATABASE_DIR / "ai_team.db"

DATABASE_DIR.mkdir(parents=True, exist_ok=True)


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

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (workspace_id)
        REFERENCES workspaces(id) ON DELETE RESTRICT
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
    FOREIGN KEY (task_id)
        REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    execution_id TEXT,
    result_type TEXT NOT NULL DEFAULT 'analysis',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id)
        REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id)
        REFERENCES agent_executions(id) ON DELETE SET NULL
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

CREATE INDEX IF NOT EXISTS idx_tasks_status
ON tasks(status);

CREATE INDEX IF NOT EXISTS idx_tasks_workspace
ON tasks(workspace_id);

CREATE INDEX IF NOT EXISTS idx_executions_task
ON agent_executions(task_id);

CREATE INDEX IF NOT EXISTS idx_results_task
ON task_results(task_id);

CREATE INDEX IF NOT EXISTS idx_audit_resource
ON audit_events(resource_type, resource_id);
"""


def connect():
    db = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        isolation_level=None
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA synchronous = FULL")
    return db


def initialize_database():
    db = connect()
    try:
        mode = db.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0]

        if mode.lower() != "wal":
            raise RuntimeError("Could not enable SQLite WAL mode")

        db.executescript(SCHEMA)
    finally:
        db.close()


@contextmanager
def transaction():
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
