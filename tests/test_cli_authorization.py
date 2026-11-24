import os
import sys
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from io import StringIO

from app.tasks.cli import main


class CLIAuthorizationTests(unittest.TestCase):

    @patch("app.tasks.cli.migrate")
    @patch("app.tasks.cli.get_session", return_value=None)
    def test_cli_rejects_missing_session(self, session, migrate):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "argv", ["cli", "tasks"]):
                error = StringIO()
                with redirect_stderr(error):
                    with self.assertRaises(SystemExit) as result:
                        main()

        self.assertEqual(result.exception.code, 1)
        self.assertIn("Valid session required", error.getvalue())

    @patch("app.tasks.cli.migrate")
    @patch("app.tasks.cli.list_authorized_tasks", return_value=[])
    @patch("app.tasks.cli.get_session", return_value={
        "user_id": "test-user",
        "username": "tester",
    })
    def test_authenticated_cli_uses_authorized_task_listing(
        self, session, tasks, migrate
    ):
        with patch.dict(
            os.environ,
            {"LOCAL_AI_SESSION_TOKEN": "test-token"},
            clear=True,
        ):
            with patch.object(sys, "argv", ["cli", "tasks"]):
                with patch("sys.stdout", new_callable=StringIO) as output:
                    main()

        tasks.assert_called_once_with("test-user", "default")
        self.assertEqual(output.getvalue().strip(), "[]")


if __name__ == "__main__":
    unittest.main()
