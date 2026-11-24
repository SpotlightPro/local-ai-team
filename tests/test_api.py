import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import app


class APITests(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_health_endpoint(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "online")

    def test_unauthenticated_access_rejected(self):
        response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 401)

    @patch("app.api.get_session", return_value=None)
    def test_invalid_session_rejected(self, mock_session):
        response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )

        self.assertEqual(response.status_code, 401)
        mock_session.assert_called_once()

    @patch("app.api.login", return_value=None)
    def test_invalid_login_rejected(self, mock_login):
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "invalid",
                "password": "wrong-password",
            },
        )

        self.assertEqual(response.status_code, 401)

    @patch("app.api.login")
    def test_successful_login(self, mock_login):
        mock_login.return_value = {
            "user_id": "test-user",
            "username": "tester",
            "token": "test-session-token",
            "expires_at": "2030-01-01T00:00:00+00:00",
        }

        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "tester",
                "password": "correct-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["access_token"],
            "test-session-token",
        )

    def test_openapi_documentation(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("paths", response.json())


if __name__ == "__main__":
    unittest.main()
