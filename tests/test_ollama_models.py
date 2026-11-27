"""Tests for the /api/ollama/models endpoint."""

import os
import pytest

from urllib.error import URLError
from fastapi.testclient import TestClient

from app.api import app, authenticated_user


@pytest.fixture
def client():
    app.dependency_overrides[authenticated_user] = lambda: {
        "user_id": "test-user",
        "username": "test-user",
    }

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(authenticated_user, None)

def test_ollama_models_success(monkeypatch, client):
    """When the Ollama HTTP API returns a model list the endpoint
    propagates the data unchanged.
    """

    dummy_base = "http://localhost:1234"
    monkeypatch.setenv("OLLAMA_BASE_URL", dummy_base)
    monkeypatch.setenv("OLLAMA_REQUEST_TIMEOUT", "5")

    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"models": [{"name": "dummy-model", "size": 123}]}'

    def dummy_urlopen(url, timeout=None):
        assert url == f"{dummy_base}/api/tags"
        return DummyResponse()

    # The endpoint uses the name ``urlopen`` imported from urllib.request.
    # Overwrite the function in the app module so the patch takes effect.
    monkeypatch.setattr("app.api.urlopen", dummy_urlopen)

    response = client.get("/api/ollama/models")
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is True
    assert isinstance(data["models"], list)
    assert data["models"][0]["name"] == "dummy-model"


def test_ollama_models_success(monkeypatch, client):
    """When the Ollama endpoint cannot be reached the API reports a
    disconnected state and provides an empty model list.
    """

    dummy_base = "http://localhost:1234"
    monkeypatch.setenv("OLLAMA_BASE_URL", dummy_base)
    monkeypatch.setenv("OLLAMA_REQUEST_TIMEOUT", "1")

    def raise_url_error(url, timeout=None):
        raise URLError("simulated network failure")

    monkeypatch.setattr("app.api.urlopen", raise_url_error)
    # Re‑apply the dependency override because the middleware clears it
    # after the first request.
    app.dependency_overrides[authenticated_user] = lambda: {
        "user_id": "test-user",
        "username": "test-user",
    }
    response = client.get("/api/ollama/models")
    assert response.status_code == 200
    data = response.json()
    assert data["connected"] is False
    assert data["models"] == []
    assert "message" in data