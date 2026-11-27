import json
import os
import secrets
import uuid

from pathlib import Path
import stat

from app.storage.database import connect


BASE_DIR = Path(__file__).resolve().parents[2]
WORKSPACES_DIR = BASE_DIR / "workspaces"

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | os.O_CLOEXEC
)


def _validate_workspace_id(workspace_id):
    if workspace_id == "default":
        return

    try:
        uuid.UUID(str(workspace_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid workspace ID")


def _paths_overlap(first, second):
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _absolute_path(path):
    return Path(os.path.abspath(os.fspath(path)))


def _open_directory(path, create=False):
    """
    Open a directory by walking from the filesystem root.

    Every component is opened with O_NOFOLLOW and O_DIRECTORY.
    Directory creation is performed relative to an already-open
    parent directory descriptor.
    """
    path = _absolute_path(path)

    if "\x00" in str(path):
        raise ValueError("Invalid filesystem path")

    fd = os.open("/", _DIRECTORY_FLAGS)

    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=fd,
                )

            except FileNotFoundError:
                if not create:
                    raise

                try:
                    os.mkdir(
                        component,
                        mode=0o700,
                        dir_fd=fd,
                    )
                except FileExistsError:
                    pass

                next_fd = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=fd,
                )

            os.close(fd)
            fd = next_fd

        result = fd
        fd = -1
        return result

    finally:
        if fd >= 0:
            os.close(fd)


def _open_child_directory(parent_fd, name, create=False):
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError("Invalid directory component")

    try:
        return os.open(
            name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_fd,
        )

    except FileNotFoundError:
        if not create:
            raise

        try:
            os.mkdir(
                name,
                mode=0o700,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            pass

        return os.open(
            name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_fd,
        )


def require_workspace(workspace_id, db=None):
    _validate_workspace_id(workspace_id)

    own_connection = db is None
    connection = db or connect()

    try:
        workspace = connection.execute(
            """
            SELECT *
            FROM workspaces
            WHERE id = ? AND active = 1
            """,
            (workspace_id,),
        ).fetchone()

        if workspace is None:
            raise ValueError(
                f"Workspace not found or inactive: {workspace_id}"
            )

        return dict(workspace)

    finally:
        if own_connection:
            connection.close()


def get_workspace(workspace_id):
    try:
        return require_workspace(workspace_id)
    except ValueError:
        return None


def list_workspaces(include_inactive=False):
    with connect() as db:
        if include_inactive:
            rows = db.execute(
                "SELECT * FROM workspaces ORDER BY created_at"
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT * FROM workspaces
                WHERE active = 1
                ORDER BY created_at
                """
            ).fetchall()

        return [dict(row) for row in rows]


def create_workspace(name, description="", root_path=None):
    if not isinstance(name, str):
        raise ValueError("Invalid workspace name")

    name = name.strip()

    if not name:
        raise ValueError("Workspace name cannot be empty")

    if len(name) > 120:
        raise ValueError("Workspace name is too long")

    workspace_id = str(uuid.uuid4())

    project_root = (
        _absolute_path(Path(root_path).expanduser())
        if root_path
        else _absolute_path(
            WORKSPACES_DIR / workspace_id / "project"
        )
    )

    managed_root = _absolute_path(
        WORKSPACES_DIR / workspace_id
    )

    artifacts_root = managed_root / "artifacts"

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = db.execute(
            """
            SELECT id, root_path
            FROM workspaces
            """
        ).fetchall()

        for workspace in existing:
            other_managed = _absolute_path(
                WORKSPACES_DIR / workspace["id"]
            )

            if _paths_overlap(project_root, other_managed):
                raise ValueError(
                    "Project root overlaps another workspace's "
                    "managed directory: " + workspace["id"]
                )

            if workspace["root_path"]:
                other_root = _absolute_path(
                    workspace["root_path"]
                )

                if _paths_overlap(project_root, other_root):
                    raise ValueError(
                        "Project root overlaps workspace: "
                        + workspace["id"]
                    )

        project_fd = _open_directory(
            project_root,
            create=True,
        )
        os.close(project_fd)

        artifacts_fd = _open_directory(
            artifacts_root,
            create=True,
        )
        os.close(artifacts_fd)

        db.execute(
            """
            INSERT INTO workspaces (
                id, name, description, root_path
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                workspace_id,
                name,
                description,
                str(project_root),
            ),
        )

        db.execute(
            """
            INSERT INTO audit_events (
                event_type, actor, resource_type,
                resource_id, details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "workspace.created",
                "user",
                "workspace",
                workspace_id,
                json.dumps({"name": name}),
            ),
        )

        db.commit()

    return get_workspace(workspace_id)


def get_workspace_context(workspace_id, db=None):
    workspace = require_workspace(workspace_id, db)

    if not workspace["root_path"]:
        raise ValueError("Workspace has no configured project root")

    project_root = _absolute_path(
        Path(workspace["root_path"]).expanduser()
    )

    managed_root = _absolute_path(
        WORKSPACES_DIR / workspace_id
    )

    # A project's root must not expose another workspace's
    # managed directory through its filesystem tree.
    own_connection = db is None
    connection = db or connect()

    try:
        rows = connection.execute(
            "SELECT id FROM workspaces WHERE id != ?",
            (workspace_id,),
        ).fetchall()

        for row in rows:
            other_managed = _absolute_path(
                WORKSPACES_DIR / row["id"]
            )

            if _paths_overlap(project_root, other_managed):
                raise ValueError(
                    "Project root overlaps another workspace's "
                    "managed directory"
                )

    finally:
        if own_connection:
            connection.close()

    project_fd = _open_directory(project_root)

    try:
        project_root = Path(os.path.realpath(
            f"/proc/self/fd/{project_fd}"
        ))
    finally:
        os.close(project_fd)

    managed_fd = _open_directory(managed_root)

    try:
        artifacts_fd = _open_child_directory(
            managed_fd,
            "artifacts",
            create=True,
        )

        try:
            artifacts_root = Path(os.path.realpath(
                f"/proc/self/fd/{artifacts_fd}"
            ))
        finally:
            os.close(artifacts_fd)

    finally:
        os.close(managed_fd)

    return {
        "workspace_id": workspace_id,
        "workspace_name": workspace["name"],
        "project_root": project_root,
        "artifacts_root": artifacts_root,
    }


def _open_workspace_artifacts(workspace_id):
    """
    Open the workspace artifact directory securely.

    Return an open directory descriptor so callers can perform
    all filesystem operations relative to the validated directory.
    """
    workspace = require_workspace(workspace_id)

    if not workspace["root_path"]:
        raise ValueError("Workspace has no configured project root")

    project_root = _absolute_path(
        Path(workspace["root_path"]).expanduser()
    )

    managed_root = _absolute_path(
        WORKSPACES_DIR / workspace_id
    )

    # Preserve the existing cross-workspace containment checks.
    with connect() as db:
        rows = db.execute(
            "SELECT id FROM workspaces WHERE id != ?",
            (workspace_id,),
        ).fetchall()

        for row in rows:
            other_managed = _absolute_path(
                WORKSPACES_DIR / row["id"]
            )

            if _paths_overlap(project_root, other_managed):
                raise ValueError(
                    "Project root overlaps another workspace's "
                    "managed directory"
                )

    # Verify the configured project root contains no symlink
    # components, using the existing descriptor-based traversal.
    project_fd = _open_directory(project_root)
    os.close(project_fd)

    # Keep the managed directory anchored by its descriptor.
    managed_fd = _open_directory(managed_root)

    try:
        artifacts_fd = _open_child_directory(
            managed_fd,
            "artifacts",
            create=True,
        )
    finally:
        os.close(managed_fd)

    return artifacts_fd


def write_workspace_artifact(workspace_id, relative_path, content):
    if not isinstance(content, str):
        raise ValueError("Artifact content must be text")

    relative_path = Path(relative_path)

    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
        or "\x00" in str(relative_path)
    ):
        raise ValueError("Invalid artifact path")

    # Crucially, this descriptor remains open throughout the write.
    artifacts_fd = _open_workspace_artifacts(workspace_id)

    parent_fd = artifacts_fd
    opened_fds = []

    try:
        artifacts_root = Path(os.path.realpath(
            f"/proc/self/fd/{artifacts_fd}"
        ))

        for component in relative_path.parts[:-1]:
            next_fd = _open_child_directory(
                parent_fd,
                component,
                create=True,
            )

            opened_fds.append(next_fd)
            parent_fd = next_fd

        filename = relative_path.parts[-1]

        if filename in ("", ".", "..") or "/" in filename:
            raise ValueError("Invalid artifact filename")

        temporary_name = (
            "."
            + filename
            + "."
            + secrets.token_hex(16)
            + ".tmp"
        )

        file_fd = os.open(
            temporary_name,
            (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
            ),
            0o600,
            dir_fd=parent_fd,
        )

        try:
            remaining = memoryview(content.encode("utf-8"))

            while remaining:
                written = os.write(file_fd, remaining)

                if written <= 0:
                    raise OSError("Artifact write failed")

                remaining = remaining[written:]

            os.fsync(file_fd)

        except Exception:
            os.close(file_fd)

            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

            raise

        else:
            os.close(file_fd)

        try:
            os.rename(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )

            os.fsync(parent_fd)

        except Exception:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

            raise

    finally:
        for fd in reversed(opened_fds):
            os.close(fd)

        os.close(artifacts_fd)

    return artifacts_root / relative_path

# ---------------------------------------------------------------------------
# Binary image helpers – Phase 1, Task 1
# ---------------------------------------------------------------------------

def write_workspace_image(
    workspace_id: str,
    relative_path: str,
    image_bytes: bytes,
):
    """Write binary image data to the workspace artifact directory.

    The implementation mirrors the existing text‑artifact writer but
    operates on raw ``bytes``.  It uses the same descriptor‑based
    traversal and symlink protection that the text writer relies on.

    Parameters
    ----------
    workspace_id:
        Identifier of the workspace.
    relative_path:
        Path relative to the artifacts root.  Must be a non‑empty
        relative path without ``..`` components, null bytes, or
        leading slashes.
    image_bytes:
        Binary image payload.

    Returns
    -------
    pathlib.Path
        Absolute path of the written file.
    """
    # Validate type of payload
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise ValueError("image_bytes must be bytes")

    relative_path = Path(relative_path)

    # Path validation – same as write_workspace_artifact
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
        or "\x00" in str(relative_path)
    ):
        raise ValueError("Invalid artifact path")

    # Open the artifacts directory using the existing helper
    artifacts_fd = _open_workspace_artifacts(workspace_id)
    parent_fd = artifacts_fd
    opened_fds = []

    try:
        # Walk/create parent directories
        for component in relative_path.parts[:-1]:
            next_fd = _open_child_directory(parent_fd, component, create=True)
            opened_fds.append(next_fd)
            parent_fd = next_fd

        filename = relative_path.parts[-1]
        if filename in ("", ".", "..") or "/" in filename:
            raise ValueError("Invalid artifact filename")

        # Create a unique temporary file in the target directory
        temp_name = f".{filename}.{secrets.token_hex(16)}.tmp"
        file_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )

        try:
            # Write the image bytes atomically
            remaining = memoryview(image_bytes)
            while remaining:
                written = os.write(file_fd, remaining)
                if written <= 0:
                    raise OSError("Artifact write failed")
                remaining = remaining[written:]
            os.fsync(file_fd)
        except Exception:
            os.close(file_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(file_fd)

        # Atomically rename temp to final name; rename will fail if
        # destination exists, satisfying the "never overwrite" rule.
        # Use os.link to atomically create the destination file only if it
        # does not already exist.  This guarantees a no‑overwrite
        # operation that is safe from race conditions.
        try:
            os.link(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            # Ensure the directory entry is flushed to disk.
            os.fsync(parent_fd)
            # Remove the temporary file now that it has been safely
            # linked into the final location.
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileExistsError:
            # Destination already existed – clean up the temp file and
            # raise a clear error.
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise ValueError("Destination file already exists")
        except Exception:
            # Ensure the temporary file is removed on any other error.
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise

        # Resolve the absolute path of the created file
        artifacts_root = Path(os.path.realpath(f"/proc/self/fd/{artifacts_fd}"))
        return artifacts_root / relative_path

    finally:
        for fd in reversed(opened_fds):
            os.close(fd)
        os.close(artifacts_fd)


def read_workspace_image(
    workspace_id: str,
    relative_path: str,
    max_bytes: int = 10485760,
):
    """Read binary image data from the workspace artifact directory.

    Parameters
    ----------
    workspace_id:
        Identifier of the workspace.
    relative_path:
        Path relative to the artifacts root.  Must be validated the
        same way as in the writer.
    max_bytes:
        Maximum number of bytes to read.  Reading stops when this
        threshold is reached or the file ends.  The function raises
        ``ValueError`` if the file is larger than ``max_bytes``.
    """
    relative_path = Path(relative_path)

    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
        or "\x00" in str(relative_path)
    ):
        raise ValueError("Invalid artifact path")

    artifacts_fd = _open_workspace_artifacts(workspace_id)
    parent_fd = artifacts_fd
    opened_fds = []

    try:
        # Traverse to the directory containing the file
        for component in relative_path.parts[:-1]:
            next_fd = _open_child_directory(parent_fd, component, create=False)
            opened_fds.append(next_fd)
            parent_fd = next_fd

        filename = relative_path.parts[-1]
        if filename in ("", ".", "..") or "/" in filename:
            raise ValueError("Invalid artifact filename")

        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            # Verify regular file
            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("Requested artifact is not a regular file")
            # Read up to max_bytes, but read one extra byte to detect
            # overflow.  This ensures that a file exactly equal to
            # max_bytes is accepted, while larger files trigger an error.
            chunks = []
            total = 0
            while True:
                to_read = min(8192, max_bytes + 1 - total)
                if to_read <= 0:
                    # No more bytes can be read without exceeding the limit
                    break
                chunk = os.read(file_fd, to_read)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("File exceeds max_bytes limit")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        for fd in reversed(opened_fds):
            os.close(fd)
        os.close(artifacts_fd)


def delete_workspace_image(workspace_id: str, relative_path: str) -> None:
    """Delete a single image artifact.

    The function mirrors the path‑validation logic used by the binary writer
    and performs the deletion in a descriptor‑based, symlink‑safe manner.

    Parameters
    ----------
    workspace_id:
        Identifier of the workspace whose artifact tree contains the file.
    relative_path:
        Artifact path relative to the workspace's artifact root.  Must be a
        non‑empty relative path with no ``..`` components, null bytes or
        leading slashes.
    """

    # ---- Path validation -------------------------------------------------
    rp = Path(relative_path)
    if (
        rp.is_absolute()
        or not rp.parts
        or ".." in rp.parts
        or "\x00" in str(rp)
    ):
        raise ValueError("Invalid artifact path")

    # ---- Open the artifact root securely ---------------------------------
    artifacts_fd = _open_workspace_artifacts(workspace_id)
    parent_fd = artifacts_fd
    opened_fds = []

    try:
        # Walk the path components without creating any new directories.
        for comp in rp.parts[:-1]:
            fd = _open_child_directory(parent_fd, comp, create=False)
            opened_fds.append(fd)
            parent_fd = fd

        filename = rp.parts[-1]
        if filename in ("", ".", "..") or "/" in filename:
            raise ValueError("Invalid artifact filename")

        # Ensure the target is a regular file (reject symlinks, dirs, etc.).
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("Requested artifact is not a regular file")
        finally:
            os.close(file_fd)

        # Perform the unlink relative to the open directory descriptor.
        os.unlink(filename, dir_fd=parent_fd)
    finally:
        # Clean up all descriptors in reverse order.
        for fd in reversed(opened_fds):
            os.close(fd)
        os.close(artifacts_fd)
