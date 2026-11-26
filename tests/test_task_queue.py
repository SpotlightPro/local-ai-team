
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from app.storage import database
from app.storage.migrations import migrate
from app.tasks import queue
from app.workspaces import service


class TaskQueueTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.db_patch = patch.object(
            database,
            "DATABASE_PATH",
            self.root / "test.db",
        )
        self.db_patch.start()

        self.workspace_patch = patch.object(
            service,
            "WORKSPACES_DIR",
            self.root / "workspaces",
        )
        self.workspace_patch.start()

        database.initialize_database()
        migrate()

    def tearDown(self):
        self.workspace_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def create_task(self):
        return queue.submit_task(
            "Queue test",
            "Testing background task execution",
        )

    def test_task_can_be_claimed(self):
        task_id = self.create_task()

        task = queue.claim_task("worker-1")

        self.assertIsNotNone(task)
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["claimed_by"], "worker-1")

    def test_task_cannot_be_claimed_twice(self):
        self.create_task()

        first = queue.claim_task("worker-1")
        second = queue.claim_task("worker-2")

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_task_completion_persists_results(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        queue.complete_task(
            task_id,
            "worker-1",
            {"final_report": "Successful result"},
        )

        result = queue.get_task(task_id)

        self.assertEqual(
            result["task"]["status"],
            "completed",
        )

        self.assertEqual(
            result["results"][0]["content"],
            "Successful result",
        )

    def test_wrong_worker_cannot_complete_task(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        with self.assertRaises(RuntimeError):
            queue.complete_task(
                task_id,
                "worker-2",
                {"final_report": "Unauthorized"},
            )

    def test_failed_task_is_scheduled_for_retry(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        queue.fail_task(
            task_id,
            "worker-1",
            "Simulated execution failure",
        )

        result = queue.get_task(task_id)

        self.assertEqual(
            result["task"]["status"],
            "pending",
        )

        self.assertEqual(
            result["task"]["last_error"],
            "Simulated execution failure",
        )

    def test_retry_limit_is_respected(self):
        task_id = self.create_task()

        with database.connect() as db:
            db.execute("""
                UPDATE tasks
                SET max_attempts = 1
                WHERE id = ?
            """, (task_id,))

        queue.claim_task("worker-1")

        queue.fail_task(
            task_id,
            "worker-1",
            "Permanent failure",
        )

        result = queue.get_task(task_id)

        self.assertEqual(
            result["task"]["status"],
            "failed",
        )

    def test_expired_lease_can_be_reclaimed(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        with database.connect() as db:
            db.execute("""
                UPDATE tasks
                SET lease_expires_at =
                    '2000-01-01T00:00:00+00:00'
                WHERE id = ?
            """, (task_id,))

        reclaimed = queue.claim_task("worker-2")

        self.assertEqual(reclaimed["id"], task_id)
        self.assertEqual(reclaimed["claimed_by"], "worker-2")
        self.assertEqual(reclaimed["attempt_count"], 2)

    def test_expired_worker_cannot_complete_task(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        with database.connect() as db:
            db.execute("""
                UPDATE tasks
                SET lease_expires_at =
                    '2000-01-01T00:00:00+00:00'
                WHERE id = ?
            """, (task_id,))

        with self.assertRaisesRegex(
            RuntimeError,
            "lease expired",
        ):
            queue.complete_task(
                task_id,
                "worker-1",
                {"final_report": "Stale result"},
            )

        result = queue.get_task(task_id)

        self.assertEqual(
            result["task"]["status"],
            "running",
        )

        self.assertEqual(result["results"], [])

    def test_expired_worker_cannot_fail_reclaimed_task(self):
        task_id = self.create_task()

        queue.claim_task("worker-1")

        with database.connect() as db:
            db.execute("""
                UPDATE tasks
                SET lease_expires_at =
                    '2000-01-01T00:00:00+00:00'
                WHERE id = ?
            """, (task_id,))

        queue.claim_task("worker-2")

        queue.fail_task(
            task_id,
            "worker-1",
            "Stale worker failure",
        )

        result = queue.get_task(task_id)

        self.assertEqual(
            result["task"]["status"],
            "running",
        )

        self.assertEqual(
            result["task"]["claimed_by"],
            "worker-2",
        )

    def test_task_lease_can_be_renewed(self):
        task_id = self.create_task()
        queue.claim_task("worker-1")

        with database.connect() as db:
            before = db.execute(
                "SELECT lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()["lease_expires_at"]

        renewed = queue.renew_task_lease(
            task_id,
            "worker-1",
        )

        self.assertTrue(renewed)

        with database.connect() as db:
            after = db.execute(
                "SELECT lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()["lease_expires_at"]

        self.assertGreaterEqual(after, before)

    def test_wrong_worker_cannot_renew_lease(self):
        task_id = self.create_task()
        queue.claim_task("worker-1")

        renewed = queue.renew_task_lease(
            task_id,
            "worker-2",
        )

        self.assertFalse(renewed)

    def test_expired_worker_cannot_renew_lease(self):
        task_id = self.create_task()
        queue.claim_task("worker-1")

        with database.connect() as db:
            db.execute("""
                UPDATE tasks
                SET lease_expires_at =
                    '2000-01-01T00:00:00+00:00'
                WHERE id = ?
            """, (task_id,))

        renewed = queue.renew_task_lease(
            task_id,
            "worker-1",
        )

        self.assertFalse(renewed)


if __name__ == "__main__":
    unittest.main()
