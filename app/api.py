import json
import os
from urllib.error import URLError
from urllib.request import urlopen

from fastapi.responses import FileResponse
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.auth.service import (
    login,
    get_session,
    get_authorized_task,
    list_authorized_tasks,
)
from app.tasks.queue import submit_task


app = FastAPI(
    title="Local AI Team",
    description="Multi-agent AI orchestration platform",
    version="0.1.0",
)

bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class TaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    priority: int = Field(default=5, ge=1, le=10)


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
    try:
        result = get_authorized_task(
            user["user_id"],
            task_id,
            workspace_id,
        )
    except PermissionError:
        raise HTTPException(403, "Workspace access denied")

    if result is None:
        raise HTTPException(404, "Task not found")

    return result


@app.get("/api/ollama/models")
def ollama_models(user=Depends(authenticated_user)):
    base_url = os.environ.get(
        "OLLAMA_BASE_URL",
        "http://127.0.0.1:11434",
    ).rstrip("/")

    try:
        with urlopen(base_url + "/api/tags", timeout=5) as response:
            data = json.loads(response.read())

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

    except (URLError, TimeoutError, ValueError, OSError):
        return {
            "connected": False,
            "models": [],
            "message": "Ollama is unavailable",
        }


@app.get("/", include_in_schema=False)
def dashboard():
    index = Path(__file__).resolve().parent / "static" / "index.html"
    return FileResponse(index, media_type="text/html")
