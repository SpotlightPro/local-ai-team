import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.storage import database
from app.storage.migrations import migrate
from app.auth import (
    create_first_admin,
    create_user,
    get_session,
    login,
    logout,
    require_platform_admin,
    require_workspace_access,
    require_workspace_role,
    set_user_active,
    list_users,
)


class AuthorizationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"

        self.db_patch = patch.object(
            database, "DATABASE_PATH", self.db_path
        )
        self.db_patch.start()

        database.initialize_database()
        migrate()

        with database.connect() as db:
            workspace_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO workspaces (id, name) VALUES (?, ?)",
                (workspace_id, "Second workspace"),
            )
            db.commit()

        self.workspace_b = workspace_id

        self.admin_id = create_first_admin(
            "platformadmin",
            "SecurePassword123!",
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_first_admin_is_platform_admin(self):
        admin = require_platform_admin(self.admin_id)
        self.assertEqual(admin["platform_role"], "platform_admin")

    def test_second_admin_initialization_rejected(self):
        with self.assertRaises(RuntimeError):
            create_first_admin("anotheradmin", "AnotherPassword123!")

    def test_login_and_session(self):
        session = login("platformadmin", "SecurePassword123!")
        self.assertIsNotNone(session)
        self.assertIsNotNone(get_session(session["token"]))

    def test_invalid_password_rejected(self):
        self.assertIsNone(login("platformadmin", "WrongPassword123!"))

    def test_logout_revokes_session(self):
        token = login(
            "platformadmin", "SecurePassword123!"
        )["token"]
        self.assertTrue(logout(token))
        self.assertIsNone(get_session(token))

    def test_platform_admin_access(self):
        result = require_workspace_access(
            self.admin_id, self.workspace_b
        )
        self.assertEqual(result["role"], "platform_admin")

    def test_create_workspace_user(self):
        user_id = create_user(
            "workspace_manager",
            "ManagerPassword123!",
            "default",
            "manager",
            self.admin_id,
        )
        result = require_workspace_role(
            user_id, "default", {"manager"}
        )
        self.assertEqual(result["role"], "manager")

    def test_cross_workspace_access_denied(self):
        user_id = create_user(
            "workspace_member",
            "MemberPassword123!",
            "default",
            "member",
            self.admin_id,
        )
        with self.assertRaises(PermissionError):
            require_workspace_access(user_id, self.workspace_b)

    def test_manager_cannot_create_administrator(self):
        manager_id = create_user(
            "manager",
            "ManagerPassword123!",
            "default",
            "manager",
            self.admin_id,
        )
        with self.assertRaises(PermissionError):
            create_user(
                "newadmin",
                "NewAdminPassword123!",
                "default",
                "administrator",
                manager_id,
            )

    def test_member_cannot_manage_users(self):
        member_id = create_user(
            "member",
            "MemberPassword123!",
            "default",
            "member",
            self.admin_id,
        )
        with self.assertRaises(PermissionError):
            list_users(member_id, "default")

    def test_viewer_is_not_administrator(self):
        viewer_id = create_user(
            "viewer",
            "ViewerPassword123!",
            "default",
            "viewer",
            self.admin_id,
        )
        with self.assertRaises(PermissionError):
            require_workspace_role(
                viewer_id,
                "default",
                {"administrator", "manager"},
            )

    def test_deactivation_revokes_sessions(self):
        user_id = create_user(
            "temporary",
            "TemporaryPassword123!",
            "default",
            "member",
            self.admin_id,
        )
        token = login(
            "temporary", "TemporaryPassword123!"
        )["token"]

        self.assertTrue(
            set_user_active(user_id, False, self.admin_id)
        )
        self.assertIsNone(get_session(token))

    def test_inactive_user_cannot_login(self):
        user_id = create_user(
            "inactive",
            "InactivePassword123!",
            "default",
            "member",
            self.admin_id,
        )
        set_user_active(user_id, False, self.admin_id)
        self.assertIsNone(
            login("inactive", "InactivePassword123!")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
