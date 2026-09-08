"""MCP (Model Context Protocol) server for yask, spoken over stdio.

Every tool returns plain JSON-serializable data. Actions that would change
the state of multiple tasks come back with ``requires_confirmation`` and the
list of affected tasks; the agent should re-invoke the same tool with
``confirm=true`` once the user agrees.
"""

from __future__ import annotations

import functools

import json

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import TextContent

from .store import Store, YaskError


def _wrap(fn):
    """Return a tool function that turns domain errors into readable results.

    functools.wraps keeps ``__wrapped__`` so the MCP SDK still sees the
    original signature when building the tool schema.
    """

    @functools.wraps(fn)
    def runner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except YaskError as e:
            payload: dict = {"ok": False, "error": str(e)}
            if getattr(e, "affected", None) is not None:
                payload["requires_confirmation"] = True
                payload["affected"] = e.affected
            return payload

    return runner


def build_server(store: Store) -> FastMCP:
    mcp = FastMCP("yask")

    @mcp.tool()
    @_wrap
    def list_projects() -> list[dict]:
        """List all projects with their task counts."""
        return store.list_projects()

    @mcp.tool()
    @_wrap
    def create_project(name: str) -> dict:
        """Create a project. Projects have completely separate state."""
        return store.create_project(name)

    @mcp.tool()
    @_wrap
    def get_project(project_id: int) -> dict:
        """Get a project with its full task tree (epics nest their children)."""
        return store.get_project(project_id)

    @mcp.tool()
    @_wrap
    def list_tasks(
        project_id: int, state: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        """List a project's tasks. Optional state filter; archived hidden by default."""
        return store.list_tasks(project_id, state, include_archived)

    @mcp.tool()
    @_wrap
    def create_task(
        project_id: int,
        title: str,
        type: str = "Task",
        estimate: float | None = None,
        parent_number: int | None = None,
        description: str = "",
    ) -> dict:
        """Create a task (Story/Task/Bug or Epic). Epics take no estimate."""
        return store.create_task(
            project_id, title, type, estimate, parent_number, description
        )

    @mcp.tool()
    @_wrap
    def update_task(
        project_id: int,
        number: int,
        title: str | None = None,
        description: str | None = None,
        type: str | None = None,
        estimate: float | None = None,
        parent_number: int | None = None,
    ) -> dict:
        """Update a task's fields. parent_number null detaches it from its epic."""
        return store.update_task(
            project_id,
            number,
            title=title,
            description=description,
            type_name=type,
            estimate=estimate,
            parent_number=parent_number,
        )

    @mcp.tool()
    @_wrap
    def set_prerequisites(project_id: int, number: int, prereq_numbers: list[int]) -> dict:
        """Replace a task's prerequisite list. Cycles are rejected."""
        return store.set_prerequisites(project_id, number, prereq_numbers)

    @mcp.tool()
    @_wrap
    def move_task(
        project_id: int,
        number: int,
        to_state: str,
        confirm: bool = False,
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        """Move a task to a workflow state (Backlog, Todo, Planning, In progress, Review, Done).

        Prerequisites not yet past the target stage are moved along with it.
        If several tasks are affected and confirm is false, the result contains
        the affected list and nothing is changed.
        """
        return store.move_task(
            project_id, number, to_state, confirm, before_number, after_number
        )

    @mcp.tool()
    @_wrap
    def archive_task(project_id: int, number: int, confirm: bool = False) -> dict:
        """Archive a task (an epic archives its whole subtree)."""
        return store.archive_task(project_id, number, confirm)

    @mcp.tool()
    @_wrap
    def restore_task(
        project_id: int, number: int, to_state: str = "Backlog", confirm: bool = False
    ) -> dict:
        """Restore an archived task to a workflow state (default Backlog)."""
        return store.restore_task(project_id, number, to_state, confirm)

    @mcp.tool()
    @_wrap
    def delete_task(project_id: int, number: int, confirm: bool = False) -> dict:
        """Permanently delete a task (an epic's subtree is removed with it)."""
        return store.delete_task(project_id, number, confirm)

    @mcp.tool()
    @_wrap
    def reorder_task(
        project_id: int,
        number: int,
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        """Reorder a task within its column (default: move to the end)."""
        return store.reorder_task(project_id, number, before_number, after_number)

    @mcp.tool()
    @_wrap
    def get_task_history(project_id: int, number: int) -> list[dict]:
        """Every state change of a task, with timestamps."""
        return store.get_history(project_id, number)

    @mcp.tool()
    @_wrap
    def list_task_types() -> list[dict]:
        """List task types (Story, Task, Bug, Epic, plus any custom ones)."""
        return store.list_task_types()

    @mcp.tool()
    @_wrap
    def add_task_type(name: str) -> dict:
        """Add a new regular (non-epic) task type."""
        return store.create_task_type(name)

    @mcp.tool()
    @_wrap
    def add_attachment(
        project_id: int, number: int, filename: str, data_base64: str,
        content_type: str = "text/markdown",
    ) -> dict:
        """Attach markdown or an image to a task (base64-encoded content)."""
        import base64

        return store.add_attachment(
            project_id, number, filename, content_type, base64.b64decode(data_base64)
        )

    @mcp.tool()
    @_wrap
    def list_attachments(project_id: int, number: int) -> list[dict]:
        """List a task's attachments (metadata only)."""
        return store.list_attachments(project_id, number)

    @mcp.tool()
    @_wrap
    def get_attachment(attachment_id: int):
        """Read an attachment's content.

        Markdown/text attachments come back as a JSON object with the decoded
        text in the "text" field. Images come back as metadata plus a native
        MCP image block so the agent can view them.
        """
        meta, data = store.get_attachment(attachment_id)
        if meta["content_type"].startswith("text/"):
            return {**meta, "text": data.decode("utf-8", errors="replace")}
        fmt = meta["content_type"].split("/", 1)[1]
        return [TextContent(type="text", text=json.dumps(meta)), Image(data=data, format=fmt)]

    @mcp.tool()
    @_wrap
    def delete_attachment(attachment_id: int) -> dict:
        """Delete an attachment permanently."""
        store.delete_attachment(attachment_id)
        return {"deleted": attachment_id}

    return mcp


def run_mcp(store: Store) -> None:
    server = build_server(store)
    server.run()
