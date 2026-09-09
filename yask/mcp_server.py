"""MCP (Model Context Protocol) server for yask, spoken over stdio.

Every tool returns plain JSON-serializable data. Actions that would change
the state of multiple tasks come back with ``requires_confirmation`` and the
list of affected tasks; the agent should re-invoke the same tool with
``confirm=true`` once the user agrees.
"""

from __future__ import annotations

import functools
import inspect

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import TextContent

from .store import NotFound, ValidationError, Store, YaskError


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


def _resolve_project(store: Store, project) -> int:
    """Resolve a project identity (name or numeric id) to its project id.

    Accepts a project name (case-insensitive) or a numeric id; a numeric
    string is also accepted. Raises a domain error when it cannot resolve.
    """
    if isinstance(project, bool):
        raise ValidationError(f"invalid project '{project}': expected a name or id")
    if isinstance(project, int):
        return store._get_project(project)["id"]
    text = str(project).strip()
    if text.isdigit():
        try:
            return store._get_project(int(text))["id"]
        except NotFound:
            pass  # not a numeric id; fall through to case-insensitive name match
    key = text.lower()
    for p in store.list_projects():
        if p["name"].lower() == key:
            return p["id"]
    raise NotFound(f"project '{project}' not found")


def _project_arg(store: Store):
    """Let a project-scoped tool identify its project by name or id.

    Renames the first parameter (``project_id``) to ``project`` in the
    exposed tool schema and resolves it to an internal project id before the
    tool body runs, so bodies keep taking ``project_id``.
    """

    def decorate(fn):
        params = list(inspect.signature(fn).parameters.values())
        renamed = [params[0].replace(name="project", annotation=str | int)] + params[1:]
        new_sig = inspect.signature(fn).replace(parameters=renamed)

        @functools.wraps(fn)
        def runner(*args, **kwargs):
            if args:
                kwargs = dict(kwargs)
                kwargs.setdefault("project", args[0])
                args = args[1:]
            if "project" not in kwargs:
                raise TypeError("missing required argument 'project'")
            kwargs["project_id"] = _resolve_project(store, kwargs.pop("project"))
            return fn(*args, **kwargs)

        runner.__signature__ = new_sig
        return runner

    return decorate


_EXT_TO_CONTENT_TYPE = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def _read_attachment_file(file_path: str) -> tuple[str, bytes, str]:
    """Read an attachment off the local filesystem after basic sanity checks.

    Returns ``(filename, data, content_type)`` derived from the given path.
    Raises a domain error for missing files, oversized files, or extensions
    whose content type cannot be determined.
    """
    path = Path(file_path)
    if not path.is_file():
        raise NotFound(f"attachment file not found: {file_path}")
    size = path.stat().st_size
    if size > Store.MAX_ATTACHMENT_SIZE:
        raise ValidationError("attachment exceeds 10 MB limit")
    ext = path.suffix.lower()
    content_type = _EXT_TO_CONTENT_TYPE.get(ext)
    if content_type is None:
        raise ValidationError(
            f"cannot infer content type from extension '{path.suffix}'; pass content_type explicitly"
        )
    return path.name, path.read_bytes(), content_type


def build_server(store: Store) -> FastMCP:
    mcp = FastMCP("yask")

    @mcp.tool()
    @_wrap
    def list_projects():
        """List all projects with their task counts."""
        return store.list_projects()

    @mcp.tool()
    @_wrap
    def create_project(name: str) -> dict:
        """Create a project. Projects have completely separate state."""
        return store.create_project(name)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def get_project(project_id: int) -> dict:
        """Get a project with its full task tree (epics nest their children)."""
        return store.get_project(project_id)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def list_tasks(
        project_id: int,
        state: str | None = None,
        include_archived: bool = False,
        label: str | None = None,
    ):
        """List a project's tasks. Optional state/label filters; archived hidden by default."""
        return store.list_tasks(project_id, state, include_archived, label)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def create_task(
        project_id: int,
        title: str,
        type: str = "Task",
        estimate: float | None = None,
        parent_number: int | None = None,
        description: str = "",
    ) -> dict:
        """Create a task (Story/Task/Bug or Epic). Epics take no estimate.
        New tasks always start in the Backlog; move them forward separately."""
        return store.create_task(
            project_id, title, type, estimate, parent_number, description
        )

    @mcp.tool()
    @_wrap
    @_project_arg(store)
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
            type=type,
            estimate=estimate,
            parent_number=parent_number,
        )

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def set_prerequisites(project_id: int, number: int, prereq_numbers: list[int]) -> dict:
        """Replace a task's prerequisite list. Cycles are rejected."""
        return store.set_prerequisites(project_id, number, prereq_numbers)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
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
    @_project_arg(store)
    def archive_task(project_id: int, number: int, confirm: bool = False) -> dict:
        """Archive a task (an epic archives its whole subtree)."""
        return store.archive_task(project_id, number, confirm)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def restore_task(
        project_id: int, number: int, to_state: str = "Backlog", confirm: bool = False
    ) -> dict:
        """Restore an archived task to a workflow state (default Backlog)."""
        return store.restore_task(project_id, number, to_state, confirm)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def delete_task(project_id: int, number: int, confirm: bool = False) -> dict:
        """Permanently delete a task (an epic's subtree is removed with it)."""
        return store.delete_task(project_id, number, confirm)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
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
    @_project_arg(store)
    def get_task_history(project_id: int, number: int):
        """Every state change of a task, with timestamps."""
        return store.get_history(project_id, number)

    @mcp.tool()
    @_wrap
    def list_task_types():
        """List task types (Story, Task, Bug, Epic, plus any custom ones)."""
        return store.list_task_types()

    @mcp.tool()
    @_wrap
    def add_task_type(name: str) -> dict:
        """Add a new regular (non-epic) task type."""
        return store.create_task_type(name)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def add_attachment(
        project_id: int,
        number: int,
        filename: str | None = None,
        data_base64: str | None = None,
        content_type: str | None = None,
        file_path: str | None = None,
    ) -> dict:
        """Attach markdown or an image to a task.

        Provide ``file_path`` (reads the file, infers content_type from the
        extension, filename defaults to the basename) or ``data_base64``
        (base64-encoded content, content_type defaults to text/markdown).
        data_base64 is the primary path; if both are given, file_path wins.
        """
        if file_path is not None:
            inferred_name, data, inferred_type = _read_attachment_file(file_path)
            filename = filename if filename is not None else inferred_name
            content_type = content_type or inferred_type
        else:
            if data_base64 is None:
                raise ValidationError("provide data_base64 or file_path")
            import base64

            try:
                data = base64.b64decode(data_base64)
            except (ValueError, TypeError) as e:
                raise ValidationError(f"invalid data_base64: {e}") from e
            content_type = content_type or "text/markdown"
        return store.add_attachment(
            project_id, number, filename, content_type, data
        )

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def list_attachments(project_id: int, number: int):
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

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def create_label(project_id: int, name: str) -> dict:
        """Create a project label, unique within the project."""
        return store.create_label(project_id, name)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def list_labels(project_id: int):
        """List a project's labels."""
        return store.list_labels(project_id)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def set_task_labels(project_id: int, number: int, label_ids: list[int]) -> dict:
        """Replace a task's label set. The labels must belong to the project."""
        return store.set_task_labels(project_id, number, label_ids)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def delete_label(project_id: int, label_id: int) -> dict:
        """Delete a project label, detaching it from all tasks it is applied to."""
        return store.delete_label(project_id, label_id)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def list_project_roles(project_id: int):
        """List a project's preset user-story roles (ordered)."""
        return store.list_project_roles(project_id)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def set_project_roles(project_id: int, names: list[str]):
        """Replace a project's preset user-story roles. Order is preserved and
        names are case-insensitively de-duplicated. Names must not be empty
        and must not contain '/'."""
        return store.set_project_roles(project_id, names)

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def remove_project_role(project_id: int, name: str) -> dict:
        """Remove one preset role from a project."""
        return store.remove_project_role(project_id, name)

    return mcp


def run_mcp(store: Store) -> None:
    server = build_server(store)
    server.run()
