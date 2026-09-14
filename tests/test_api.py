"""REST API tests via FastAPI TestClient."""

import base64

import pytest
from fastapi.testclient import TestClient

from yask.api import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pid(client):
    r = client.post("/api/projects", json={"name": "API Demo"})
    assert r.status_code == 201
    return r.json()["id"]


def test_projects_crud(client):
    assert client.get("/api/projects").json() == []
    r = client.post("/api/projects", json={"name": "P1"})
    assert r.status_code == 201
    assert client.get("/api/projects").json()[0]["name"] == "P1"
    # duplicate name is a conflict
    assert client.post("/api/projects", json={"name": "p1"}).status_code == 409
    # empty name is a validation error
    assert client.post("/api/projects", json={"name": "  "}).status_code == 400


def test_task_lifecycle_over_api(client, pid):
    r = client.post(f"/api/projects/{pid}/tasks", json={"title": "first", "type": "Story", "estimate": 3})
    assert r.status_code == 201
    t = r.json()
    assert t["number"] == 1
    assert t["state"] == "Backlog"

    # move: single task applies without confirmation
    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo"})
    assert r.json() == {"applied": True, "affected": [
        {"number": 1, "title": "first", "type": "Story", "from": "Backlog", "to": "Todo"}
    ]}

    # invalid state
    assert client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Nope"}).status_code == 400
    # unknown task
    assert client.post(f"/api/projects/{pid}/tasks/99/move", json={"to_state": "Todo"}).status_code == 404

    # update
    r = client.patch(f"/api/projects/{pid}/tasks/1", json={"title": "first!", "estimate": 5, "description": "desc"})
    assert r.status_code == 200
    assert r.json()["title"] == "first!"
    assert r.json()["estimate"] == 5
    assert r.json()["description"] == "desc"

    # history
    hist = client.get(f"/api/projects/{pid}/tasks/1/history").json()
    assert [h["to_state"] for h in hist] == ["Backlog", "Todo"]


def test_create_task_ignores_state_field(client, pid):
    # new tasks can only be added to the Backlog (#1); a stale client
    # sending "state" in the body is ignored and the task lands in Backlog
    r = client.post(
        f"/api/projects/{pid}/tasks", json={"title": "sneaky", "state": "Todo"}
    )
    assert r.status_code == 201
    assert r.json()["state"] == "Backlog"


def test_move_confirmation_flow(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "main"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "prereq"})
    client.put(f"/api/projects/{pid}/tasks/1/prereqs", json={"prereq_numbers": [2]})

    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo"})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["requires_confirmation"] is True
    assert len(body["affected"]) == 2
    # nothing changed
    assert client.get(f"/api/projects/{pid}/tasks/2").json()["state"] == "Backlog"

    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo", "confirm": True})
    assert r.status_code == 200
    assert r.json()["applied"] is True
    assert client.get(f"/api/projects/{pid}/tasks/2").json()["state"] == "Todo"


def test_reorder_self_reference_noop_api(client, pid):
    """A self-referencing reorder is a clean 200 no-op, not a 500 (#51)."""
    client.post(f"/api/projects/{pid}/tasks", json={"title": "first"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "second"})
    r = client.post(f"/api/projects/{pid}/tasks/1/reorder", json={"after_number": 1})
    assert r.status_code == 200
    assert r.json()["number"] == 1
    r = client.post(f"/api/projects/{pid}/tasks/1/reorder", json={"before_number": 1})
    assert r.status_code == 200
    # column order unchanged
    order = [t["number"] for t in client.get(f"/api/projects/{pid}/tasks").json()]
    assert order == [1, 2]


def test_archive_and_delete_flow(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "e", "type": "Epic"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "s", "parent_number": 1})

    # deleting a non-archived task is refused — only archived tasks may be deleted
    r = client.delete(f"/api/projects/{pid}/tasks/1?confirm=true")
    assert r.status_code == 400
    assert client.get(f"/api/projects/{pid}/tasks/1").status_code == 200

    # archived tasks hidden by default
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={})
    assert r.status_code == 409  # subtree of 2 tasks
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={"confirm": True})
    assert r.status_code == 200
    tasks = client.get(f"/api/projects/{pid}/tasks").json()
    assert tasks == []
    tasks = client.get(f"/api/projects/{pid}/tasks?include_archived=true").json()
    assert len(tasks) == 2

    # restore single
    r = client.post(f"/api/projects/{pid}/tasks/1/restore", json={})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}/tasks/1").json()["state"] == "Backlog"

    # re-archive, then delete with confirmation (409 without, 200 with, subtree gone)
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={"confirm": True})
    assert r.status_code == 200
    # deleting a 2-task subtree needs confirmation: 409 without, 200 with
    r = client.delete(f"/api/projects/{pid}/tasks/1")
    assert r.status_code == 409
    r = client.delete(f"/api/projects/{pid}/tasks/1?confirm=true")
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}/tasks/1").status_code == 404
    assert client.get(f"/api/projects/{pid}/tasks/2").status_code == 404  # subtree gone


def test_prereq_cycle_rejected(client, pid):
    for i in (1, 2):
        client.post(f"/api/projects/{pid}/tasks", json={"title": f"t{i}"})
    client.put(f"/api/projects/{pid}/tasks/1/prereqs", json={"prereq_numbers": [2]})
    r = client.put(f"/api/projects/{pid}/tasks/2/prereqs", json={"prereq_numbers": [1]})
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"]


def test_attachment_upload_download(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert r.status_code == 201
    att = r.json()
    got = client.get(f"/api/attachments/{att['id']}")
    assert got.status_code == 200
    assert got.content == b"# hi"
    assert got.headers["content-type"].startswith("text/markdown")

    # wrong content type rejected
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("x.exe", b"x", "application/octet-stream")},
    )
    assert r.status_code == 400

    assert client.delete(f"/api/attachments/{att['id']}").status_code == 200
    assert client.get(f"/api/attachments/{att['id']}").status_code == 404


def _multipart_body(boundary, filename, data, content_type):
    """Build a raw multipart/form-data body with the exact filename bytes,
    so a CRLF inside the filename reaches the parser unescaped."""
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode()


def test_attachment_malicious_filename_no_header_injection(client, pid):
    """Bug #68: a crafted upload filename must not inject HTTP response
    headers.     The multipart filename carries a raw CRLF; the resulting
    ``Content-Disposition`` header must contain no control characters and no
    injected ``X-Attacked`` header.
    """
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    boundary = "----yaskboundary"
    body = _multipart_body(
        boundary,
        "x\r\nX-Attacked: 1",
        b"data",
        "text/markdown",
    )
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 201
    att = r.json()

    got = client.get(f"/api/attachments/{att['id']}")
    assert got.status_code == 200
    disp = got.headers.get("content-disposition", "")
    # No control characters (CRLF, tab) survive into the header value.
    assert "\r" not in disp and "\n" not in disp and "\t" not in disp
    assert "X-Attacked" not in disp
    # No injected response header leaked onto the response at all.
    header_names = {k.lower() for k in got.headers.keys()}
    assert "x-attacked" not in header_names


def test_attachment_upload_rejects_oversized_payload(client, pid):
    """Bug #72: an oversized attachment is rejected by the size check before
    the full payload is buffered in memory (DoS, CWE-770). The web handler
    caps its read at ``MAX + 1`` bytes and rejects anything larger; the
    store guard is the backend safety net.
    """
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    boundary = "----yaskoversizedboundary"
    cap = 10 * 1024 * 1024

    # control: exactly the cap is accepted.
    body_ok = _multipart_body(boundary, "ok.md", b"x" * cap, "text/markdown")
    r_ok = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_ok,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_ok.status_code == 201
    stored_after_ok = len(
        client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"]
    )

    # the boundary itself: cap + 1 byte is rejected.
    body_over = _multipart_body(boundary, "over.md", b"x" * (cap + 1), "text/markdown")
    r_over = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_over,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_over.status_code == 400
    # nothing new was stored by the rejection.
    assert (
        len(client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"])
        == stored_after_ok
    )

    # a clearly oversized payload is rejected too.
    body_big = _multipart_body(boundary, "big.md", b"x" * (cap * 2), "text/markdown")
    r_big = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_big,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_big.status_code == 400
    assert (
        len(client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"])
        == stored_after_ok
    )


def test_task_types_over_api(client):
    r = client.get("/api/task-types").json()
    assert {t["name"] for t in r} >= {"Story", "Task", "Bug", "Epic"}
    r = client.post("/api/task-types", json={"name": "Spike"})
    assert r.status_code == 201
    client.post(f"/api/projects", json={"name": "T2"})
    pid2 = client.get("/api/projects").json()[0]["id"]
    r = client.post(f"/api/projects/{pid2}/tasks", json={"title": "s", "type": "Spike"})
    assert r.status_code == 201
    assert r.json()["type"] == "Spike"


def test_epic_estimate_over_api(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "e", "type": "Epic"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "a", "type": "Story", "estimate": 3, "parent_number": 1})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "b", "type": "Bug", "estimate": 2, "parent_number": 1})
    e = client.get(f"/api/projects/{pid}/tasks/1").json()
    assert e["estimate"] is None
    assert e["estimate_total"] == 5
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["tasks"][0]["children"][0]["title"] == "a"


def test_openapi_route_table_stable(client):
    """Refactor guard (#82): the REST surface is pinned. Every
    (method, path, status code, operationId) entry of the OpenAPI route
    table must stay byte-identical when the internals of api.py change.
    FastAPI derives the operationId as ``{route name}_{path}_{method}``,
    so pinning it also guards the route ``name`` passed to ``_route``."""
    spec = client.get("/openapi.json").json()

    def status_of(op):
        # Declared responses are the success status plus 422 (request
        # validation) when there is a body; the success one is the rest.
        return next(k for k in op["responses"] if k != "422")

    table = {
        (method.upper(), path, status_of(op), op["operationId"])
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
    }
    assert table == {
        ("GET", "/api/projects", "200", "api_list_projects_api_projects_get"),
        ("POST", "/api/projects", "201", "api_create_project_api_projects_post"),
        ("GET", "/api/projects/{project_id}", "200", "api_get_project_api_projects__project_id__get"),
        ("GET", "/api/projects/{project_id}/tasks", "200", "api_list_tasks_api_projects__project_id__tasks_get"),
        ("POST", "/api/projects/{project_id}/tasks", "201", "api_create_task_api_projects__project_id__tasks_post"),
        ("GET", "/api/projects/{project_id}/tasks/{number}", "200", "api_get_task_api_projects__project_id__tasks__number__get"),
        ("PATCH", "/api/projects/{project_id}/tasks/{number}", "200", "api_update_task_api_projects__project_id__tasks__number__patch"),
        ("DELETE", "/api/projects/{project_id}/tasks/{number}", "200", "api_delete_task_api_projects__project_id__tasks__number__delete"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/move", "200", "api_move_task_api_projects__project_id__tasks__number__move_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/archive", "200", "api_archive_task_api_projects__project_id__tasks__number__archive_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/restore", "200", "api_restore_task_api_projects__project_id__tasks__number__restore_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/reorder", "200", "api_reorder_task_api_projects__project_id__tasks__number__reorder_post"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/prereqs", "200", "api_get_prereqs_api_projects__project_id__tasks__number__prereqs_get"),
        ("PUT", "/api/projects/{project_id}/tasks/{number}/prereqs", "200", "api_set_prereqs_api_projects__project_id__tasks__number__prereqs_put"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/history", "200", "api_history_api_projects__project_id__tasks__number__history_get"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/attachments", "200", "api_list_attachments_api_projects__project_id__tasks__number__attachments_get"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/attachments", "201", "api_add_attachment_api_projects__project_id__tasks__number__attachments_post"),
        ("PUT", "/api/projects/{project_id}/tasks/{number}/labels", "200", "api_set_task_labels_api_projects__project_id__tasks__number__labels_put"),
        ("GET", "/api/attachments/{attachment_id}", "200", "api_get_attachment_api_attachments__attachment_id__get"),
        ("DELETE", "/api/attachments/{attachment_id}", "200", "api_delete_attachment_api_attachments__attachment_id__delete"),
        ("GET", "/api/projects/{project_id}/labels", "200", "api_list_labels_api_projects__project_id__labels_get"),
        ("POST", "/api/projects/{project_id}/labels", "201", "api_create_label_api_projects__project_id__labels_post"),
        ("PUT", "/api/projects/{project_id}/labels/{label_id}", "200", "api_update_label_api_projects__project_id__labels__label_id__put"),
        ("DELETE", "/api/projects/{project_id}/labels/{label_id}", "200", "api_delete_label_api_projects__project_id__labels__label_id__delete"),
        ("GET", "/api/projects/{project_id}/roles", "200", "api_list_roles_api_projects__project_id__roles_get"),
        ("PUT", "/api/projects/{project_id}/roles", "200", "api_set_roles_api_projects__project_id__roles_put"),
        ("DELETE", "/api/projects/{project_id}/roles/{name}", "200", "api_delete_role_api_projects__project_id__roles__name__delete"),
        ("GET", "/api/telegram-users", "200", "api_list_telegram_users_api_telegram_users_get"),
        ("POST", "/api/telegram-users", "201", "api_add_telegram_user_api_telegram_users_post"),
        ("PUT", "/api/telegram-users/{chat_id}", "200", "api_set_telegram_user_password_api_telegram_users__chat_id__put"),
        ("DELETE", "/api/telegram-users/{chat_id}", "200", "api_remove_telegram_user_api_telegram_users__chat_id__delete"),
        ("GET", "/api/task-types", "200", "api_list_types_api_task_types_get"),
        ("POST", "/api/task-types", "201", "api_create_type_api_task_types_post"),
        ("PATCH", "/api/task-types/{type_id}", "200", "api_rename_type_api_task_types__type_id__patch"),
        ("DELETE", "/api/task-types/{type_id}", "200", "api_delete_type_api_task_types__type_id__delete"),
    }


def test_web_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "yask" in r.text.lower()
    r = client.get("/static/js/main.js")
    assert r.status_code == 200
    r = client.get("/static/style.css")
    assert r.status_code == 200


def test_project_isolation_over_api(client):
    a = client.post("/api/projects", json={"name": "A"}).json()["id"]
    b = client.post("/api/projects", json={"name": "B"}).json()["id"]
    client.post(f"/api/projects/{a}/tasks", json={"title": "in a"})
    assert client.get(f"/api/projects/{b}/tasks").json() == []
