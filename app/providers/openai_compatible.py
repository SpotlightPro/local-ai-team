import requests
from app.providers.base import ModelProvider


class OpenAICompatibleProvider(ModelProvider):
    """Provider for LM Studio and compatible APIs."""

    def __init__(
        self,
        base_url,
        model,
        api_key=None,
        timeout=300,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self):
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return headers

    def generate(self, prompt, system_prompt=""):
        messages = []

        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })

        messages.append({
            "role": "user",
            "content": prompt
        })

        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json={
                "model": self.model,
                "messages": messages,
                "stream": False
            },
            timeout=self.timeout
        )

        response.raise_for_status()

        return response.json()["choices"][0]["message"]["content"]

    def health_check(self):
        response = requests.get(
            f"{self.base_url}/models",
            headers=self._headers(),
            timeout=10
        )

        response.raise_for_status()

        models = [
            item["id"]
            for item in response.json().get("data", [])
        ]

        return self.model in models
