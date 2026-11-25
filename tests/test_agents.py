import unittest
from unittest.mock import patch

from app.agents.factory import create_agent


class FakeProvider:
    def __init__(self):
        self.model = "default-model"

    def generate(self, prompt, system_prompt=""):
        return "test response"

    def health_check(self):
        return True


class AgentFactoryTests(unittest.TestCase):

    @patch("app.agents.factory.get_providers")
    def test_agent_uses_configured_model(self, mock_providers):
        provider = FakeProvider()
        mock_providers.return_value = {"ollama": provider}

        agent = create_agent(
            name="Analyst",
            role="Analysis",
            instructions="Analyse independently",
            provider_name="ollama",
            model="gpt-oss-20b:32k",
        )

        self.assertEqual(
            agent.provider.model,
            "gpt-oss-20b:32k"
        )

    @patch("app.agents.factory.get_providers")
    def test_agent_uses_default_model_when_not_overridden(
        self, mock_providers
    ):
        provider = FakeProvider()
        mock_providers.return_value = {"ollama": provider}

        agent = create_agent(
            name="Reviewer",
            role="Review",
            instructions="Review critically",
            provider_name="ollama",
        )

        self.assertEqual(agent.provider.model, "default-model")

    @patch("app.agents.factory.get_providers")
    def test_unknown_provider_is_rejected(self, mock_providers):
        mock_providers.return_value = {"ollama": FakeProvider()}

        with self.assertRaises(ValueError):
            create_agent(
                name="Coordinator",
                role="Coordination",
                instructions="Coordinate",
                provider_name="unknown",
            )


if __name__ == "__main__":
    unittest.main()
