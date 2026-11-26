import time
import uuid
import logging
import threading

from app.tasks.queue import (
    claim_task,
    complete_task,
    fail_task,
    renew_task_lease,
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

LEASE_RENEW_INTERVAL = 60


def maintain_task_lease(
    task_id,
    worker_id,
    stop_event,
    lost_event,
):
    """Renew the task lease while the worker owns it."""

    while not stop_event.wait(LEASE_RENEW_INTERVAL):
        try:
            renewed = renew_task_lease(
                task_id,
                worker_id,
            )

            if not renewed:
                logger.error(
                    "Task lease lost: %s",
                    task_id,
                )
                lost_event.set()
                return

            logger.info(
                "Task lease renewed: %s",
                task_id,
            )

        except Exception:
            logger.exception(
                "Task lease renewal failed: %s",
                task_id,
            )

            # Stop processing if ownership cannot be verified.
            lost_event.set()
            return

def execute_task(task, worker_id):
    task_id = task["id"]
    workspace_id = task["workspace_id"]

    logger.info(
        "Starting task %s in workspace %s",
        task_id,
        workspace_id,
    )

    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()

    heartbeat = threading.Thread(
        target=maintain_task_lease,
        args=(
            task_id,
            worker_id,
            stop_heartbeat,
            lease_lost,
        ),
        name=f"lease-{task_id}",
        daemon=True,
    )

    heartbeat.start()

    try:
        context = get_workspace_context(workspace_id)

        result = run_collaboration(
            task["title"] + "\n\n" + task["description"],
            workspace_id=workspace_id,
            project_root=str(context["project_root"]),
            task_id=task_id,
            cancellation_event=lease_lost,
        )

        if lease_lost.is_set():
            raise RuntimeError(
                "Task ownership lost during AI execution"
            )

        outputs = {
            "analysis.md": result["analysis"],
            "critique.md": result["critique"],
            "final_report.md": result["final_report"],
        }

        artifact_paths = {}

        for filename, content in outputs.items():

            if lease_lost.is_set():
                raise RuntimeError(
                    "Task ownership lost during artifact generation"
                )

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

        # Stop heartbeat before completing the task.
        stop_heartbeat.set()
        heartbeat.join()

        if lease_lost.is_set():
            raise RuntimeError(
                "Cannot complete task: lease ownership lost"
            )

        # complete_task independently verifies worker ownership
        # and lease validity inside its database transaction.
        complete_task(
            task_id,
            worker_id,
            results,
        )

        logger.info(
            "Task completed: %s in workspace %s",
            task_id,
            workspace_id,
        )

    except Exception as error:
        logger.exception(
            "Task failed: %s",
            task_id,
        )

        try:
            fail_task(
                task_id,
                worker_id,
                f"{type(error).__name__}: {error}",
            )
        except Exception:
            logger.exception(
                "Could not record task failure: %s",
                task_id,
            )

    finally:
        stop_heartbeat.set()

        if heartbeat.is_alive():
            heartbeat.join(timeout=5)


def run_worker(poll_interval=3, once=False):
    migrate()

    worker_id = str(uuid.uuid4())

    logger.info(
        "Worker started: %s",
        worker_id,
    )

    try:
        while True:
            task = claim_task(worker_id)

            if task is None:
                if once:
                    logger.info("No pending tasks.")
                    break

                time.sleep(poll_interval)
                continue

            execute_task(
                task,
                worker_id,
            )

            if once:
                break

    except KeyboardInterrupt:
        logger.info("Worker stopping.")

    finally:
        logger.info(
            "Worker stopped: %s",
            worker_id,
        )


if __name__ == "__main__":
    run_worker()
