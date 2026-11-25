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


class APISecurityBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        self.user = {
            "user_id": "test-user",
            "username": "tester",
        }
        self.headers = {
            "Authorization": "Bearer test-token"
        }

    def request(self, method, path, target, result):
        from unittest.mock import patch

        with patch(
            "app.api.get_session",
            return_value=self.user,
        ), patch(target, return_value=result) as mocked:
            response = getattr(self.client, method)(
                path,
                headers=self.headers,
            )

        return response, mocked

    def test_execution_listing_uses_authorization(self):
        response, mocked = self.request(
            "get",
            "/api/workspaces/default/executions",
            "app.api.list_authorized_agent_executions",
            [],
        )

        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with("test-user", "default")

    def test_execution_cross_workspace_denied(self):
        from unittest.mock import patch

        with patch(
            "app.api.get_session",
            return_value=self.user,
        ), patch(
            "app.api.get_authorized_agent_execution",
            side_effect=PermissionError,
        ):
            response = self.client.get(
                "/api/workspaces/other/executions/123",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 403)

    def test_task_results_listing(self):
        from unittest.mock import patch

        with patch(
            "app.api.get_session",
            return_value=self.user,
        ), patch(
            "app.api.get_authorized_task_results",
            return_value=[{"id": "result-1"}],
        ), patch(
            "app.api.get_authorized_task",
            return_value={"id": "task-1"},
        ):
            response = self.client.get(
                "/api/workspaces/default/tasks/task-1/results",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "result-1")

    def test_individual_result_retrieval(self):
        response, _ = self.request(
            "get",
            "/api/workspaces/default/results/result-1",
            "app.api.get_authorized_task_result",
            {"id": "result-1"},
        )

        self.assertEqual(response.status_code, 200)

    def test_audit_listing_uses_authorization(self):
        response, mocked = self.request(
            "get",
            "/api/workspaces/default/audit",
            "app.api.list_authorized_workspace_audit",
            [],
        )

        self.assertEqual(response.status_code, 200)
        mocked.assert_called_once_with("test-user", "default")

    def test_missing_audit_event_returns_404(self):
        response, _ = self.request(
            "get",
            "/api/workspaces/default/audit/missing",
            "app.api.get_authorized_workspace_audit_event",
            None,
        )

        self.assertEqual(response.status_code, 404)

    def test_task_submission_passes_authenticated_actor(self):
        from unittest.mock import patch

        with patch(
            "app.api.get_session",
            return_value=self.user,
        ), patch(
            "app.api.submit_task",
            return_value="task-123",
        ) as mocked:
            response = self.client.post(
                "/api/workspaces/default/tasks",
                headers=self.headers,
                json={
                    "title": "Security test",
                    "description": "Verify task submission",
                    "priority": 5,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_id"], "task-123")
        self.assertEqual(
            mocked.call_args.kwargs["actor_id"],
            "test-user",
        )

    def test_task_submission_denied_without_workspace_access(self):
        from unittest.mock import patch

        with patch(
            "app.api.get_session",
            return_value=self.user,
        ), patch(
            "app.api.submit_task",
            side_effect=PermissionError,
        ):
            response = self.client.post(
                "/api/workspaces/other/tasks",
                headers=self.headers,
                json={
                    "title": "Unauthorized task",
                    "description": "Should be rejected",
                },
            )

        self.assertEqual(response.status_code, 403)
