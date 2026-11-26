import os
import threading
import unittest
from unittest.mock import patch, Mock

from app.agents.factory import create_agent
from app.agents import factory
from app.orchestration import collaboration


class FakeProvider:
    def __init__(self):
        self.model = "default-model"


class CollaborationModelTests(unittest.TestCase):

    def test_cancellation_stops_workflow_before_reviewer(self):
        cancellation_event = threading.Event()

        def execute_analyst(*args, **kwargs):
            cancellation_event.set()
            return "Completed analysis"

        with patch.object(
            collaboration, "create_agent"
        ) as create_agent, patch.object(
            collaboration,
            "execute_tracked_agent",
            side_effect=execute_analyst,
        ):
            with self.assertRaises(
                collaboration.CollaborationCancelled
            ):
                collaboration.run_collaboration(
                    task="Test task",
                    workspace_id="default",
                    project_root="/tmp",
                    task_id="test-task",
                    cancellation_event=cancellation_event,
                )

        create_agent.assert_called_once()

    def test_agents_have_independent_provider_instances(self):
        with patch.object(
            factory,
            "get_providers",
            side_effect=lambda: {"ollama": FakeProvider()}
        ):
            analyst = create_agent(
                "Analyst", "Analysis", "Analyse",
                "ollama", "gpt-oss-20b:32k"
            )
            reviewer = create_agent(
                "Reviewer", "Review", "Review",
                "ollama", "llama3.2:latest"
            )

        self.assertIsNot(analyst.provider, reviewer.provider)
        self.assertEqual(analyst.provider.model, "gpt-oss-20b:32k")
        self.assertEqual(reviewer.provider.model, "llama3.2:latest")

    def check_agent_model(self, node, setting, expected):
        state = {
            "task": "Test task",
            "task_id": "test-task",
            "workspace_id": "default",
            "project_root": "/tmp",
            "analysis": "Analysis",
            "critique": "Critique",
            "final_report": "",
        }

        with patch.dict(os.environ, {setting: expected}):
            with patch.object(
                collaboration, "create_agent"
            ) as create:
                with patch.object(
                    collaboration,
                    "execute_tracked_agent",
                    return_value="Test output"
                ):
                    create.return_value = Mock()
                    node(state)

        self.assertEqual(
            create.call_args.kwargs["model"],
            expected
        )

    def test_analyst_model_configuration(self):
        self.check_agent_model(
            collaboration.analyst_node,
            "LOCAL_AI_ANALYST_MODEL",
            "gpt-oss-20b:32k"
        )

    def test_reviewer_model_configuration(self):
        self.check_agent_model(
            collaboration.reviewer_node,
            "LOCAL_AI_REVIEWER_MODEL",
            "llama3.2:latest"
        )

    def test_coordinator_model_configuration(self):
        self.check_agent_model(
            collaboration.coordinator_node,
            "LOCAL_AI_COORDINATOR_MODEL",
            "gpt-oss-20b:32k"
        )


if __name__ == "__main__":
    unittest.main()
