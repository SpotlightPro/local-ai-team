import requests
from app.providers.base import ModelProvider


class OllamaProvider(ModelProvider):

    def __init__(
        self,
        base_url="http://192.168.1.7:11434",
        model="gpt-oss-20b:latest",
        timeout=300,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

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
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False
            },
            timeout=self.timeout
        )

        response.raise_for_status()
        return response.json()["message"]["content"]

    def health_check(self):
        response = requests.get(
            f"{self.base_url}/api/tags",
            timeout=10
        )
        response.raise_for_status()

        models = [
            item["name"]
            for item in response.json().get("models", [])
        ]

        return self.model in models
