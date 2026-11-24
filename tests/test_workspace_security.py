import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

from app.tasks import queue
from app.orchestration.collaboration import execute_tracked_agent

from app.storage import database
from app.storage.migrations import migrate
from app.workspaces import service


class WorkspaceSecurityTests(unittest.TestCase):

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

    def create_workspace(self, name="Test Workspace", root=None):
        return service.create_workspace(
            name,
            root_path=root,
        )

    def test_nested_artifact_creation(self):
        workspace = self.create_workspace()

        result = service.write_workspace_artifact(
            workspace["id"],
            "reports/2026/result.md",
            "Security test",
        )

        self.assertTrue(result.is_file())
        self.assertEqual(
            result.read_text(encoding="utf-8"),
            "Security test",
        )

    def test_path_traversal_rejected(self):
        workspace = self.create_workspace()

        with self.assertRaises(ValueError):
            service.write_workspace_artifact(
                workspace["id"],
                "../outside.txt",
                "Should not be written",
            )

    def test_absolute_artifact_path_rejected(self):
        workspace = self.create_workspace()

        with self.assertRaises(ValueError):
            service.write_workspace_artifact(
                workspace["id"],
                "/tmp/outside.txt",
                "Should not be written",
            )

    def test_symbolic_link_directory_rejected(self):
        workspace = self.create_workspace()

        context = service.get_workspace_context(
            workspace["id"]
        )

        outside = self.root / "outside"
        outside.mkdir()

        link = context["artifacts_root"] / "escape"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(OSError):
            service.write_workspace_artifact(
                workspace["id"],
                "escape/attack.txt",
                "Should not escape",
            )

        self.assertFalse((outside / "attack.txt").exists())

    def test_symbolic_link_artifacts_root_rejected(self):
        workspace = self.create_workspace()

        managed = (
            self.root / "workspaces" / workspace["id"]
        )

        artifacts = managed / "artifacts"
        saved = managed / "artifacts-original"

        outside = self.root / "outside"
        outside.mkdir()

        artifacts.rename(saved)
        artifacts.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(OSError):
            service.get_workspace_context(workspace["id"])

    def test_symbolic_link_project_root_rejected(self):
        outside = self.root / "outside-project"
        outside.mkdir()

        link = self.root / "project-link"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(OSError):
            self.create_workspace(root=link)

    def test_existing_artifact_replaced_atomically(self):
        workspace = self.create_workspace()

        first = service.write_workspace_artifact(
            workspace["id"],
            "report.md",
            "First version",
        )

        second = service.write_workspace_artifact(
            workspace["id"],
            "report.md",
            "Second version",
        )

        self.assertEqual(first, second)
        self.assertEqual(
            second.read_text(encoding="utf-8"),
            "Second version",
        )

    def test_workspaces_have_independent_artifacts(self):
        first = self.create_workspace("First")
        second = self.create_workspace("Second")

        first_file = service.write_workspace_artifact(
            first["id"],
            "report.md",
            "First workspace",
        )

        second_file = service.write_workspace_artifact(
            second["id"],
            "report.md",
            "Second workspace",
        )

        self.assertNotEqual(first_file.parent, second_file.parent)

        self.assertEqual(
            first_file.read_text(encoding="utf-8"),
            "First workspace",
        )

        self.assertEqual(
            second_file.read_text(encoding="utf-8"),
            "Second workspace",
        )

    def test_overlapping_project_roots_rejected(self):
        shared = self.root / "shared-project"

        self.create_workspace("First", shared)

        with self.assertRaises(ValueError):
            self.create_workspace(
                "Second",
                shared / "nested",
            )


    def test_task_results_are_workspace_scoped(self):
        first = self.create_workspace("First")
        second = self.create_workspace("Second")

        task_id = queue.submit_task(
            "Isolation test",
            "Test task results",
            workspace_id=first["id"],
        )

        with database.connect() as db:
            db.execute("""
                INSERT INTO task_results (
                    id, task_id, result_type, content
                )
                VALUES (?, ?, ?, ?)
            """, ("result-1", task_id, "analysis", "Private result"))

        owner_result = queue.get_task(task_id, first["id"])
        other_result = queue.get_task(task_id, second["id"])

        self.assertEqual(
            owner_result["results"][0]["content"],
            "Private result",
        )
        self.assertIsNone(other_result)

    def test_agent_execution_rejects_wrong_workspace(self):
        first = self.create_workspace("First")
        second = self.create_workspace("Second")

        task_id = queue.submit_task(
            "Isolation test",
            "Test agent ownership",
            workspace_id=first["id"],
        )

        agent = SimpleNamespace(
            provider=SimpleNamespace(model="test-model"),
            execute=lambda prompt: "Should not execute",
        )

        state = {
            "task_id": task_id,
            "workspace_id": second["id"],
        }

        with self.assertRaisesRegex(
            ValueError,
            "does not belong",
        ):
            execute_tracked_agent(
                state, agent, "Test Agent", "test", "Test prompt"
            )

        with database.connect() as db:
            count = db.execute("""
                SELECT COUNT(*)
                FROM agent_executions
                WHERE task_id = ?
            """, (task_id,)).fetchone()[0]

        self.assertEqual(count, 0)

    def test_valid_agent_execution_is_persisted(self):
        workspace = self.create_workspace("Agent Workspace")

        task_id = queue.submit_task(
            "Execution test",
            "Verify transaction persistence",
            workspace_id=workspace["id"],
        )

        agent = SimpleNamespace(
            provider=SimpleNamespace(model="test-model"),
            execute=lambda prompt: "Successful execution",
        )

        result = execute_tracked_agent(
            {
                "task_id": task_id,
                "workspace_id": workspace["id"],
            },
            agent,
            "Test Agent",
            "test",
            "Test prompt",
        )

        self.assertEqual(result, "Successful execution")

        with database.connect() as db:
            execution = db.execute("""
                SELECT task_id, status, output
                FROM agent_executions
                WHERE task_id = ?
            """, (task_id,)).fetchone()

        self.assertIsNotNone(execution)
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["output"], "Successful execution")



    def test_individual_resources_enforce_workspace_isolation(self):
        from app.auth.service import (
            create_first_admin,
            create_user,
            get_authorized_agent_execution,
            get_authorized_task_result,
            get_authorized_workspace_audit_event,
        )

        first = self.create_workspace("Private Workspace A")
        second = self.create_workspace("Private Workspace B")

        admin_id = create_first_admin(
            "isolation_admin",
            "SecurePassword123!",
        )

        user_id = create_user(
            "workspace_user",
            "SecurePassword456!",
            first["id"],
            "member",
            admin_id,
        )

        task_id = queue.submit_task(
            "Private task",
            "Workspace isolation verification",
            workspace_id=first["id"],
        )

        execution_id = "isolated-execution-1"
        result_id = "isolated-result-1"

        with database.connect() as db:
            db.execute("""
                INSERT INTO agent_executions (
                    id, task_id, agent_name, provider, model,
                    status, output
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                task_id,
                "Analyst",
                "test",
                "test-model",
                "completed",
                "Private execution",
            ))

            db.execute("""
                INSERT INTO task_results (
                    id, task_id, result_type, content
                )
                VALUES (?, ?, ?, ?)
            """, (
                result_id,
                task_id,
                "analysis",
                "Private result",
            ))

            cursor = db.execute("""
                INSERT INTO audit_events (
                    event_type, actor, resource_type,
                    resource_id, details
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                "task.private",
                "test",
                "task",
                task_id,
                "{}",
            ))

            audit_id = cursor.lastrowid
            db.commit()

        # Authorized workspace owner can retrieve each resource.
        self.assertEqual(
            get_authorized_agent_execution(
                user_id, execution_id, first["id"]
            )["id"],
            execution_id,
        )

        self.assertEqual(
            get_authorized_task_result(
                user_id, result_id, first["id"]
            )["id"],
            result_id,
        )

        self.assertEqual(
            get_authorized_workspace_audit_event(
                user_id, audit_id, first["id"]
            )["id"],
            audit_id,
        )

        # Ordinary users cannot access resources through another workspace.
        with self.assertRaises(PermissionError):
            get_authorized_agent_execution(
                user_id, execution_id, second["id"]
            )

        with self.assertRaises(PermissionError):
            get_authorized_task_result(
                user_id, result_id, second["id"]
            )

        with self.assertRaises(PermissionError):
            get_authorized_workspace_audit_event(
                user_id, audit_id, second["id"]
            )

        # Database retrieval must independently enforce ownership.
        self.assertIsNone(
            queue.get_agent_execution(execution_id, second["id"])
        )

        self.assertIsNone(
            queue.get_task_result(result_id, second["id"])
        )

        self.assertIsNone(
            queue.get_workspace_audit_event(audit_id, second["id"])
        )

        # Direct retrieval cannot disclose another workspace's data.
        self.assertIsNone(
            queue.get_agent_execution(execution_id, second["id"])
        )

        self.assertIsNone(
            queue.get_task_result(result_id, second["id"])
        )

        self.assertIsNone(
            queue.get_workspace_audit_event(audit_id, second["id"])
        )

        # Authorization wrappers reject unauthorized workspace access.
        for operation in (
            lambda: get_authorized_agent_execution(
                user_id, execution_id, second["id"]
            ),
            lambda: get_authorized_task_result(
                user_id, result_id, second["id"]
            ),
            lambda: get_authorized_workspace_audit_event(
                user_id, audit_id, second["id"]
            ),
        ):
            with self.assertRaises(PermissionError):
                operation()


if __name__ == "__main__":
    unittest.main()
