"""Google Tasks adapter (reference implementation for adapter authors).

Scope choices (per-method scopes from the Tasks v1 discovery document,
https://developers.google.com/workspace/tasks/auth):

- Read: ``tasks.readonly`` for ``tasklists.list``, ``tasks.list`` and ``tasks.get``.
- Write: ``tasks`` for ``tasks.insert``, ``tasks.patch`` and ``tasks.delete``.
  It is the only scope that allows any Tasks mutation.

API behavior the tools account for (Tasks v1 discovery descriptions):

- ``showCompleted`` alone does not return tasks completed in Google's own
  apps or web UI; ``showHidden`` must also be true, so the list tool sets
  both together.
- ``due`` is an RFC 3339 timestamp, but only its date is stored. The tools
  accept ``YYYY-MM-DD`` and send midnight UTC for it.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="tasks",
    title="Google Tasks",
    api_services=("tasks.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/tasks.readonly",),
    write_scopes=("https://www.googleapis.com/auth/tasks",),
)

_TASK_FIELDS = ("id", "title", "notes", "status", "due", "completed", "parent", "updated")
_STATUSES = frozenset({"needsAction", "completed"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


def normalize_due(value: str) -> str:
    """Accept YYYY-MM-DD or an RFC 3339 timestamp; return what the API expects."""
    try:
        if _DATE_RE.match(value):
            return f"{date.fromisoformat(value).isoformat()}T00:00:00.000Z"
        if _TIMESTAMP_RE.match(value):
            datetime.fromisoformat(value.upper())
            return value
    except ValueError:
        pass
    raise GoogleMCPError(f"due must be YYYY-MM-DD or an RFC 3339 timestamp, got {value!r}")


class TasksAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("tasks_list_tasklists")
        def list_tasklists(max_results: int = 25, page_token: str | None = None) -> JSON:
            """List the user's Google Tasks lists."""
            svc = ctx.client("tasks", "v1")
            resp = ctx.execute(
                svc.tasklists().list(maxResults=clamp_page_size(max_results), pageToken=page_token)
            )
            return {
                "tasklists": [pick(i, ("id", "title", "updated")) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("tasks_list_tasks")
        def list_tasks(
            tasklist_id: str = "@default",
            show_completed: bool = False,
            max_results: int = 25,
            page_token: str | None = None,
        ) -> JSON:
            """List tasks in a task list. Use tasklist_id '@default' for the default list.

            show_completed=true also returns tasks completed in Google's apps.
            """
            svc = ctx.client("tasks", "v1")
            resp = ctx.execute(
                svc.tasks().list(
                    tasklist=tasklist_id,
                    showCompleted=show_completed,
                    showHidden=show_completed,
                    maxResults=clamp_page_size(max_results),
                    pageToken=page_token,
                )
            )
            return {
                "tasks": [pick(i, _TASK_FIELDS) for i in resp.get("items", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("tasks_create_task", write=True)
        def create_task(
            title: str,
            tasklist_id: str = "@default",
            notes: str | None = None,
            due: str | None = None,
        ) -> JSON:
            """Create a task. ``due`` is YYYY-MM-DD or an RFC 3339 timestamp.

            Google keeps only the date part of ``due``.
            """
            body: JSON = {"title": title}
            if notes:
                body["notes"] = notes
            if due:
                body["due"] = normalize_due(due)
            svc = ctx.client("tasks", "v1")
            return pick(
                ctx.execute(svc.tasks().insert(tasklist=tasklist_id, body=body)), _TASK_FIELDS
            )

        @registry.tool("tasks_get_task")
        def get_task(task_id: str, tasklist_id: str = "@default") -> JSON:
            """Get one task by id."""
            svc = ctx.client("tasks", "v1")
            return pick(
                ctx.execute(svc.tasks().get(tasklist=tasklist_id, task=task_id)), _TASK_FIELDS
            )

        def patch(task_id: str, tasklist_id: str, body: JSON) -> JSON:
            svc = ctx.client("tasks", "v1")
            return pick(
                ctx.execute(svc.tasks().patch(tasklist=tasklist_id, task=task_id, body=body)),
                _TASK_FIELDS,
            )

        @registry.tool("tasks_update_task", write=True, idempotent=True)
        def update_task(
            task_id: str,
            tasklist_id: str = "@default",
            title: str | None = None,
            notes: str | None = None,
            due: str | None = None,
            status: str | None = None,
        ) -> JSON:
            """Update a task. Only fields you pass change. status: 'needsAction' or 'completed'.

            due is YYYY-MM-DD or an RFC 3339 timestamp; an empty string clears it.
            """
            body: JSON = {}
            if title is not None:
                body["title"] = title
            if notes is not None:
                body["notes"] = notes
            if due is not None:
                # An empty string clears the due date.
                body["due"] = normalize_due(due) if due else None
            if status is not None:
                if status not in _STATUSES:
                    raise GoogleMCPError(f"status must be one of {sorted(_STATUSES)}")
                body["status"] = status
                if status == "needsAction":
                    # Reopening requires clearing the completion timestamp.
                    body["completed"] = None
            if not body:
                raise GoogleMCPError("nothing to update")
            return patch(task_id, tasklist_id, body)

        @registry.tool("tasks_complete_task", write=True, idempotent=True)
        def complete_task(task_id: str, tasklist_id: str = "@default") -> JSON:
            """Mark a task as completed."""
            return patch(task_id, tasklist_id, {"status": "completed"})

        @registry.tool("tasks_delete_task", write=True, destructive=True, idempotent=True)
        def delete_task(task_id: str, tasklist_id: str = "@default") -> JSON:
            """Delete a task permanently."""
            svc = ctx.client("tasks", "v1")
            ctx.execute(svc.tasks().delete(tasklist=tasklist_id, task=task_id))
            return {"deleted": True, "task_id": task_id, "tasklist_id": tasklist_id}


ADAPTER = TasksAdapter()
