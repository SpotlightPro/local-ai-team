from app.agents.base import Agent
from app.providers.registry import get_providers


def create_agent(
    name: str,
    role: str,
    instructions: str,
    provider_name: str,
) -> Agent:

    providers = get_providers()

    if provider_name not in providers:
        raise ValueError(
            f"Provider '{provider_name}' is not configured"
        )

    return Agent(
        name=name,
        role=role,
        instructions=instructions,
        provider=providers[provider_name]
    )
