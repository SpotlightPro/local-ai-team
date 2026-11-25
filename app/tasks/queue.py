import json
from contextlib import closing
import sqlite3
import uuid

from datetime import datetime, timedelta, timezone

from app.storage.database import connect


LEASE_HOURS = 2


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat(timespec="seconds")


def submit_task(
    title,
    description,
    priority=5,
    workspace_id=None,
    actor_id=None,
):
    task_id = str(uuid.uuid4())
    now = timestamp(utc_now())
    workspace_id = workspace_id or "default"

    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")

        from app.workspaces.service import require_workspace

        require_workspace(workspace_id, db)

        # Authenticated submissions must have permission to create tasks.
        if actor_id is not None:
            from app.auth.service import require_workspace_access

            require_workspace_access(
                actor_id,
                workspace_id,
                allowed_roles={
                    "administrator",
                    "manager",
                    "member",
                },
                db=db,
            )

        db.execute("""
            INSERT INTO tasks (
                id, workspace_id, title, description,
                status, priority, available_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (
            task_id,
            workspace_id,
            title,
            description,
            priority,
            now,
        ))

        db.execute("""
            INSERT INTO audit_events (
                event_type, actor, resource_type,
                resource_id, details
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            "task.submitted",
            actor_id or "system",
            "task",
            task_id,
            json.dumps({"title": title}),
        ))

        db.commit()

    return task_id

    
def claim_task(worker_id):
    now = utc_now()
    now_text = timestamp(now)

    lease = timestamp(
        now + timedelta(hours=LEASE_HOURS)
    )

    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")

        # Permanently fail jobs that exhausted their retries.
        db.execute("""
            UPDATE tasks
            SET status = 'failed',
                claimed_by = NULL,
                lease_expires_at = NULL,
                last_error = COALESCE(
                    last_error,
                    'Execution lease expired; retry limit reached'
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
              AND lease_expires_at <= ?
              AND attempt_count >= max_attempts
        """, (now_text,))

        task = db.execute("""
            SELECT *
            FROM tasks
            WHERE attempt_count < max_attempts
              AND (
                  (status = 'pending' AND
                   (available_at IS NULL OR available_at <= ?))
                  OR
                  (status = 'running' AND
                   lease_expires_at <= ?)
              )
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
        """, (now_text, now_text)).fetchone()

        if task is None:
            db.commit()
            return None

        db.execute("""
            UPDATE tasks
            SET status = 'running',
                claimed_by = ?,
                lease_expires_at = ?,
                attempt_count = attempt_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (worker_id, lease, task["id"]))

        db.execute("""
            INSERT INTO audit_events (
                event_type, actor, resource_type,
                resource_id, details
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            "task.claimed",
            worker_id,
            "task",
            task["id"],
            json.dumps({
                "attempt": task["attempt_count"] + 1
            })
        ))

        db.commit()

        return dict(task) | {
            "attempt_count": task["attempt_count"] + 1,
            "claimed_by": worker_id,
            "status": "running",
        }

def renew_task_lease(task_id, worker_id):
    """Extend a task lease while the current worker still owns it."""
    now = utc_now()
    now_text = timestamp(now)

    new_expiry = timestamp(
        now + timedelta(hours=LEASE_HOURS)
    )

    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")

        cursor = db.execute("""
            UPDATE tasks
            SET lease_expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'running'
              AND claimed_by = ?
              AND lease_expires_at > ?
        """, (
            new_expiry,
            task_id,
            worker_id,
            now_text,
        ))

        db.commit()

        return cursor.rowcount == 1


def complete_task(task_id, worker_id, results):
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")

        now = timestamp(utc_now())

        task = db.execute("""
            SELECT id
            FROM tasks
            WHERE id = ?
              AND status = 'running'
              AND claimed_by = ?
              AND lease_expires_at > ?
        """, (task_id, worker_id, now)).fetchone()

        if task is None:
            db.rollback()
            raise RuntimeError(
                "Task ownership lost or lease expired"
            )

        for result_type, content in results.items():
            db.execute("""
                INSERT INTO task_results (
                    id, task_id, result_type, content
                )
                VALUES (?, ?, ?, ?)
            """, (
                str(uuid.uuid4()),
                task_id,
                result_type,
                content
            ))

        db.execute("""
            UPDATE tasks
            SET status = 'completed',
                claimed_by = NULL,
                lease_expires_at = NULL,
                last_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND claimed_by = ?
        """, (task_id, worker_id))

        db.execute("""
            INSERT INTO audit_events (
                event_type, actor, resource_type,
                resource_id, details
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            "task.completed",
            worker_id,
            "task",
            task_id,
            "{}"
        ))

        db.commit()


def fail_task(task_id, worker_id, error):
    now = utc_now()
    now_text = timestamp(now)

    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE")

        task = db.execute("""
            SELECT attempt_count, max_attempts
            FROM tasks
            WHERE id = ?
              AND status = 'running'
              AND claimed_by = ?
              AND lease_expires_at > ?
        """, (
            task_id,
            worker_id,
            now_text,
        )).fetchone()

        if task is None:
            db.rollback()
            return

        retry = (
            task["attempt_count"] < task["max_attempts"]
        )

        delay = min(
            300,
            10 * (2 ** (task["attempt_count"] - 1))
        )

        available = timestamp(
            now + timedelta(seconds=delay)
        )

        db.execute("""
            UPDATE tasks
            SET status = ?,
                available_at = ?,
                claimed_by = NULL,
                lease_expires_at = NULL,
                last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND claimed_by = ?
        """, (
            "pending" if retry else "failed",
            available,
            error[:4000],
            task_id,
            worker_id,
        ))

        db.execute("""
            INSERT INTO audit_events (
                event_type, actor, resource_type,
                resource_id, details
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            "task.retry_scheduled" if retry else "task.failed",
            worker_id,
            "task",
            task_id,
            json.dumps({"error": error[:1000]})
        ))

        db.commit()



def get_task(task_id, workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        task = db.execute("""
            SELECT *
            FROM tasks
            WHERE id = ? AND workspace_id = ?
        """, (task_id, workspace_id)).fetchone()

        if task is None:
            return None

        results = db.execute("""
            SELECT r.result_type, r.content, r.created_at
            FROM task_results r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.task_id = ?
              AND t.workspace_id = ?
            ORDER BY r.created_at
        """, (task_id, workspace_id)).fetchall()

        return {
            "task": dict(task),
            "results": [dict(row) for row in results]
        }


def list_tasks(workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        rows = db.execute("""
            SELECT *
            FROM tasks
            WHERE workspace_id = ?
            ORDER BY created_at DESC
        """, (workspace_id,)).fetchall()

        return [dict(row) for row in rows]


def list_agent_executions(workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        rows = db.execute("""
            SELECT e.*, t.workspace_id
            FROM agent_executions e
            JOIN tasks t ON t.id = e.task_id
            WHERE t.workspace_id = ?
            ORDER BY e.started_at DESC
        """, (workspace_id,)).fetchall()

        return [dict(row) for row in rows]


def get_agent_execution(execution_id, workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        row = db.execute("""
            SELECT e.*, t.workspace_id
            FROM agent_executions e
            JOIN tasks t ON t.id = e.task_id
            WHERE e.id = ?
              AND t.workspace_id = ?
        """, (execution_id, workspace_id)).fetchone()

        return dict(row) if row else None


def get_task_results(task_id, workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        task = db.execute("""
            SELECT id
            FROM tasks
            WHERE id = ? AND workspace_id = ?
        """, (task_id, workspace_id)).fetchone()

        if task is None:
            return []

        rows = db.execute("""
            SELECT r.*
            FROM task_results r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.task_id = ?
              AND t.workspace_id = ?
            ORDER BY r.created_at
        """, (task_id, workspace_id)).fetchall()

        return [dict(row) for row in rows]


def list_workspace_audit(workspace_id="default"):
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        rows = db.execute("""
            SELECT a.*
            FROM audit_events a
            WHERE
                (
                    a.resource_type = 'workspace'
                    AND a.resource_id = ?
                )
                OR
                (
                    a.resource_type = 'task'
                    AND EXISTS (
                        SELECT 1
                        FROM tasks t
                        WHERE t.id = a.resource_id
                          AND t.workspace_id = ?
                    )
                )
                OR
                (
                    a.resource_type = 'agent_execution'
                    AND EXISTS (
                        SELECT 1
                        FROM agent_executions e
                        JOIN tasks t ON t.id = e.task_id
                        WHERE e.id = a.resource_id
                          AND t.workspace_id = ?
                    )
                )
                OR
                (
                    a.resource_type = 'task_result'
                    AND EXISTS (
                        SELECT 1
                        FROM task_results r
                        JOIN tasks t ON t.id = r.task_id
                        WHERE r.id = a.resource_id
                          AND t.workspace_id = ?
                    )
                )
            ORDER BY a.created_at DESC, a.id DESC
        """, (
            workspace_id,
            workspace_id,
            workspace_id,
            workspace_id,
        )).fetchall()

        return [dict(row) for row in rows]



def get_task_result(result_id, workspace_id="default"):
    """Retrieve one result only when its parent task belongs to the workspace."""
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        row = db.execute("""
            SELECT r.*
            FROM task_results r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.id = ?
              AND t.workspace_id = ?
        """, (result_id, workspace_id)).fetchone()

        return dict(row) if row else None


def get_workspace_audit_event(event_id, workspace_id="default"):
    """Retrieve one audit event within its workspace ownership boundary."""
    from app.workspaces.service import require_workspace

    with closing(connect()) as db:
        require_workspace(workspace_id, db)

        row = db.execute("""
            SELECT a.*
            FROM audit_events a
            WHERE a.id = ?
              AND (
                  (
                      a.resource_type = 'workspace'
                      AND a.resource_id = ?
                  )
                  OR
                  (
                      a.resource_type = 'task'
                      AND EXISTS (
                          SELECT 1 FROM tasks t
                          WHERE t.id = a.resource_id
                            AND t.workspace_id = ?
                      )
                  )
                  OR
                  (
                      a.resource_type = 'agent_execution'
                      AND EXISTS (
                          SELECT 1
                          FROM agent_executions e
                          JOIN tasks t ON t.id = e.task_id
                          WHERE e.id = a.resource_id
                            AND t.workspace_id = ?
                      )
                  )
                  OR
                  (
                      a.resource_type = 'task_result'
                      AND EXISTS (
                          SELECT 1
                          FROM task_results r
                          JOIN tasks t ON t.id = r.task_id
                          WHERE r.id = a.resource_id
                            AND t.workspace_id = ?
                      )
                  )
              )
        """, (
            event_id,
            workspace_id,
            workspace_id,
            workspace_id,
            workspace_id,
        )).fetchone()

        return dict(row) if row else None
