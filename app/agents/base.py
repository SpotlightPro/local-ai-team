from dataclasses import dataclass
from app.providers.base import ModelProvider


@dataclass
class Agent:
    name: str
    role: str
    instructions: str
    provider: ModelProvider

    def execute(self, task: str) -> str:
        system_prompt = f"""
You are an AI agent operating within a multi-agent AI platform.

Your identity: {self.name}
Your role: {self.role}

Instructions:
{self.instructions}

Requirements:
- Analyse the task independently.
- Explain your reasoning and conclusions.
- Identify assumptions and uncertainties.
- Never claim to have performed actions you did not perform.
- Do not modify files or external systems.
- Return clear, structured findings.
"""

        return self.provider.generate(
            prompt=task,
            system_prompt=system_prompt
        )
