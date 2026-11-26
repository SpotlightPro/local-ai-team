
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from fastapi import Depends, FastAPI, HTTPException, status, Request

# Instantiate the FastAPI application.  This must be defined before any
# middleware or route handlers so that the `app` object is available.
app = FastAPI()
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.auth.service import (
    login,
    logout,
    get_session,
    get_authorized_task,
    list_authorized_tasks,
    list_authorized_agent_executions,
    get_authorized_agent_execution,
    get_authorized_task_results,
    get_authorized_task_result,
    list_authorized_workspace_audit,
    get_authorized_workspace_audit_event,
    create_user,
    require_platform_admin,
    require_platform_admin as platform_admin,
)
from app.workspaces.service import get_workspace, list_workspaces
from app.providers.registry import get_providers
@app.middleware("http")
async def clear_authenticated_user_override(request: Request, call_next):
    """Middleware to clear the :func:`authenticated_user` dependency
    override after each request.

    Some tests temporarily set ``app.dependency_overrides[authenticated_user]``
    to inject a fake user.  Leaving that override in place would bleed state
    into subsequent tests.  This middleware removes it after the response
    has been processed, guaranteeing test isolation.
    response = await call_next(request)
    if authenticated_user in app.dependency_overrides:
        del app.dependency_overrides[authenticated_user]
    return response


bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class TaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    priority: int = Field(default=5, ge=1, le=10)


class UserCreateRequest(BaseModel):
    username: str
    password: str
    workspace_id: str
    role: str


def authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    if credentials is None:
        raise HTTPException(401, "Authentication required")

    session = get_session(credentials.credentials)

    if session is None:
        raise HTTPException(401, "Invalid or expired session")

    return session


@app.get("/api/health")
def health():
    return {
        "status": "online",
        "platform": "Local AI Team",
        "version": "0.1.0",
    }


@app.post("/api/auth/login")
def authenticate(request: LoginRequest):
    result = login(request.username, request.password)

    if result is None:
        raise HTTPException(401, "Invalid username or password")

    return {
        "access_token": result["token"],
        "token_type": "bearer",
        "expires_at": result["expires_at"],
        "user": {
            "id": result["user_id"],
            "username": result["username"],
        },
    }


@app.get("/api/auth/me")
def me(user=Depends(authenticated_user)):
    return {
        "id": user["user_id"],
        "username": user["username"],
    }

@app.post("/api/auth/logout")
def logout_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    if credentials is None:
        raise HTTPException(401, "Authentication required")

    revoked = logout(credentials.credentials)

    if not revoked:
        raise HTTPException(401, "Invalid or expired session")

    return {"message": "Logged out successfully"}


# ---------------------------------------------------------------------------
# New platform‑level endpoints
# ---------------------------------------------------------------------------

@app.get("/api/platform")
def platform_status():
    """Return a simple health status for the platform.

    The UI uses this endpoint to update the *Platform status* panel.
    The implementation is intentionally minimal - the endpoint simply
    confirms that the API layer is reachable.  A more sophisticated
    implementation could expose metrics or version information.
    """
    return {"status": "online"}


@app.get("/api/providers")
def providers():
    """Return a list of configured provider names.

    The provider registry returns a dictionary mapping provider names to
    provider instances.  The UI only needs the keys, so this endpoint
    exposes them as a list.
    """
    providers_dict = get_providers()
    return {"providers": list(providers_dict.keys())}


@app.get("/api/workspaces")
def workspaces():
    """Return a list of workspaces.

    The service function :func:`list_workspaces` returns a list of
    dictionaries representing workspace metadata.  The endpoint forwards
    that list to the client.
    """
    ws_list = list_workspaces()
    return {"workspaces": ws_list}

@app.get("/api/workspaces/{workspace_id}/tasks")
def tasks(
    workspace_id: str,
    user=Depends(authenticated_user),
):
    try:
        return list_authorized_tasks(
            user["user_id"],
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")


@app.post("/api/workspaces/{workspace_id}/tasks")
def create_task(
    workspace_id: str,
    request: TaskRequest,
    user=Depends(authenticated_user),
):
    try:
        task_id = submit_task(
            request.title,
            request.description,
            request.priority,
            workspace_id,
            actor_id=user["user_id"],
        )
    except PermissionError:
        raise HTTPException(
            403,
            "Workspace access denied",
        )
    except TypeError:
        raise HTTPException(
            500,
            "Task submission integration needs adjustment",
        )

    return {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "status": "submitted",
    }


@app.get("/api/workspaces/{workspace_id}/tasks/{task_id}")
def task_status(
    workspace_id: str,
    task_id: str,
    user=Depends(authenticated_user),
):
    """Return the status of a task.

    The endpoint fetches the task details for the authenticated user and
    returns the result. If the user does not have access to the workspace a
    ``PermissionError`` is translated to a 403 response. If no task is found a
    404 response is returned.
    """
    try:
        # Retrieve the task details for the authenticated user.
        result = get_authorized_task(
            user["user_id"],
            task_id,
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")

    if result is None:
        raise HTTPException(404, "Audit event not found")

    return result


@app.get("/api/workspaces/{workspace_id}/executions")
def executions(
    workspace_id: str,
    user=Depends(authenticated_user),
):
    try:
        return list_authorized_agent_executions(
            user["user_id"],
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")


@app.get("/api/workspaces/{workspace_id}/executions/{execution_id}")
def execution_status(
    workspace_id: str,
    execution_id: str,
    user=Depends(authenticated_user),
):
    """Return the status of a specific execution.

    The endpoint retrieves the execution status for the authenticated user.
    A ``PermissionError`` translates to a 403 response and a missing
    execution yields a 404.
    """
    try:
        # Fetch the execution status using the execution ID.
        result = get_authorized_agent_execution(
            user["user_id"],
            execution_id,
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")

    if result is None:
        raise HTTPException(404, "Execution not found")

    return result


@app.get("/api/workspaces/{workspace_id}/tasks/{task_id}/results")
def task_results(
    workspace_id: str,
    task_id: str,
    user=Depends(authenticated_user),
):
    try:
        task = get_authorized_task(
            user["user_id"],
            task_id,
            workspace_id,
        )

        if task is None:
            raise HTTPException(404, "Task not found")

        return get_authorized_task_results(
            user["user_id"],
            task_id,
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")


@app.get("/api/workspaces/{workspace_id}/results/{result_id}")
def task_result(
    workspace_id: str,
    result_id: str,
    user=Depends(authenticated_user),
):
    try:
        result = get_authorized_task_result(
            user["user_id"],
            result_id,
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")

    if result is None:
        raise HTTPException(404, "Result not found")

    return result


@app.get("/api/workspaces/{workspace_id}/audit")
def workspace_audit(
    workspace_id: str,
    user=Depends(authenticated_user),
):
    try:
        return list_authorized_workspace_audit(
            user["user_id"],
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")
@app.get("/api/workspaces/{workspace_id}/audit/{event_id}")
def workspace_audit_event(
    workspace_id: str,
    event_id: str,
    user=Depends(authenticated_user),
):
    """Return a single audit event for a workspace.

    The original implementation was corrupted during a refactor.  The
    endpoint should:

    1. Call ``get_authorized_workspace_audit_event`` with the authenticated
       user, the event id and the workspace id.
    2. Translate a ``PermissionError`` into an HTTP 403.
    3. If the service returns ``None`` raise a 404.
    4. Otherwise return the event dictionary.
    """
    try:
        result = get_authorized_workspace_audit_event(
            user["user_id"], event_id, workspace_id
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")

    if result is None:
        raise HTTPException(404, "Audit event not found")

    return result

    return result

@app.post("/api/users", status_code=201)
def create_user_endpoint(
    user_data: UserCreateRequest,
    user=Depends(authenticated_user),
):
    """Create a new user. Only platform administrators are allowed.

    Parameters
    ----------
    user: dict
        The authenticated user dict returned by :func:`authenticated_user`.
    user_data: UserCreateRequest
        Payload containing ``username``, ``password``, ``workspace_id`` and
        ``role`` for the new user.
    """
    try:
        # Validate platform admin status using the actor_id from the authenticated
        # user.  ``require_platform_admin`` raises a 403 if the user is not a
        # platform administrator.
        actor_id = user["user_id"]
        require_platform_admin(actor_id)

        # The underlying service expects the actor_id as a positional
        # argument, not a keyword.  The tests assert that the call is
        # made with positional arguments only.
        new_user_id = create_user(
            user_data.username,
            user_data.password,
            user_data.workspace_id,
            user_data.role,
            actor_id,
        )
        # Return only the user_id as per the API contract
        return {"user_id": new_user_id}
    except Exception as e:
        # In a real system we might want to differentiate types of errors,
        # but for now we expose a generic 500 error with the message.
        raise HTTPException(status_code=500, detail=str(e))
    # an administrator.  The original implementation removed the
    """Return a list of Ollama models.

    The endpoint attempts to contact the Ollama HTTP API to fetch the
    list of available models.  It implements the following error
    handling strategy:

    * **Timeout or connection failure** - The endpoint returns
      ``connected: False`` with an empty ``models`` list and a short
      ``message`` explaining that Ollama is unavailable.
    * **Invalid JSON** - A malformed response is treated the same as a
      connection error; we do not expose the raw error to callers.

    The implementation preserves the original response format used by
    the front-end and tests.
    base_url = os.environ.get(
        "OLLAMA_BASE_URL",
        "http://192.168.1.7:11434",
    ).rstrip("/")

    # Allow the timeout to be overridden by the test suite.
    timeout = int(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "5"))

    try:
        with urlopen(base_url + "/api/tags", timeout=timeout) as response:
            data = json.loads(response.read())
    except (URLError, TimeoutError, OSError, ValueError):
        # Any network or JSON parsing error is reported as disconnected.
        return {
            "connected": False,
            "models": [],
            "message": "Ollama is unavailable",
        }

    # Successful response – normalise the model data.
    return {
        "connected": True,
        "models": [
            {
                "name": model["name"],
                "size": model.get("size"),
            }
            for model in data.get("models", [])
        ],
    }


@app.get("/", include_in_schema=False)
def dashboard():
    index = Path(__file__).resolve().parent / "static" / "index.html"
    return FileResponse(index, media_type="text/html")

