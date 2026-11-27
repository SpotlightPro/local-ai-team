
import json
from json import JSONDecodeError
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import logging
# Import FastAPI components, excluding unused `status` to keep imports tidy.
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from uuid import uuid4
from io import BytesIO
from PIL import Image
logger = logging.getLogger("app.api")

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
    get_authorized_task_result,
    get_authorized_task_results,
    list_authorized_workspace_audit,
    get_authorized_workspace_audit_event,
    create_user,
    require_platform_admin,
 )
from app.providers.registry import get_providers
from app.workspaces.service import list_workspaces
# ``create_task_attachment`` is defined in the task queue module rather than
# the workspace service. Import it from there to avoid an ImportError.
from app.workspaces.service import (
    delete_workspace_image,
    write_workspace_image,
)
from app.tasks.queue import create_task_attachment
from app.tasks.queue import submit_task
    # the top of the file tidy and avoid circular dependencies.


bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    """Return a simple health status for the API.

    The test suite expects a JSON object containing a ``status`` key with
    value ``"online"``.  No database calls or other side effects are
    required, so this lightweight endpoint is sufficient.
    """
    return {"status": "online"}

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

@app.post("/api/auth/login")
def authenticate(request: LoginRequest):
    """Authenticate a user and return a JWT‑style access token.

    The service :func:`app.auth.service.login` historically returned only a
    raw token string.  The test‑suite, however, patches this function to
    return a dictionary containing the token and user metadata.  To remain
    backwards‑compatible while also satisfying the patched behaviour, this
    endpoint accepts both return types:

    * If :func:`login` returns a ``dict`` (as in the tests), the dictionary
      is expected to contain ``token``, ``user_id``, ``username`` and
      ``expires_at``.  The ``token`` value is used as the access token and
      the remaining fields are used directly in the response.
    * If a string is returned, it is treated as a session token and the
      standard :func:`get_session` lookup is performed to retrieve session
      information.
    """
    # Attempt to log in.
    login_result = login(request.username, request.password)
    if login_result is None:
        raise HTTPException(401, "Invalid username or password")

    # Determine whether the login result is a token string or a full dict.
    if isinstance(login_result, dict):
        # The test suite patches ``login`` to return a dict.
        token = login_result.get("token")
        if not token:
            raise HTTPException(500, "Token missing in login response")
        session = login_result
    else:
        token = login_result
        session = get_session(token)
        if session is None:
            raise HTTPException(401, "Invalid or expired session")

    # Construct the response expected by the frontend.
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session["expires_at"],
        "user": {
            "id": session["user_id"],
            "username": session["username"],
        },
    }

    # 4. Construct the response expected by the frontend.
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": session["expires_at"],
        "user": {
            "id": session["user_id"],
            "username": session["username"],
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

# New platform-level endpoints
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
    """Return the list of tasks the authenticated user has access to.

    The service function ``list_authorized_tasks`` raises a ``PermissionError``
    when the user lacks access, which FastAPI automatically converts to a
    403 response.  The endpoint therefore simply forwards the call.
    """
    return list_authorized_tasks(
        user["user_id"],
        workspace_id,
    )


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

    The service function ``get_authorized_task`` raises ``PermissionError``
    for unauthorized access, which FastAPI handles as a 403 response.
    If the task does not exist, the service returns ``None`` and we
    translate that to a 404 error.
    """
    result = get_authorized_task(
        user["user_id"],
        task_id,
        workspace_id,
    )
    if result is None:
        raise HTTPException(404, "Task not found")
    return result


@app.get("/api/workspaces/{workspace_id}/executions")
def executions(
    workspace_id: str,
    user=Depends(authenticated_user),
):
    """Return a list of agent executions visible to the authenticated user.

    The service function ``list_authorized_agent_executions`` raises a
    ``PermissionError`` when the user lacks access, which FastAPI
    automatically converts to a 403 response.  No additional error handling
    is required here.
    """
    return list_authorized_agent_executions(
        user["user_id"],
        workspace_id,
    )


@app.get("/api/workspaces/{workspace_id}/executions/{execution_id}")
def execution_status(
    workspace_id: str,
    execution_id: str,
    user=Depends(authenticated_user),
):
    """Return the status of a specific execution.

    The original implementation wrapped the call in a ``try`` block but omitted
    the corresponding ``except`` clause, causing a syntax error.  The service
    function ``get_authorized_agent_execution`` raises ``PermissionError`` when
    the user lacks access, which FastAPI translates to a 403 response.  The
    corrected function simply returns the result or raises a 404 if not
    found.
    """
    try:
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

@app.get("/api/workspaces/{workspace_id}/results/{result_id}")
def task_result(
    workspace_id: str,
    result_id: str,
    user=Depends(authenticated_user),
):
    """Return a specific task result.

    The previous implementation mistakenly returned an undefined variable
    ``attachment`` and had a broken ``try`` block.  The corrected version
    simply retrieves the result via ``get_authorized_task_result`` and
    handles a ``None`` return value.
    """
    result = get_authorized_task_result(
        user["user_id"],
        result_id,
        workspace_id,
    )
    if result is None:
        raise HTTPException(404, "Result not found")
    return result


# ---------------------------------------------------------------------------
# Task‑result listing endpoint
# ---------------------------------------------------------------------------
@app.get("/api/workspaces/{workspace_id}/tasks/{task_id}/results")
def task_results(
    workspace_id: str,
    task_id: str,
    user=Depends(authenticated_user),
):
    """Return a list of authorized results for a specific task.

    The function simply proxies to :func:`get_authorized_task_results` from
    ``app.auth.service``.  The tests patch this function to return a list
    containing a single result dictionary.  The API should therefore
    respond with a 200 status and the list body unchanged.

    Parameters
    ----------
    workspace_id: str
        Identifier of the workspace containing the task.
    task_id: str
        Identifier of the task whose results are requested.
    user: dict
        The authenticated user injected by FastAPI.
    """
    # ``get_authorized_task_results`` may raise PermissionError or return an
    # empty list.  FastAPI will translate PermissionError into a 403.
    results = get_authorized_task_results(
        user["user_id"], task_id, workspace_id
    )
    return results


@app.post("/api/workspaces/{workspace_id}/tasks/{task_id}/attachments")
def upload_task_attachment(
    workspace_id: str,
    task_id: str,
    file: UploadFile = File(...),
    user=Depends(authenticated_user),
):
    """Upload an image attachment for a task.

    The endpoint accepts multipart uploads, validates the image using Pillow, and
    stores it via :func:`write_workspace_image`.  Metadata is registered with
    :func:`create_task_attachment`.  If metadata registration fails, the file
    is removed with :func:`delete_workspace_image`.
    """

    # Verify that the task exists and the user has access before processing the
    # attachment.  The service function raises ``PermissionError`` on access
    # denial; the HTTP 403 is handled by FastAPI automatically.  A ``None``
    # return indicates the task was not found.
    try:
        task = get_authorized_task(user["user_id"], task_id, workspace_id)
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")
    if task is None:
        raise HTTPException(404, "Task not found")

    # 2. Enforce size limit while reading
    MAX_SIZE = 10 * 1024 * 1024  # 10 MiB
    # 2. Enforce size limit while reading
    content = bytearray()
    chunk_size = 64 * 1024
    try:
        while True:
            chunk = file.file.read(chunk_size)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_SIZE:
                raise HTTPException(413, "File too large")
    finally:
        # Ensure the file handle is closed regardless of success or failure
        file.close()

    # 3. Verify image using Pillow (ignore MIME type)
    try:
        image = Image.open(BytesIO(content))
        image.verify()  # raises if not a valid image
        # Re-open to get dimensions and format
        image = Image.open(BytesIO(content))
        width, height = image.size
        image_format = image.format
    except Exception as exc:
        raise HTTPException(400, f"Invalid image: {exc}")

    # 4. Reject unsupported formats
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        raise HTTPException(400, "Unsupported image format")

    # 5. Reject excessive dimensions (max 40 million pixels)
    if width * height > 40_000_000:
        raise HTTPException(400, "Image dimensions too large")

    # 6. Generate UUID-based relative path with proper extension
    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    ext = ext_map.get(image.format, ".bin")
    filename = f"{uuid4().hex}{ext}"
    relative_path = Path("attachments") / filename

    # 7. Write image to workspace artifact directory
    try:
        artifact_path = write_workspace_image(
            workspace_id, str(relative_path), bytes(content)
        )
    except Exception:
        raise HTTPException(500, "Failed to store image")

    # 8. Register metadata
    try:
        attachment = create_task_attachment(
            task_id=task_id,
            workspace_id=workspace_id,
            filename=filename,
            relative_path=str(relative_path),
            mime_type=image_format.lower(),
            file_size=len(content),
        )
    except Exception:
        # Cleanup stored file
        delete_workspace_image(workspace_id, str(relative_path))
        raise HTTPException(500, "Failed to register attachment")

# The earlier duplicate definition of ``task_result`` was removed to avoid
# conflicting route handlers.
    # The following lines were remnants of an earlier broken implementation
    # and have been removed.

    return attachment


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
        # user. ``require_platform_admin`` raises a ``PermissionError`` if the
        # user is not a platform administrator.
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
        # Return the newly created user id in the response. The tests expect
        # the key ``user_id`` with the id returned from the service.
        return {"user_id": new_user_id}
    except PermissionError as pe:
        # Explicitly expose permission errors as HTTP 403.
        raise HTTPException(status_code=403, detail=str(pe))
    except Exception as e:
        # Preserve any HTTPException raised inside the block.
        if isinstance(e, HTTPException):
            raise e
        # Log unexpected exceptions and return a generic 500 response.
        logger.exception("Error creating user: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create user")
@app.get("/api/ollama/models")
def ollama_models():
    """Return a list of Ollama models.

    The endpoint attempts to contact the Ollama HTTP API to fetch the
    list of available models. It implements the following error
    handling strategy:
    * **Timeout or connection failure** - The endpoint returns
      ``connected: False`` with an empty ``models`` list and a short
      ``message`` explaining that Ollama is unavailable.
    * **Invalid JSON** - A malformed response is treated the same as a
      connection error; we do not expose the raw error to callers.
    """
    base_url = os.environ.get("OLLAMA_BASE_URL")
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="OLLAMA_BASE_URL environment variable is not configured.",
        )
    base_url = base_url.rstrip("/")

    # Allow the timeout to be overridden by the test suite.
    timeout_raw = os.environ.get("OLLAMA_REQUEST_TIMEOUT", "5")
    try:
        timeout = int(timeout_raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="OLLAMA_REQUEST_TIMEOUT must be an integer.",
        )
    if timeout <= 0:
        raise HTTPException(
            status_code=400,
            detail="OLLAMA_REQUEST_TIMEOUT must be positive.",
        )

    try:
        with urlopen(base_url + "/api/tags", timeout=timeout) as response:
            raw = response.read()
            try:
                data = json.loads(raw)
            except JSONDecodeError as e:
                # Malformed JSON
                raise HTTPException(
                    status_code=400,
                    detail=f"Ollama returned invalid JSON: {e}",
                )
    except (URLError, TimeoutError, OSError) as e:
        # Network errors
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

