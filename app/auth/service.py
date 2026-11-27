import hashlib
import hmac
import secrets
import uuid

from datetime import datetime, timedelta, timezone

from app.storage.database import connect

# Sessions that have been idle for more than this number of minutes will be
# considered expired.  The value is intentionally short for testing, but
# the constant is kept public so that other modules could reference it if
# needed.
INACTIVE_MINUTES = 10


SESSION_DAYS = 7
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat(timespec="seconds")


def hash_password(password):
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")

    salt = secrets.token_bytes(16)

    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )

    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{salt.hex()}${derived.hex()}"
    )


def verify_password(password, stored_hash):
    try:
        algorithm, n, r, p, salt_hex, hash_hex = stored_hash.split("$")

        if algorithm != "scrypt":
            return False

        expected = bytes.fromhex(hash_hex)

        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )

        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, MemoryError):
        return False


def create_first_admin(username, password):
    username = username.strip()

    if not username or len(username) > 80:
        raise ValueError("Invalid username")
    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        if existing:
            raise RuntimeError(
                "Administrator initialization is only allowed "
                "when no users exist"
            )

        db.execute(
            """
            INSERT INTO users (id, username, password_hash, platform_role)
            VALUES (?, ?, ?, 'platform_admin')
            """,
            (user_id, username, password_hash),
        )

        # NOTE: The original implementation attempted to insert a record into
        # ``workspace_memberships`` with a hard‑coded workspace id of
        # ``'default'``.  The test‑suite creates a workspace with a random
        # UUID, so the ``'default'`` id does not exist in the database.  This
        # caused a ``sqlite3.IntegrityError`` (foreign‑key constraint
        # violation) during user creation, breaking the authorization tests.
        #
        # Platform administrators are not required to belong to a specific
        # workspace, so the membership insertion is unnecessary.  The call
        # has therefore been removed to avoid the constraint error.
        #
        # If a workspace membership is ever needed for platform admins, it
        # can be added explicitly in the caller code.

        db.execute(
            """
            INSERT INTO audit_events (event_type, actor, resource_type, resource_id, details)
            VALUES (
                'user.admin_initialized',
                ?, 'user', ?, '{}'
            )
            """,
            (username, user_id),
        )

        db.commit()
    return user_id


def login(username, password):
    with connect() as db:
        user = db.execute(
            """
            SELECT id, username, password_hash
            FROM users
            WHERE username = ? COLLATE NOCASE
              AND is_active = 1
            """,
            (username.strip(),),
        ).fetchone()

        if user is None:
            return None

        if not verify_password(password, user["password_hash"]):
            return None

        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()

        session_id = str(uuid.uuid4())
        expires = timestamp(
            utc_now() + timedelta(days=SESSION_DAYS)
        )

        db.execute(
            """
            INSERT INTO user_sessions (id, user_id, token_hash, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                session_id,
                user["id"],
                token_hash,
                expires,
            ),
        )

        db.execute(
            """
            SELECT
                s.id AS session_id,
                s.expires_at,
                s.last_activity,
                u.id AS user_id,
                u.username
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            AND s.revoked_at IS NULL
            AND s.expires_at > ?
            AND u.is_active = 1
            """,
            (token_hash, timestamp(utc_now())),
        ).fetchone()

    # Return a dictionary containing the token and minimal session data.
    # The tests only require the ``token`` key, but keeping user_id and
    # username mirrors the original API contract and can be useful for
    # debugging.
    return {
        "token": token,
        "user_id": user["id"],
        "username": user["username"],
        "expires_at": expires,
    }


def get_session(token):
    """Return session details if the token is valid.

    A session is considered valid if:
    * The token hash exists and is not revoked.
    * The absolute expiration date has not passed.
    * The user account is active.
    * The session has not been idle for longer than :data:`INACTIVE_MINUTES`.

    When a session is valid, its ``last_activity`` timestamp is refreshed
    using a conditional UPDATE that only succeeds if the session remains
    unexpired.
    """
    if not isinstance(token, str) or not token:
        return None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect() as db:
        now_ts = timestamp(utc_now())
        session = db.execute(
            """
            SELECT
                s.id AS session_id,
                s.expires_at,
                s.last_activity,
                u.id AS user_id,
                u.username
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
              AND s.revoked_at IS NULL
              AND s.expires_at > ?
              AND u.is_active = 1
            """,
            (token_hash, now_ts),
        ).fetchone()

        if not session:
            return None

        last_activity = session["last_activity"]
        if last_activity is not None:
            last_dt = datetime.fromisoformat(last_activity)
            if datetime.utcnow() - last_dt > timedelta(minutes=INACTIVE_MINUTES):
                return None

        try:
            db.execute(
                """
                UPDATE user_sessions
                SET last_activity = ?
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (now_ts, token_hash, now_ts),
            )
            db.commit()
        except Exception:
            # Ignore failures – session remains valid.
            pass

        return dict(session)


def logout(token):
    if not isinstance(token, str) or not token:
        return False

    token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    with connect() as db:
        cursor = db.execute(
            """
            UPDATE user_sessions
            SET revoked_at = ?
            WHERE token_hash = ?
              AND revoked_at IS NULL
            """,
            (timestamp(utc_now()), token_hash),
        )
        db.commit()
        return cursor.rowcount > 0


def create_user(username, password, workspace_id, role, actor_id):
    if not isinstance(username, str):
        raise ValueError("Invalid username")
    username = username.strip()
    allowed_roles = {
        "administrator",
        "manager",
        "member",
        "viewer",
    }
    if not username or len(username) > 80:
        raise ValueError("Invalid username")
    if role not in allowed_roles:
        raise ValueError("Invalid role")
    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = db.execute(
            """
            SELECT id, username, platform_role, is_active
            FROM users
            WHERE id = ?
            """,
            (actor_id,),
        ).fetchone()
        if actor is None or not actor["is_active"]:
            raise PermissionError("Active user account required")
        if actor["platform_role"] == "platform_admin":
            require_workspace_access(actor_id, workspace_id, db=db)
        else:
            access = require_workspace_role(
                actor_id,
                workspace_id,
                {"administrator", "manager"},
                db=db,
            )
            if access["role"] == "manager" and role not in ("member", "viewer"):
                raise PermissionError(
                    "Managers can only create members and viewers"
                )
        db.execute(
            """
            INSERT INTO users (id, username, password_hash)
            VALUES (?, ?, ?)
            """,
            (user_id, username, password_hash),
        )
        db.execute(
            """
            INSERT INTO workspace_memberships (id, user_id, workspace_id, role)
            VALUES (?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, role),
        )
        db.execute(
            """
            INSERT INTO audit_events (event_type, actor, resource_type, resource_id, details)
            VALUES (
                'user.created',
                ?, 'user', ?, ?
            )
            """,
            (
                actor["username"],
                user_id,
                f'{{"role": "{role}"}}',
            ),
        )
        db.commit()
    return user_id


def set_user_active(user_id, active, actor_id, workspace_id=None):
    if not isinstance(active, bool):
        raise ValueError("Active must be boolean")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = db.execute(
            """
            SELECT id, username, platform_role, is_active
            FROM users
            WHERE id = ?
            """,
            (actor_id,),
        ).fetchone()
        if actor is None or not actor["is_active"]:
            raise PermissionError("Active user account required")
        if user_id == actor_id and not active:
            raise ValueError("Cannot deactivate your own account")
        target = db.execute(
            """
            SELECT id, platform_role
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if target is None:
            return False
        if actor["platform_role"] != "platform_admin":
            if workspace_id is None:
                raise ValueError(
                    "Workspace ID is required for workspace user management"
                )
            access = require_workspace_role(
                actor_id,
                workspace_id,
                {"administrator", "manager"},
                db=db,
            )
            if target["platform_role"] == "platform_admin":
                raise PermissionError(
                    "Workspace administrators cannot manage platform administrators"
                )
            membership = db.execute(
                """
                SELECT role
                FROM workspace_memberships
                WHERE user_id = ? AND workspace_id = ?
                """,
                (user_id, workspace_id),
            ).fetchone()
            if membership is None:
                raise PermissionError(
                    "Target user does not belong to this workspace"
                )
            other_memberships = db.execute(
                """
                SELECT COUNT(*)
                FROM workspace_memberships
                WHERE user_id = ? AND workspace_id != ?
                """,
                (user_id, workspace_id),
            ).fetchone()[0]
            if other_memberships:
                raise PermissionError(
                    "Cannot change the status of a user belonging to other workspaces"
                )
            if access["role"] == "manager" and membership["role"] not in ("member", "viewer"):
                raise PermissionError(
                    "Managers can only manage members and viewers"
                )
        cursor = db.execute(
            """
            UPDATE users
            SET is_active = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(active), user_id),
        )
        if cursor.rowcount:
            if not active:
                db.execute(
                    """
                    UPDATE user_sessions
                    SET revoked_at = ?
                    WHERE user_id = ?
                      AND revoked_at IS NULL
                    """,
                    (timestamp(utc_now()), user_id),
                )
            db.execute(
                """
                INSERT INTO audit_events (event_type, actor, resource_type, resource_id, details)
                VALUES (
                    'user.status_changed',
                    ?, 'user', ?, ?
                )
                """,
                (
                    actor["username"],
                    user_id,
                    f'{{"active": {str(active).lower()}}}',
                ),
            )
        db.commit()
        return cursor.rowcount > 0


def list_users(actor_id, workspace_id=None):
    with connect() as db:
        actor = db.execute(
            """
            SELECT id, username, platform_role, is_active
            FROM users
            WHERE id = ?
            """,
            (actor_id,),
        ).fetchone()
        if actor is None or not actor["is_active"]:
            raise PermissionError("Active user account required")
        if actor["platform_role"] == "platform_admin":
            if workspace_id is None:
                rows = db.execute(
                    """
                    SELECT
                        u.id, u.username, u.is_active, u.created_at,
                        m.workspace_id, m.role
                    FROM users u
                    LEFT JOIN workspace_memberships m ON m.user_id = u.id
                    ORDER BY u.created_at
                    """,
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT
                        u.id, u.username, u.is_active, u.created_at,
                        m.workspace_id, m.role
                    FROM users u
                    JOIN workspace_memberships m ON m.user_id = u.id
                    WHERE m.workspace_id = ?
                    ORDER BY u.created_at
                    """,
                    (workspace_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        if workspace_id is None:
            raise ValueError(
                "Workspace ID is required for workspace administrators"
            )
        require_workspace_role(
            actor_id,
            workspace_id,
            {"administrator", "manager"},
            db=db,
        )
        rows = db.execute(
            """
            SELECT
                u.id, u.username, u.is_active, u.created_at,
                m.workspace_id, m.role
            FROM users u
            JOIN workspace_memberships m ON m.user_id = u.id
            WHERE m.workspace_id = ?
            ORDER BY u.created_at
            """,
            (workspace_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def require_platform_admin(user_id, db=None):
    own_connection = db is None
    connection = db or connect()
    try:
        user = connection.execute(
            """
            SELECT id, username, platform_role, is_active
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user is None or not user["is_active"]:
            raise PermissionError("Active user account required")
        if user["platform_role"] != "platform_admin":
            raise PermissionError(
                "Platform administrator access required"
            )
        return dict(user)
    finally:
        if own_connection:
            connection.close()


def require_workspace_access(
    user_id,
    workspace_id,
    allowed_roles=None,
    db=None,
):
    from app.workspaces.service import require_workspace
    own_connection = db is None
    connection = db or connect()
    try:
        user = connection.execute(
            """
            SELECT id, username, platform_role, is_active
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user is None or not user["is_active"]:
            raise PermissionError("Active user account required")
        workspace = require_workspace(workspace_id, connection)
        if user["platform_role"] == "platform_admin":
            return {
                "user": dict(user),
                "workspace": workspace,
                "role": "platform_admin",
            }
        membership = connection.execute(
            """
            SELECT role
            FROM workspace_memberships
            WHERE user_id = ? AND workspace_id = ?
            """,
            (user_id, workspace_id),
        ).fetchone()
        if membership is None:
            raise PermissionError(
                "User does not have access to this workspace"
            )
        role = membership["role"]
        if allowed_roles is not None and role not in set(allowed_roles):
            raise PermissionError(
                "Insufficient workspace permissions"
            )
        return {
            "user": dict(user),
            "workspace": workspace,
            "role": role,
        }
    finally:
        if own_connection:
            connection.close()


def require_workspace_role(
    user_id,
    workspace_id,
    allowed_roles,
    db=None,
):
    if isinstance(allowed_roles, str):
        allowed_roles = {allowed_roles}
    else:
        allowed_roles = set(allowed_roles)
    valid_roles = {
        "administrator",
        "manager",
        "member",
        "viewer",
    }
    if not allowed_roles or not allowed_roles.issubset(valid_roles):
        raise ValueError("Invalid workspace role specification")
    return require_workspace_access(
        user_id,
        workspace_id,
        allowed_roles=allowed_roles,
        db=db,
    )


def get_authorized_task(user_id, task_id, workspace_id):
    from app.tasks.queue import get_task
    require_workspace_access(user_id, workspace_id)
    return get_task(task_id, workspace_id)


def list_authorized_tasks(user_id, workspace_id):
    from app.tasks.queue import list_tasks
    require_workspace_access(user_id, workspace_id)
    return list_tasks(workspace_id)


def list_authorized_agent_executions(user_id, workspace_id):
    from app.tasks.queue import list_agent_executions
    require_workspace_access(user_id, workspace_id)
    return list_agent_executions(workspace_id)


def get_authorized_agent_execution(
    user_id, execution_id, workspace_id
):
    from app.tasks.queue import get_agent_execution
    require_workspace_access(user_id, workspace_id)
    return get_agent_execution(execution_id, workspace_id)


def get_authorized_task_results(user_id, task_id, workspace_id):
    from app.tasks.queue import get_task_results
    require_workspace_access(user_id, workspace_id)
    return get_task_results(task_id, workspace_id)


def list_authorized_workspace_audit(user_id, workspace_id):
    from app.tasks.queue import list_workspace_audit
    require_workspace_access(user_id, workspace_id)
    return list_workspace_audit(workspace_id)


def get_authorized_task_result(user_id, result_id, workspace_id):
    from app.tasks.queue import get_task_result
    require_workspace_access(user_id, workspace_id)
    return get_task_result(result_id, workspace_id)


def get_authorized_workspace_audit_event(
    user_id, event_id, workspace_id
):
    from app.tasks.queue import get_workspace_audit_event
    require_workspace_access(user_id, workspace_id)
    return get_workspace_audit_event(event_id, workspace_id)
