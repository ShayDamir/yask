"""FastAPI application: REST API for the yask domain + static web UI."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .store import (
    ConfirmationRequired,
    CycleError,
    Store,
    ValidationError,
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
    title: str
    type: str = "Task"
    estimate: float | None = None
    parent_number: int | None = None
    description: str = ""
    state: str = "Backlog"
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


class TaskLabelsIn(BaseModel):
    label_ids: list[int]


# -- app factory -------------------------------------------------------------


def create_app(db_path: str | Path) -> FastAPI:
    db_ = Db(db_path)
    app = FastAPI(title="yask", version="0.1.0")

    def store() -> Store:
        return Store(db_.conn())

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

    # -- projects

    @app.get("/api/projects")
    def api_list_projects():
        return handle(lambda: store().list_projects())

    @app.post("/api/projects", status_code=201)
    def api_create_project(body: ProjectIn):
        return handle(lambda: store().create_project(body.name))

    @app.get("/api/projects/{project_id}")
    def api_get_project(project_id: int):
        return handle(lambda: store().get_project(project_id))

    # -- tasks

    @app.get("/api/projects/{project_id}/tasks")
    def api_list_tasks(
        project_id: int,
        state: str | None = None,
        include_archived: bool = False,
        label: str | None = None,
    ):
        return handle(
            lambda: store().list_tasks(project_id, state, include_archived, label)
        )

    @app.post("/api/projects/{project_id}/tasks", status_code=201)
    def api_create_task(project_id: int, body: TaskIn):
        return handle(
            lambda: store().create_task(
                project_id,
                body.title,
                body.type,
                body.estimate,
                body.parent_number,
                body.description,
                body.state,
                body.before_number,
                body.after_number,
            )
        )

    @app.get("/api/projects/{project_id}/tasks/{number}")
    def api_get_task(project_id: int, number: int):
        return handle(lambda: store().get_task(project_id, number))

    @app.patch("/api/projects/{project_id}/tasks/{number}")
    def api_update_task(project_id: int, number: int, body: TaskUpdate):
        data = body.model_dump(exclude_unset=True)

        def run():
            kwargs = {k: v for k, v in data.items() if k != "parent_number"}
            if "parent_number" in data:
                kwargs["parent_number"] = data["parent_number"]
            return store().update_task(project_id, number, **kwargs)

        return handle(run)

    @app.post("/api/projects/{project_id}/tasks/{number}/move")
    def api_move_task(project_id: int, number: int, body: MoveIn):
        return handle(
            lambda: store().move_task(
                project_id,
                number,
                body.to_state,
                body.confirm,
                body.before_number,
                body.after_number,
            )
        )

    @app.post("/api/projects/{project_id}/tasks/{number}/archive")
    def api_archive_task(project_id: int, number: int, body: ConfirmIn):
        return handle(lambda: store().archive_task(project_id, number, body.confirm))

    @app.post("/api/projects/{project_id}/tasks/{number}/restore")
    def api_restore_task(project_id: int, number: int, body: RestoreIn):
        return handle(
            lambda: store().restore_task(
                project_id, number, body.to_state, body.confirm
            )
        )

    @app.post("/api/projects/{project_id}/tasks/{number}/reorder")
    def api_reorder_task(project_id: int, number: int, body: PositionIn):
        return handle(
            lambda: store().reorder_task(
                project_id, number, body.before_number, body.after_number
            )
        )

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

    @app.put("/api/projects/{project_id}/tasks/{number}/prereqs")
    def api_set_prereqs(project_id: int, number: int, body: PrereqsIn):
        return handle(
            lambda: store().set_prerequisites(
                project_id, number, body.prereq_numbers
            )
        )

    @app.get("/api/projects/{project_id}/tasks/{number}/history")
    def api_history(project_id: int, number: int):
        return handle(lambda: store().get_history(project_id, number))

    # -- attachments

    @app.get("/api/projects/{project_id}/tasks/{number}/attachments")
    def api_list_attachments(project_id: int, number: int):
        return handle(lambda: store().list_attachments(project_id, number))

    @app.post(
        "/api/projects/{project_id}/tasks/{number}/attachments", status_code=201
    )
    async def api_add_attachment(
        project_id: int, number: int, file: UploadFile = File(...)
    ):
        data = await file.read()
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

    @app.delete("/api/attachments/{attachment_id}")
    def api_delete_attachment(attachment_id: int):
        return handle(lambda: store().delete_attachment(attachment_id))

    # -- labels

    @app.get("/api/projects/{project_id}/labels")
    def api_list_labels(project_id: int):
        return handle(lambda: store().list_labels(project_id))

    @app.post("/api/projects/{project_id}/labels", status_code=201)
    def api_create_label(project_id: int, body: LabelIn):
        return handle(lambda: store().create_label(project_id, body.name))

    @app.put("/api/projects/{project_id}/tasks/{number}/labels")
    def api_set_task_labels(project_id: int, number: int, body: TaskLabelsIn):
        return handle(
            lambda: store().set_task_labels(project_id, number, body.label_ids)
        )

    # -- task types

    @app.get("/api/task-types")
    def api_list_types():
        return handle(lambda: store().list_task_types())

    @app.post("/api/task-types", status_code=201)
    def api_create_type(body: TaskTypeIn):
        return handle(lambda: store().create_task_type(body.name))

    @app.patch("/api/task-types/{type_id}")
    def api_rename_type(type_id: int, body: TaskTypeIn):
        return handle(lambda: store().rename_task_type(type_id, body.name))

    @app.delete("/api/task-types/{type_id}")
    def api_delete_type(type_id: int):
        return handle(lambda: store().delete_task_type(type_id))

    # -- web UI

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
