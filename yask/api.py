"""FastAPI application: REST API for the yask domain + static web UI."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import re
import threading
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

# Strict CSP for the web UI (tasks #86/#98): only the module script and the
# stylesheet from /static, images from self/data:/blob: (data: favicon,
# blob: attachment viewer), same-origin fetch only, no framing, no
# <base>, no native form submits (the UI intercepts every form).
# ``style-src`` is fully strict: the UI uses stylesheet classes for static
# styling, and the dynamic label-chip color is applied via CSSOM custom
# properties (el.style.setProperty), which CSP does not block — so no
# inline style="..." attributes remain in the markup.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)

# Inert CSP for attachment byte responses (task #84): sandbox blocks
# script execution outright and the document is treated as a unique
# origin, so an SVG served as a document can never run its embedded
# <script> even if Content-Disposition were lost or a consumer
# rendered the response directly.
ATTACHMENT_CSP = "sandbox; default-src 'none'"

# Bounded attachment downloads (task #96, decision B): GET
# /api/attachments/{id} streams the stored bytes through a
# StreamingResponse instead of building one in-memory Response, and a
# per-app semaphore caps how many downloads read their blob at once.
# Worst-case in-flight memory is then K blobs (each <= 10 MB) instead
# of one per concurrent request; waiting requests park as coroutines
# holding no bytes.
ATTACHMENT_DOWNLOAD_CONCURRENCY = 4
# Stream slice size: a 10 MB attachment streams as 10 chunks, keeping
# time-to-first-byte low without a per-request copy beyond one blob +
# one transient chunk.
ATTACHMENT_STREAM_CHUNK = 1024 * 1024


# -- Host/Origin checks (task #85: CSRF / DNS rebinding) --------------------
#
# The loopback bind is a safety measure, not a security boundary: a page on
# attacker.com can still POST a CORS-simple multipart form to
# http://127.0.0.1:4304 (CSRF, CWE-352), and DNS rebinding can make
# attacker.com present itself to the browser as 127.0.0.1 (CWE-350). The
# ``origin_host_check`` middleware closes both vectors:
#   * the ``Host`` header must name a loopback interface — a rebound
#     page's requests carry ``Host: attacker.com:4304`` and are rejected
#     before routing, so a rebound origin can neither read nor write;
#   * an ``Origin`` header, when present, must be same-origin with the
#     ``Host`` header (same host, same port) — the browser's
#     ``Origin: https://evil.com`` on the cross-site multipart upload
#     fails;
#   * ``Sec-Fetch-Site: cross-site`` is rejected — defense in depth for
#     intermediaries that would strip ``Origin``.
# Non-browser clients (curl, scripts) send no ``Origin`` and pass the
# Origin check; the Host check still applies to them, so a local
# ``curl http://127.0.0.1:4304/...`` keeps working.


def _is_loopback_name(name: str) -> bool:
    """True if *name* is a loopback host name (localhost, 127.0.0.0/8, ::1).

    Duplicates ``cli._is_loopback_host`` on purpose: the CLI helper decides
    whether a *bind* is safe, this one decides whether a *request* is local.
    Keeping them local avoids a cross-module dependency.
    """
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _split_host_header(value: str) -> tuple[str, str | None]:
    """Split a ``Host`` header into ``(host, port)``; port is None when absent.

    IPv6 arrives bracketed (``[::1]:4304``), so brackets and port are
    stripped separately. Malformed values are kept opaque — they then fail
    the loopback check.
    """
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1:]
        return (host, rest[1:]) if rest.startswith(":") else (value, None)
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, port
    return value, None


def _origin_host_port(value: str) -> tuple[str, str] | None:
    """Parse an ``Origin`` header into ``(host, port)`` with default ports
    applied (http -> 80, https -> 443).

    Returns None for values that are not a valid http(s) origin
    (``null``, ``javascript:…``, unparseable garbage) — the caller rejects
    those.
    """
    value = value.strip()
    m = re.match(r"^(?:https?)://([^:/?]+)(?::(\d+))?$", value)
    if m is None:
        return None
    host, port = m.groups()
    if port is None:
        port = "443" if value.lower().startswith("https") else "80"
    return host.lower(), port


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
    to_state: str = db.DEFAULT_STATE
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


def create_app(db_path: str | Path, allow_remote: bool = False) -> FastAPI:
    db_ = Db(db_path)
    app = FastAPI(title="yask", version="0.1.0")

    # Per-app download slots (task #96): caps concurrent in-flight
    # attachment downloads so N concurrent GETs never hold N full blobs.
    # Per-app on purpose — each test fixture and the server get their own
    # semaphore. NOTE: an asyncio.Semaphore binds to the first event loop
    # that uses it, so one app instance must be driven from a single loop
    # (true in production — one uvicorn loop per app — and in tests, where
    # each app is used by exactly one TestClient). Cross-loop misuse fails
    # loudly rather than silently.
    download_slots = asyncio.Semaphore(ATTACHMENT_DOWNLOAD_CONCURRENCY)

    # NOTE (task #85): middleware order matters. Starlette stacks these
    # decorators in reverse code order, so ``origin_host_check`` must be
    # declared BEFORE ``security_headers`` to stay inner to it — the
    # headers middleware then decorates even the 403 this check returns
    # (task #86's "every response carries the four headers" invariant).
    @app.middleware("http")
    async def origin_host_check(request, call_next):
        # CSRF / DNS-rebinding guard (task #85), skipped wholesale when the
        # server was started with allow_remote (the CLI's --allow-remote):
        # the explicit override already warns that the API runs without
        # these protections, and remote clients must keep working.
        if allow_remote:
            return await call_next(request)

        # 1. Host must name a loopback interface (DNS-rebinding kill: a
        #    rebound origin's requests carry the attacker's hostname).
        host_header = request.headers.get("host", "")
        host, host_port = _split_host_header(host_header)
        if not _is_loopback_name(host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Host header must name a loopback interface"
                },
            )

        # 2. An Origin header, when present, must be same-origin with the
        #    Host header (same host, same port). This is what stops the
        #    cross-site multipart attachment upload: the browser always
        #    sends Origin on POST/PUT/PATCH/DELETE, and curl & co send
        #    none (the non-browser exemption).
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = _origin_host_port(origin)
            # A Host header without an explicit port means the browser is
            # on the scheme's default port (http -> 80, https -> 443).
            expected_port = host_port
            if expected_port is None:
                expected_port = (
                    "443" if origin.strip().lower().startswith("https") else "80"
                )
            if (
                parsed is None
                or parsed[0] != host.lower()
                or parsed[1] != expected_port
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "Origin must be same-origin with the Host header"
                        )
                    },
                )

        # 3. Sec-Fetch-Site: cross-site is rejected outright (defense in
        #    depth for intermediaries that strip Origin).
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse(
                status_code=403,
                content={"detail": "cross-site requests are rejected"},
            )

        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Defense-in-depth on every response (task #86): CSP (module
        # scripts only, no inline scripts, no framing, no exfiltration
        # channels), X-Frame-Options (clickjacking), nosniff (MIME
        # sniffing), no-referrer (Referer leakage). App-level, so it
        # covers API routes, the /static mount, and error responses.
        # The UI CSP is the default; an endpoint that supplies its own
        # policy keeps it — attachment bytes carry the inert policy
        # (task #84) — so the invariant is: every response has the four
        # headers, and the CSP is endpoint-specific when set.
        response = await call_next(request)
        if "content-security-policy" not in response.headers:
            response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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
            return store().update_task(project_id, number, **data)

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
    async def api_get_attachment(attachment_id: int):
        # (task #96, decision B) The blob is read once off the event loop
        # and streamed back in ATTACHMENT_STREAM_CHUNK slices, so each
        # in-flight download holds one full blob (<= 10 MB) plus one
        # transient chunk, and the semaphore above bounds how many blobs
        # exist at once. The full-bytes store methods stay as-is: the MCP
        # server and the Telegram bot need the complete content and are
        # single-reader paths.
        #
        # The read happens in the handler body, not the generator, so a
        # missing attachment still raises HTTPException(404) before
        # response headers are sent; anyio.to_thread.run_sync preserves
        # the threading semantics the sync endpoint had (FastAPI ran it in
        # its thread pool; an async handler must not block the loop).
        await download_slots.acquire()
        try:
            meta, data = await anyio.to_thread.run_sync(
                lambda: handle(lambda: store().get_attachment(attachment_id))
            )
        except BaseException:
            # The read failed (404, …): the slot is not handed to the
            # generator below, so release it before re-raising.
            download_slots.release()
            raise
        # SVG is the only executable type in the attachment allowlist:
        # force a download so direct navigation cannot render (and
        # script) it, and give every attachment the inert CSP so the
        # bytes are never an executable document this app serves
        # (task #84 owns the download disposition and the CSP; the
        # global security headers are task #86's). The web UI viewer
        # fetches the blob and renders it, so it is unaffected by the
        # disposition.
        disposition = (
            "attachment" if meta["content_type"] == "image/svg+xml" else "inline"
        )

        def stream():
            try:
                for offset in range(0, len(data), ATTACHMENT_STREAM_CHUNK):
                    yield data[offset:offset + ATTACHMENT_STREAM_CHUNK]
            finally:
                # The generator owns the slot from here on: release it on
                # normal completion and when Starlette closes the generator
                # on client disconnect.
                download_slots.release()

        return StreamingResponse(
            stream(),
            media_type=meta["content_type"],
            headers={
                "Content-Disposition": f'{disposition}; filename="{meta["filename"]}"',
                "Content-Security-Policy": ATTACHMENT_CSP,
            },
        )

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
