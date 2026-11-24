from abc import ABC, abstractmethod


class ModelProvider(ABC):
    """Common interface for all AI model providers."""

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        pass

    @abstractmethod
    def health_check(self) -> bool:
        pass
