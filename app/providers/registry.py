import os

from app.providers.ollama import OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


def get_providers():
    """Create configured providers without requiring them all to be online."""

    providers = {
        "ollama": OllamaProvider(
            base_url=os.getenv(
                "OLLAMA_BASE_URL",
                "http://192.168.1.7:11434"
            ),
            model=os.getenv(
                "OLLAMA_MODEL",
                "gpt-oss-20b:latest"
            ),
        )
    }

    if os.getenv("LM_STUDIO_ENABLED", "false").lower() == "true":
        providers["lm_studio"] = OpenAICompatibleProvider(
            base_url=os.getenv(
                "LM_STUDIO_BASE_URL",
                "http://127.0.0.1:1234/v1"
            ),
            model=os.getenv(
                "LM_STUDIO_MODEL",
                "openai/gpt-oss-20b"
            ),
        )

    return providers
