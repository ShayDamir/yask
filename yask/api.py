"""FastAPI application: REST API for the yask domain + static web UI."""

from __future__ import annotations

import inspect
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .store import (
    ConfirmationRequired,
    CycleError,
    Store,
    YaskError,
)

WEB_DIR = Path(__file__).parent / "web"


class Db:
    """One SQLite connection per worker thread (WAL allows many readers)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.path)
            self._local.conn = conn
        return conn


# -- request models ----------------------------------------------------------


class ProjectIn(BaseModel):
    name: str


class TaskIn(BaseModel):
    # New tasks always land in the Backlog (#1); the column is not choosable.
    title: str
    type: str = "Task"
    estimate: float | None = None
    parent_number: int | None = None
    description: str = ""
    before_number: int | None = None
    after_number: int | None = None


_UNSET = object()


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    type: str | None = None
    estimate: float | None = None
    # absent in the payload -> unchanged; null -> detach from epic
    parent_number: int | None = _UNSET


class MoveIn(BaseModel):
    to_state: str
    confirm: bool = False
    before_number: int | None = None
    after_number: int | None = None


class RestoreIn(BaseModel):
    to_state: str = "Backlog"
    confirm: bool = False


class ConfirmIn(BaseModel):
    confirm: bool = False


class PositionIn(BaseModel):
    before_number: int | None = None
    after_number: int | None = None


class PrereqsIn(BaseModel):
    prereq_numbers: list[int]


class TaskTypeIn(BaseModel):
    name: str


class LabelIn(BaseModel):
    name: str
    color: str = ""


class LabelUpdateIn(BaseModel):
    color: str = ""


class TaskLabelsIn(BaseModel):
    label_ids: list[int]


class ProjectRolesIn(BaseModel):
    names: list[str]


class TelegramUserIn(BaseModel):
    chat_id: int
    password: str


class TelegramUserPasswordIn(BaseModel):
    password: str


# -- app factory -------------------------------------------------------------


def create_app(db_path: str | Path) -> FastAPI:
    db_ = Db(db_path)
    app = FastAPI(title="yask", version="0.1.0")

    def store() -> Store:
        return Store(db_.conn(), source="web")

    def handle(fn):
        try:
            return fn()
        except ConfirmationRequired as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "confirmation required: this action changes state "
                    "for multiple tasks",
                    "requires_confirmation": True,
                    "affected": e.affected,
                },
            )
        except YaskError as e:
            raise HTTPException(status_code=e.status, detail=str(e))

    def _route(
        method: str,
        path: str,
        store_method: str,
        *,
        status: int = 200,
        name: str,
        body: type[BaseModel] | None = None,
        query: dict[str, tuple[type, Any]] | None = None,
        str_params: tuple[str, ...] = (),
    ) -> None:
        """Register a one-route endpoint that passes through to a Store method.

        The generated endpoint is a mechanical pass-through: the path params
        (the ``{name}`` occurrences in ``path``, annotated ``int`` unless
        listed in ``str_params``), an optional single Pydantic ``body`` model
        and an optional ordered ``query`` mapping are resolved by FastAPI
        exactly as in the old explicit per-route functions, then forwarded
        by name to ``store().<store_method>`` inside ``handle``. The explicit
        ``__signature__`` keeps FastAPI's dependency resolution and OpenAPI
        generation identical to the pre-refactor functions, and ``name``
        (the old function name) preserves the OpenAPI ``operationId``.
        """
        path_names = re.findall(r"\{(\w+)\}", path)

        def endpoint(**resolved):
            args = [resolved[n] for n in path_names]
            if body is not None:
                kwargs = resolved["body"].model_dump()
            else:
                kwargs = {n: resolved[n] for n in query or ()}
            return handle(lambda: getattr(store(), store_method)(*args, **kwargs))

        params = [
            inspect.Parameter(
                n,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str if n in str_params else int,
            )
            for n in path_names
        ]
        if body is not None:
            params.append(
                inspect.Parameter(
                    "body",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=body,
                )
            )
        params.extend(
            inspect.Parameter(
                n,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=typ,
                default=default,
            )
            for n, (typ, default) in (query or {}).items()
        )
        endpoint.__signature__ = inspect.Signature(params)
        endpoint.__name__ = name
        app.add_api_route(path, endpoint, methods=[method], status_code=status,
                          name=name)

    # -- projects

    _route("GET", "/api/projects", "list_projects", name="api_list_projects")
    _route("POST", "/api/projects", "create_project", status=201,
           name="api_create_project", body=ProjectIn)
    _route("GET", "/api/projects/{project_id}", "get_project",
           name="api_get_project")

    # -- tasks

    _route("GET", "/api/projects/{project_id}/tasks", "list_tasks",
           name="api_list_tasks",
           query={
               "state": (str | None, None),
               "include_archived": (bool, False),
               "label": (str | None, None),
           })
    _route("POST", "/api/projects/{project_id}/tasks", "create_task", status=201,
           name="api_create_task", body=TaskIn)
    _route("GET", "/api/projects/{project_id}/tasks/{number}", "get_task",
           name="api_get_task")

    @app.patch("/api/projects/{project_id}/tasks/{number}")
    def api_update_task(project_id: int, number: int, body: TaskUpdate):
        data = body.model_dump(exclude_unset=True)

        def run():
            kwargs = {k: v for k, v in data.items() if k != "parent_number"}
            if "parent_number" in data:
                kwargs["parent_number"] = data["parent_number"]
            return store().update_task(project_id, number, **kwargs)

        return handle(run)

    _route("POST", "/api/projects/{project_id}/tasks/{number}/move", "move_task",
           name="api_move_task", body=MoveIn)
    _route("POST", "/api/projects/{project_id}/tasks/{number}/archive",
           "archive_task", name="api_archive_task", body=ConfirmIn)
    _route("POST", "/api/projects/{project_id}/tasks/{number}/restore",
           "restore_task", name="api_restore_task", body=RestoreIn)
    _route("POST", "/api/projects/{project_id}/tasks/{number}/reorder",
           "reorder_task", name="api_reorder_task", body=PositionIn)

    @app.delete("/api/projects/{project_id}/tasks/{number}")
    def api_delete_task(project_id: int, number: int, confirm: bool = False):
        return handle(
            lambda: store().delete_task(project_id, number, confirm)
        )

    # -- prerequisites / history

    @app.get("/api/projects/{project_id}/tasks/{number}/prereqs")
    def api_get_prereqs(project_id: int, number: int):
        t = handle(lambda: store().get_task(project_id, number))
        return t["prerequisites"]

    _route("PUT", "/api/projects/{project_id}/tasks/{number}/prereqs",
           "set_prerequisites", name="api_set_prereqs", body=PrereqsIn)
    _route("GET", "/api/projects/{project_id}/tasks/{number}/history",
           "get_history", name="api_history")

    # -- attachments

    _route("GET", "/api/projects/{project_id}/tasks/{number}/attachments",
           "list_attachments", name="api_list_attachments")

    @app.post(
        "/api/projects/{project_id}/tasks/{number}/attachments", status_code=201
    )
    async def api_add_attachment(
        project_id: int, number: int, file: UploadFile = File(...)
    ):
        # Cap the read so an oversized payload cannot force a full in-memory
        # allocation before the size check (DoS, CWE-770); the store keeps the
        # authoritative backend guard. (task #72)
        data = await file.read(Store.MAX_ATTACHMENT_SIZE + 1)
        if len(data) > Store.MAX_ATTACHMENT_SIZE:
            raise HTTPException(
                status_code=400, detail="attachment exceeds 10 MB limit"
            )
        content_type = (file.content_type or "application/octet-stream").lower()
        return handle(
            lambda: store().add_attachment(
                project_id, number, file.filename or "attachment", content_type, data
            )
        )

    @app.get("/api/attachments/{attachment_id}")
    def api_get_attachment(attachment_id: int):
        def run():
            meta, data = store().get_attachment(attachment_id)
            return Response(
                content=data,
                media_type=meta["content_type"],
                headers={
                    "Content-Disposition": (
                        f'inline; filename="{meta["filename"]}"'
                    )
                },
            )

        return handle(run)

    _route("DELETE", "/api/attachments/{attachment_id}", "delete_attachment",
           name="api_delete_attachment")

    # -- labels

    _route("GET", "/api/projects/{project_id}/labels", "list_labels",
           name="api_list_labels")
    _route("POST", "/api/projects/{project_id}/labels", "create_label", status=201,
           name="api_create_label", body=LabelIn)
    _route("PUT", "/api/projects/{project_id}/labels/{label_id}", "update_label",
           name="api_update_label", body=LabelUpdateIn)
    _route("PUT", "/api/projects/{project_id}/tasks/{number}/labels",
           "set_task_labels", name="api_set_task_labels", body=TaskLabelsIn)
    _route("DELETE", "/api/projects/{project_id}/labels/{label_id}",
           "delete_label", name="api_delete_label")

    # -- project roles (user-story role presets)

    _route("GET", "/api/projects/{project_id}/roles", "list_project_roles",
           name="api_list_roles")
    _route("PUT", "/api/projects/{project_id}/roles", "set_project_roles",
           name="api_set_roles", body=ProjectRolesIn)
    _route("DELETE", "/api/projects/{project_id}/roles/{name}",
           "remove_project_role", name="api_delete_role", str_params=("name",))

    # -- telegram users (bot authentication allowlist)

    _route("GET", "/api/telegram-users", "list_telegram_users",
           name="api_list_telegram_users")
    _route("POST", "/api/telegram-users", "add_telegram_user", status=201,
           name="api_add_telegram_user", body=TelegramUserIn)
    _route("PUT", "/api/telegram-users/{chat_id}",
           "set_telegram_user_password", name="api_set_telegram_user_password",
           body=TelegramUserPasswordIn)
    _route("DELETE", "/api/telegram-users/{chat_id}", "remove_telegram_user",
           name="api_remove_telegram_user")

    # -- task types

    _route("GET", "/api/task-types", "list_task_types", name="api_list_types")
    _route("POST", "/api/task-types", "create_task_type", status=201,
           name="api_create_type", body=TaskTypeIn)
    _route("PATCH", "/api/task-types/{type_id}", "rename_task_type",
           name="api_rename_type", body=TaskTypeIn)
    _route("DELETE", "/api/task-types/{type_id}", "delete_task_type",
           name="api_delete_type")

    # -- web UI

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
