"""Local stdio MCP server that mirrors the tools of mcp-remote-template.

It lets you run the agent and the eval suite without deploying anything. State lives in
memory for the lifetime of the process; seed it with MCP_AGENT_DEMO_SEED (a JSON list).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

Priority = Literal["low", "medium", "high"]
Status = Literal["open", "done"]

mcp = FastMCP("tasks-demo", log_level="WARNING")
_tasks: dict[str, dict[str, object]] = {}
_RANK = {"high": 0, "medium": 1, "low": 2}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new(
    title: str,
    notes: str = "",
    priority: Priority = "medium",
    due_date: str | None = None,
    status: Status = "open",
    task_id: str | None = None,
) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id or uuid.uuid4().hex[:8],
        "title": title,
        "notes": notes,
        "status": status,
        "priority": priority,
        "dueDate": due_date,
        "createdAt": _now(),
    }
    _tasks[str(task["id"])] = task
    return task


def seed(raw: str | None) -> None:
    """Load tasks from JSON: [{"id": "t1", "title": "...", "priority": "high", ...}]."""
    _tasks.clear()
    for item in json.loads(raw or "[]"):
        _new(
            title=item["title"],
            notes=item.get("notes", ""),
            priority=item.get("priority", "medium"),
            due_date=item.get("dueDate"),
            status=item.get("status", "open"),
            task_id=item.get("id"),
        )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_tasks(
    status: Status | None = None,
    priority: Priority | None = None,
    query: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    """List tasks, highest priority and nearest due date first. Optionally filter by status,
    priority or a case-insensitive text query matched against title and notes."""
    q = (query or "").lower().strip()
    rows = [
        t
        for t in _tasks.values()
        if (status is None or t["status"] == status)
        and (priority is None or t["priority"] == priority)
        and (not q or q in str(t["title"]).lower() or q in str(t["notes"]).lower())
    ]
    rows.sort(key=lambda t: (_RANK[str(t["priority"])], str(t["dueDate"] or "9999")))
    return {"count": len(rows[:limit]), "tasks": rows[:limit]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_task(id: str) -> dict[str, object]:
    """Fetch one task by id."""
    if id not in _tasks:
        raise ValueError(f"No task with id {id}.")
    return _tasks[id]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def create_task(
    title: str,
    notes: str = "",
    priority: Priority = "medium",
    dueDate: str | None = None,  # noqa: N803 (wire name shared with mcp-remote-template)
) -> dict[str, object]:
    """Create a new open task. dueDate uses YYYY-MM-DD."""
    return _new(title=title, notes=notes, priority=priority, due_date=dueDate)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def update_task(
    id: str,
    title: str | None = None,
    notes: str | None = None,
    priority: Priority | None = None,
    dueDate: str | None = None,  # noqa: N803 (wire name shared with mcp-remote-template)
    status: Status | None = None,
) -> dict[str, object]:
    """Change any field of an existing task. Only the fields you pass are modified.
    Use status "done" to complete a task."""
    if id not in _tasks:
        raise ValueError(f"No task with id {id}.")
    patch = {"title": title, "notes": notes, "priority": priority, "dueDate": dueDate, "status": status}
    _tasks[id].update({k: v for k, v in patch.items() if v is not None})
    return _tasks[id]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
def delete_task(id: str) -> dict[str, str]:
    """Permanently delete a task. Prefer update_task with status "done" to complete it."""
    if _tasks.pop(id, None) is None:
        raise ValueError(f"No task with id {id}.")
    return {"deleted": id}


def main() -> None:
    seed(os.environ.get("MCP_AGENT_DEMO_SEED"))
    mcp.run("stdio")


if __name__ == "__main__":
    main()
