
import threading
import unittest
from unittest.mock import patch, Mock

from app.tasks import worker


class WorkerLeaseTests(unittest.TestCase):

    def test_heartbeat_renews_lease(self):
        stop_event = threading.Event()
        lost_event = Mock()

        with patch.object(
            worker,
            "renew_task_lease",
            return_value=True,
        ) as renew, patch.object(
            worker,
            "LEASE_RENEW_INTERVAL",
            0,
        ):
            stop_event.set()

            worker.maintain_task_lease(
                "task-1",
                "worker-1",
                stop_event,
                lost_event,
            )

        renew.assert_not_called()
        lost_event.set.assert_not_called()

    def test_heartbeat_stops_when_lease_is_lost(self):
        stop_event = threading.Event()
        lost_event = Mock()

        with patch.object(
            worker,
            "renew_task_lease",
            return_value=False,
        ), patch.object(
            worker,
            "LEASE_RENEW_INTERVAL",
            0,
        ):
            worker.maintain_task_lease(
                "task-1",
                "worker-1",
                stop_event,
                lost_event,
            )

        lost_event.set.assert_called_once()

    def test_heartbeat_stops_when_renewal_raises(self):
        stop_event = threading.Event()
        lost_event = Mock()

        with patch.object(
            worker,
            "renew_task_lease",
            side_effect=RuntimeError("Database unavailable"),
        ), patch.object(
            worker,
            "LEASE_RENEW_INTERVAL",
            0,
        ):
            worker.maintain_task_lease(
                "task-1",
                "worker-1",
                stop_event,
                lost_event,
            )

        lost_event.set.assert_called_once()


class WorkerExecutionTests(unittest.TestCase):

    def setUp(self):
        self.task = {
            "id": "task-123",
            "workspace_id": "default",
            "title": "Test task",
            "description": "Test description",
        }

        self.context = {
            "project_root": "/tmp/test-project",
        }

        self.result = {
            "analysis": "Analysis",
            "critique": "Critique",
            "final_report": "Final report",
        }

    @patch.object(worker, "complete_task")
    @patch.object(worker, "fail_task")
    @patch.object(worker, "write_workspace_artifact")
    @patch.object(worker, "run_collaboration")
    @patch.object(worker, "get_workspace_context")
    def test_successful_execution_completes_task(
        self,
        get_context,
        run_collaboration,
        write_artifact,
        fail_task,
        complete_task,
    ):
        get_context.return_value = self.context
        run_collaboration.return_value = self.result

        write_artifact.side_effect = [
            "/tmp/analysis.md",
            "/tmp/critique.md",
            "/tmp/final_report.md",
        ]

        with patch.object(
            worker.threading,
            "Thread",
        ) as mock_thread_class:

            mock_thread = mock_thread_class.return_value
            mock_thread.is_alive.return_value = False

            worker.execute_task(
                self.task,
                "worker-1",
            )

        get_context.assert_called_once_with("default")

        run_collaboration.assert_called_once_with(
            "Test task\n\nTest description",
            workspace_id="default",
            project_root="/tmp/test-project",
            task_id="task-123",
        )

        self.assertEqual(
            write_artifact.call_count,
            3,
        )

        complete_task.assert_called_once()
        fail_task.assert_not_called()

    @patch.object(worker, "complete_task")
    @patch.object(worker, "fail_task")
    @patch.object(worker, "write_workspace_artifact")
    @patch.object(worker, "run_collaboration")
    @patch.object(worker, "get_workspace_context")
    def test_lost_lease_prevents_artifact_generation(
        self,
        get_context,
        run_collaboration,
        write_artifact,
        fail_task,
        complete_task,
    ):
        get_context.return_value = self.context
        run_collaboration.return_value = self.result

        class FakeThread:

            def __init__(self, target, args, **kwargs):
                self.target = target
                self.args = args

            def start(self):
                # Simulate immediate lease loss.
                self.args[3].set()

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        with patch.object(
            worker.threading,
            "Thread",
            FakeThread,
        ):
            worker.execute_task(
                self.task,
                "worker-1",
            )

        write_artifact.assert_not_called()
        complete_task.assert_not_called()
        fail_task.assert_called_once()

    @patch.object(worker, "complete_task")
    @patch.object(worker, "fail_task")
    @patch.object(worker, "write_workspace_artifact")
    @patch.object(worker, "run_collaboration")
    @patch.object(worker, "get_workspace_context")
    def test_execution_failure_is_recorded(
        self,
        get_context,
        run_collaboration,
        write_artifact,
        fail_task,
        complete_task,
    ):
        get_context.side_effect = RuntimeError(
            "Workspace unavailable"
        )

        with patch.object(
            worker.threading,
            "Thread",
        ) as mock_thread_class:

            mock_thread = mock_thread_class.return_value
            mock_thread.is_alive.return_value = False

            worker.execute_task(
                self.task,
                "worker-1",
            )

        fail_task.assert_called_once()

        self.assertIn(
            "Workspace unavailable",
            fail_task.call_args.args[2],
        )

        write_artifact.assert_not_called()
        complete_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
