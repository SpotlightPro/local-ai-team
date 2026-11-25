import argparse
import json
import os
import sys

from app.auth.service import (
    get_session,
    require_platform_admin,
    require_workspace_access,
    get_authorized_task,
    list_authorized_tasks,
    list_authorized_agent_executions,
    list_authorized_workspace_audit,
)
from app.tasks.queue import submit_task
from app.tasks.worker import run_worker
from app.storage.migrations import migrate
from app.workspaces.service import (
    create_workspace,
    list_workspaces,
)


def authenticate():
    token = os.environ.get("LOCAL_AI_SESSION_TOKEN")
    session = get_session(token)

    if session is None:
        raise PermissionError(
            "Valid session required. Set LOCAL_AI_SESSION_TOKEN."
        )

    return session


def main():
    parser = argparse.ArgumentParser(
        description="Local Multi-Agent AI Team"
    )

    commands = parser.add_subparsers(
        dest="command", required=True
    )

    submit = commands.add_parser("submit")
    submit.add_argument("--title", required=True)
    submit.add_argument("--description", required=True)
    submit.add_argument("--priority", type=int, default=5)
    submit.add_argument("--workspace", default="default")

    status = commands.add_parser("status")
    status.add_argument("task_id")
    status.add_argument("--workspace", default="default")

    for name in ("tasks", "executions", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", default="default")

    workspace = commands.add_parser("workspace")
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command", required=True
    )

    create = workspace_commands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--root", default=None)

    workspace_commands.add_parser("list")

    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll", type=int, default=3)

    args = parser.parse_args()
    migrate()

    try:
        # Workers run as a separate trusted service.
        if args.command == "worker":
            run_worker(
                poll_interval=args.poll,
                once=args.once,
            )
            return

        session = authenticate()
        user_id = session["user_id"]

        if args.command == "submit":
            require_workspace_access(
                user_id, args.workspace
            )
            task_id = submit_task(
                args.title,
                args.description,
                args.priority,
                args.workspace,
                actor_id=user_id,
            ) 
            print("Task submitted successfully.")
            print("Task ID:", task_id)

        elif args.command == "status":
            result = get_authorized_task(
                user_id, args.task_id, args.workspace
            )
            if result is None:
                print("Task not found in this workspace.")
                return
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.command == "tasks":
            print(json.dumps(
                list_authorized_tasks(
                    user_id, args.workspace
                ),
                indent=2, ensure_ascii=False
            ))

        elif args.command == "executions":
            print(json.dumps(
                list_authorized_agent_executions(
                    user_id, args.workspace
                ),
                indent=2, ensure_ascii=False
            ))

        elif args.command == "audit":
            print(json.dumps(
                list_authorized_workspace_audit(
                    user_id, args.workspace
                ),
                indent=2, ensure_ascii=False
            ))

        elif args.command == "workspace":
            require_platform_admin(user_id)

            if args.workspace_command == "create":
                result = create_workspace(
                    args.name,
                    args.description,
                    args.root,
                )
                print(json.dumps(result, indent=2))

            elif args.workspace_command == "list":
                print(json.dumps(
                    list_workspaces(), indent=2
                ))

    except PermissionError as exc:
        print(f"Access denied: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
