import time
import uuid
import logging

from app.tasks.queue import (
    claim_task,
    complete_task,
    fail_task,
)

from app.storage.migrations import migrate

from app.orchestration.collaboration import run_collaboration

from app.workspaces.service import (
    get_workspace_context,
    write_workspace_artifact,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("ai-team.worker")


def execute_task(task, worker_id):
    task_id = task["id"]
    workspace_id = task["workspace_id"]

    logger.info(
        "Starting task %s in workspace %s",
        task_id,
        workspace_id,
    )

    try:
        context = get_workspace_context(workspace_id)

        result = run_collaboration(
            task["title"] + "\n\n" + task["description"],
            workspace_id=workspace_id,
            project_root=str(context["project_root"]),
            task_id=task_id,
        )

        outputs = {
            "analysis.md": result["analysis"],
            "critique.md": result["critique"],
            "final_report.md": result["final_report"],
        }

        artifact_paths = {}

        for filename, content in outputs.items():
            path = write_workspace_artifact(
                workspace_id,
                f"{task_id}/{filename}",
                content,
            )
            artifact_paths[filename] = str(path)

        results = {
            "analysis": result["analysis"],
            "critique": result["critique"],
            "final_report": result["final_report"],
            "artifact_paths": str(artifact_paths),
        }

        complete_task(task_id, worker_id, results)

        logger.info(
            "Task completed: %s in workspace %s",
            task_id,
            workspace_id,
        )

    except Exception as error:
        logger.exception("Task failed: %s", task_id)

        fail_task(
            task_id,
            worker_id,
            f"{type(error).__name__}: {error}"
        )


def run_worker(poll_interval=3, once=False):
    migrate()

    worker_id = str(uuid.uuid4())

    logger.info("Worker started: %s", worker_id)

    try:
        while True:
            task = claim_task(worker_id)

            if task is None:
                if once:
                    logger.info("No pending tasks.")
                    break

                time.sleep(poll_interval)
                continue

            execute_task(task, worker_id)

            if once:
                break

    except KeyboardInterrupt:
        logger.info("Worker stopping.")

    finally:
        logger.info("Worker stopped: %s", worker_id)
