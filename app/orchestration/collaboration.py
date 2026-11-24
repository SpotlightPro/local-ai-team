import uuid
from datetime import datetime, timezone

from langgraph.graph import StateGraph, START, END
from typing import TypedDict

from app.agents.factory import create_agent
from app.storage.database import connect, transaction


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CollaborationState(TypedDict):
    task: str
    task_id: str
    workspace_id: str
    project_root: str
    analysis: str
    critique: str
    final_report: str


def execute_tracked_agent(
    state,
    agent,
    agent_name,
    provider_name,
    prompt,
):
    execution_id = str(uuid.uuid4())
    started_at = utc_now()

    model = getattr(agent.provider, "model", "unknown")

    task_id = state["task_id"]
    workspace_id = state["workspace_id"]

    with transaction() as db:
        task = db.execute("""
            SELECT 1
            FROM tasks
            WHERE id = ? AND workspace_id = ?
        """, (task_id, workspace_id)).fetchone()

        if task is None:
            raise ValueError(
                "Task does not belong to the specified workspace"
            )

        db.execute("""
            INSERT INTO agent_executions (
                id, task_id, agent_name, provider, model,
                status, input_summary, started_at
            )
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
        """, (
            execution_id,
            task_id,
            agent_name,
            provider_name,
            model,
            prompt[:2000],
            started_at,
        ))

    try:
        output = agent.execute(prompt)

        with connect() as db:
            db.execute("""
                UPDATE agent_executions
                SET status = 'completed',
                    output = ?,
                    completed_at = ?
                WHERE id = ?
                  AND task_id = ?
                  AND EXISTS (
                      SELECT 1 FROM tasks
                      WHERE tasks.id = agent_executions.task_id
                        AND tasks.workspace_id = ?
                  )
            """, (
                output,
                utc_now(),
                execution_id,
                task_id,
                workspace_id,
            ))

        return output

    except Exception as error:
        with connect() as db:
            db.execute("""
                UPDATE agent_executions
                SET status = 'failed',
                    output = ?,
                    completed_at = ?
                WHERE id = ?
                  AND task_id = ?
                  AND EXISTS (
                      SELECT 1 FROM tasks
                      WHERE tasks.id = agent_executions.task_id
                        AND tasks.workspace_id = ?
                  )
            """, (
                f"{type(error).__name__}: {error}"[:4000],
                utc_now(),
                execution_id,
                task_id,
                workspace_id,
            ))

        raise


def workspace_context_prompt(state):
    return f"""
WORKSPACE CONTEXT:
Workspace ID: {state['workspace_id']}
Project root: {state['project_root']}

This task belongs to this workspace.

Do not claim to have accessed or modified files.
Do not claim to have performed external actions.
"""


def analyst_node(state: CollaborationState):
    agent = create_agent(
        name="Independent Analyst",
        role="Primary Analyst",
        instructions="""
        Analyse the task independently.
        Identify important considerations.
        Provide evidence-based conclusions.
        Identify uncertainties and assumptions.
        """,
        provider_name="ollama",
    )

    prompt = (
        workspace_context_prompt(state)
        + "\nTASK:\n"
        + state["task"]
    )

    result = execute_tracked_agent(
        state, agent, "Independent Analyst", "ollama", prompt
    )

    return {"analysis": result}


def reviewer_node(state: CollaborationState):
    agent = create_agent(
        name="Critical Reviewer",
        role="Independent Critical Reviewer",
        instructions="""
        Critically examine the supplied analysis.
        Identify unsupported assumptions.
        Find omissions, contradictions and risks.
        Explain where you agree and disagree.
        Do not automatically agree with the analyst.
        """,
        provider_name="ollama",
    )

    prompt = f"""
    {workspace_context_prompt(state)}

    ORIGINAL TASK:
    {state['task']}

    ANALYST FINDINGS:
    {state['analysis']}

    Critically review these findings.
    """

    result = execute_tracked_agent(
        state, agent, "Critical Reviewer", "ollama", prompt
    )

    return {"critique": result}


def coordinator_node(state: CollaborationState):
    agent = create_agent(
        name="Team Coordinator",
        role="Senior Technical Coordinator",
        instructions="""
        Consolidate the work of multiple AI agents.
        Compare their conclusions.
        Highlight disagreements.
        Distinguish established facts from assumptions.
        Produce practical recommendations.
        Do not hide unresolved disagreements.
        """,
        provider_name="ollama",
    )

    prompt = f"""
    {workspace_context_prompt(state)}

    ORIGINAL TASK:
    {state['task']}

    INDEPENDENT ANALYSIS:
    {state['analysis']}

    CRITICAL REVIEW:
    {state['critique']}

    Produce a consolidated report with:

    1. Executive summary
    2. Points of agreement
    3. Disagreements
    4. Risks and uncertainties
    5. Final recommendations
    """

    result = execute_tracked_agent(
        state, agent, "Team Coordinator", "ollama", prompt
    )

    return {"final_report": result}


def build_collaboration_graph():
    graph = StateGraph(CollaborationState)

    graph.add_node("analyst", analyst_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("coordinator", coordinator_node)

    graph.add_edge(START, "analyst")
    graph.add_edge("analyst", "reviewer")
    graph.add_edge("reviewer", "coordinator")
    graph.add_edge("coordinator", END)

    return graph.compile()


def run_collaboration(
    task: str,
    workspace_id: str,
    project_root: str,
    task_id: str,
):
    workflow = build_collaboration_graph()

    return workflow.invoke({
        "task": task,
        "task_id": task_id,
        "workspace_id": workspace_id,
        "project_root": project_root,
        "analysis": "",
        "critique": "",
        "final_report": "",
    })
