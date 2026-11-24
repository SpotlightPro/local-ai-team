from pathlib import Path

from app.storage.database import connect


def migrate():
    base_dir = Path(__file__).resolve().parents[2]

    default_root = (
        base_dir / "workspaces" / "default" / "project"
    ).resolve()

    default_root.mkdir(parents=True, exist_ok=True)

    (
        base_dir / "workspaces" / "default" / "artifacts"
    ).mkdir(parents=True, exist_ok=True)

    db = connect()

    try:
        db.execute("BEGIN IMMEDIATE")

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(tasks)")
        }

        additions = {
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "available_at": "TEXT",
            "claimed_by": "TEXT",
            "lease_expires_at": "TEXT",
            "last_error": "TEXT",
        }

        for name, definition in additions.items():
            if name not in columns:
                db.execute(
                    f"ALTER TABLE tasks ADD COLUMN {name} {definition}"
                )

        db.execute(
            """
            INSERT OR IGNORE INTO workspaces (
                id, name, description, root_path
            )
            VALUES (
                'default',
                'Default Workspace',
                'Default workspace for existing and unassigned tasks',
                ?
            )
            """,
            (str(default_root),),
        )

        db.execute(
            """
            UPDATE workspaces
            SET root_path = ?
            WHERE id = 'default' AND root_path IS NULL
            """,
            (str(default_root),),
        )

        db.execute(
            """
            UPDATE tasks
            SET workspace_id = 'default'
            WHERE workspace_id IS NULL
            """
        )

        # Platform users
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                platform_role TEXT NOT NULL DEFAULT 'user'
                    CHECK (
                        platform_role IN (
                            'platform_admin',
                            'user'
                        )
                    ),
                is_active INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Authentication sessions
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Workspace membership and permissions
        db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                role TEXT NOT NULL
                    CHECK (
                        role IN (
                            'administrator',
                            'manager',
                            'member',
                            'viewer'
                        )
                    ),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, workspace_id),
                FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (workspace_id)
                    REFERENCES workspaces(id) ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_active
            ON users(is_active)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON user_sessions(user_id)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry
            ON user_sessions(expires_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memberships_workspace
            ON workspace_memberships(workspace_id, role)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memberships_user
            ON workspace_memberships(user_id, workspace_id)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_queue
            ON tasks(status, available_at, priority)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace_status
            ON tasks(workspace_id, status, created_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace_queue
            ON tasks(workspace_id, status, available_at, priority)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_executions_task_status
            ON agent_executions(task_id, status)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_results_task_created
            ON task_results(task_id, created_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_resource
            ON audit_events(resource_type, resource_id)
        """)

        db.execute("PRAGMA user_version = 3")
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
