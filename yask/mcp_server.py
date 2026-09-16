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
            kwargs["project_id"] = store.resolve_project(kwargs.pop("project"))["id"]
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


def _attachment_response(meta: dict, data: bytes):
    """One MCP content reply for a (meta, data) attachment pair.

    Markdown/text attachments come back as a JSON object with the decoded
    text in the "text" field. Images come back as metadata plus a native
    MCP image block so the agent can view them.
    """
    if meta["content_type"].startswith("text/"):
        return {**meta, "text": data.decode("utf-8", errors="replace")}
    fmt = meta["content_type"].split("/", 1)[1]
    return [TextContent(type="text", text=json.dumps(meta)), Image(data=data, format=fmt)]


# Data-driven registration for the mechanical pass-through tools: one line
# per Store method instead of a hand-written wrapper.
#
# Each entry is (tool_name, store_attr, docstring, project_scoped,
# return_annotation, omit):
#   store_attr        Store method the tool forwards to, as a string so this
#                     table needs no Store instance.
#   project_scoped    first parameter is a project given by name or id.
#   return_annotation ``dict`` marks tools whose old def carried a
#                     ``-> dict`` annotation (how the SDK then exposes it
#                     depends on project_scoped — see _make_tool); None
#                     exposes no return annotation at all.
#   omit              Store-only parameters hidden from the tool schema;
#                     they keep their Store defaults at call time.
# Docstrings are the exact description text the old tools exposed
# (multi-line ones as single strings with \n escapes), so the tool
# surface stays byte-identical.
_TOOLS = (
    ("list_projects", "list_projects", "List all projects with their task counts.", False, None, ()),
    (
        "create_project",
        "create_project",
        "Create a project. Projects have completely separate state.",
        False,
        dict,
        (),
    ),
    (
        "get_project",
        "get_project",
        "Get a project with its full task tree (epics nest their children).",
        True,
        dict,
        (),
    ),
    (
        "list_tasks",
        "list_tasks",
        "List a project's tasks. Optional state/label filters; archived hidden by default.",
        True,
        None,
        (),
    ),
    (
        "get_task",
        "get_task",
        "Get a single task by project and number. Returns the same data as a single item from list_tasks.",
        True,
        dict,
        (),
    ),
    (
        "create_task",
        "create_task",
        "Create a task (Story/Task/Bug or Epic). Epics take no estimate.\nNew tasks always start in the Backlog; move them forward separately.",
        True,
        dict,
        ("before_number", "after_number"),
    ),
    (
        "update_task",
        "update_task",
        "Update a task's fields. parent_number null detaches it from its epic.\n\nOmitting parent_number leaves the current parent unchanged; only an\nexplicit null detaches the task from its epic.\n",
        True,
        dict,
        (),
    ),
    (
        "set_prerequisites",
        "set_prerequisites",
        "Replace a task's prerequisite list. Cycles are rejected.",
        True,
        dict,
        (),
    ),
    (
        "move_task",
        "move_task",
        "Move a task to a workflow state (Backlog, Todo, Planning, In progress, Review, Done) or the holding state Blocked.\n\nMoving forward pulls prerequisites not yet past the target stage along\nwith it. Moving to Blocked is a single-task action that pulls no\nprerequisites along; the workflow docs require an unblock.md attachment\nexplaining what unblocks the task. If several tasks are affected and\nconfirm is false, the result contains the affected list and nothing is\nchanged.\n",
        True,
        dict,
        (),
    ),
    ("archive_task", "archive_task", "Archive a task (an epic archives its whole subtree).", True, dict, ()),
    (
        "restore_task",
        "restore_task",
        "Restore an archived task to a workflow state (default Backlog).",
        True,
        dict,
        (),
    ),
    (
        "delete_task",
        "delete_task",
        "Permanently delete an archived task (an epic's subtree is removed with it).",
        True,
        dict,
        (),
    ),
    (
        "reorder_task",
        "reorder_task",
        "Reorder a task within its column (default: move to the end).",
        True,
        dict,
        (),
    ),
    (
        "get_task_history",
        "get_history",
        "Every state change of a task, with timestamps.",
        True,
        None,
        (),
    ),
    (
        "list_task_types",
        "list_task_types",
        "List task types (Story, Task, Bug, Epic, plus any custom ones).",
        False,
        None,
        (),
    ),
    ("add_task_type", "create_task_type", "Add a new regular (non-epic) task type.", False, dict, ()),
    ("list_attachments", "list_attachments", "List a task's attachments (metadata only).", True, None, ()),
    (
        "create_label",
        "create_label",
        "Create a project label, unique within the project.",
        True,
        dict,
        (),
    ),
    (
        "update_label",
        "update_label",
        "Update a project label's color. Color-only; renaming is out of scope.",
        True,
        dict,
        (),
    ),
    ("list_labels", "list_labels", "List a project's labels.", True, None, ()),
    (
        "set_task_labels",
        "set_task_labels",
        "Replace a task's label set. The labels must belong to the project.",
        True,
        dict,
        (),
    ),
    (
        "delete_label",
        "delete_label",
        "Delete a project label, detaching it from all tasks it is applied to.",
        True,
        dict,
        (),
    ),
    (
        "list_project_roles",
        "list_project_roles",
        "List a project's preset user-story roles (ordered).",
        True,
        None,
        (),
    ),
    (
        "set_project_roles",
        "set_project_roles",
        "Replace a project's preset user-story roles. Order is preserved and\nnames are case-insensitively de-duplicated. Names must not be empty\nand must not contain '/'.",
        True,
        None,
        (),
    ),
    (
        "remove_project_role",
        "remove_project_role",
        "Remove one preset role from a project.",
        True,
        dict,
        (),
    ),
)


def _make_tool(store: Store, name: str, store_attr: str, doc: str, scoped: bool, ret, omit: tuple[str, ...]):
    """Build one pass-through tool for a (tool name, Store method) pair.

    Copies the Store method's signature so the exposed schema matches the
    hand-written wrapper it replaces: omitted parameters keep their Store
    defaults at call time, and a ``ret`` of None exposes no return
    annotation at all.

    The return annotation is exposed as the *string* ``"dict"`` for
    project-scoped tools and as the real ``dict`` class otherwise. This
    mirrors the old wrappers: FastMCP evaluates annotations read from plain
    function defs but takes ``__signature__`` verbatim, and ``_project_arg``
    put the def's (string) return annotation into that signature. The SDK
    treats the two differently — the string form gets a ``{"result": ...}``
    output schema, the real class gets none — so both must be preserved.
    """
    target = getattr(store, store_attr)
    sig = inspect.signature(target)
    params = [p for p in sig.parameters.values() if p.name not in omit]
    if ret is None:
        return_annotation = inspect.Signature.empty
    elif scoped:
        return_annotation = "dict"
    else:
        return_annotation = dict
    sig = sig.replace(parameters=params, return_annotation=return_annotation)

    def tool(*args, **kwargs):
        return target(*args, **kwargs)

    tool.__name__ = name  # survives the functools.wraps chain below
    tool.__doc__ = doc
    tool.__signature__ = sig  # honored by the SDK's inspect.signature
    if scoped:
        tool = _project_arg(store)(tool)  # renames project_id -> project (str|int)
    return _wrap(tool)


def build_server(store: Store) -> FastMCP:
    mcp = FastMCP("yask")
    store.source = "mcp"

    for name, store_attr, doc, scoped, ret, omit in _TOOLS:
        mcp.tool()(_make_tool(store, name, store_attr, doc, scoped, ret, omit))

    # Special tools: they do not mirror a single Store method one-to-one,
    # so they stay explicit.

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def get_next_task(project_id: int) -> dict | None:
        """Return the next actionable task in the project, or null.

        The Orchestrator uses this to pick the next task to dispatch. Priority:
        Review > In progress > Planning > Todo, by sort_order within each state.
        If the candidate has a prerequisite not yet Done, the first unmet
        prerequisite is returned instead so it is worked on first.
        """
        result = store.get_next_task(project_id)
        if result is None:
            return None
        return {"number": result["number"], "title": result["title"], "state": result["state"]}

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

            # Reject before decoding: base64 expands to ~3/4 its length in
            # bytes, so an upper bound on the decoded size is O(1) and avoids
            # allocating a large decoded blob before the cap is enforced
            # (guards against memory-exhaustion DoS; CWE-400/CWE-770).
            if len(data_base64) * 3 // 4 > Store.MAX_ATTACHMENT_SIZE:
                raise ValidationError("attachment exceeds 10 MB limit")

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
    def get_attachment(attachment_id: int):
        """Read an attachment's content.

        Markdown/text attachments come back as a JSON object with the decoded
        text in the "text" field. Images come back as metadata plus a native
        MCP image block so the agent can view them.
        """
        return _attachment_response(*store.get_attachment(attachment_id))

    @mcp.tool()
    @_wrap
    @_project_arg(store)
    def last_attachment(project_id: int, number: int):
        """Return the most recently attached document for a task, with its name.

        Markdown/text attachments come back as a JSON object with the decoded
        text in the "text" field. Images come back as metadata plus a native
        MCP image block. "Last" is the most recently created attachment; if it
        is not useful, request a specific one by id with get_attachment.
        """
        return _attachment_response(*store.last_attachment(project_id, number))

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
