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


def test_archive_and_delete_flow(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "e", "type": "Epic"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "s", "parent_number": 1})

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

    # delete with confirmation
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
