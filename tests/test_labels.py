"""Tests for labels: store, REST API and MCP surface."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from mcp.types import TextContent

from yask.api import create_app
from yask.mcp_server import build_server
from yask.store import Conflict, ValidationError


# -- store ------------------------------------------------------------------

def test_create_and_list_labels(store, project):
    pid = project["id"]
    a = store.create_label(pid, "frontend")
    b = store.create_label(pid, "backend")
    assert {l["name"] for l in store.list_labels(pid)} == {"frontend", "backend"}
    assert a["name"] == "frontend"
    assert b["name"] == "backend"


def test_label_names_unique_per_project_case_insensitive(store, project):
    pid = project["id"]
    store.create_label(pid, "frontend")
    with pytest.raises(Conflict):
        store.create_label(pid, "Frontend")
    # another project may reuse the name
    other = store.create_project("Other")["id"]
    store.create_label(other, "frontend")
    assert len(store.list_labels(other)) == 1


def test_label_name_must_not_be_empty(store, project):
    with pytest.raises(ValidationError):
        store.create_label(project["id"], "   ")


def test_labels_are_project_scoped(store, project):
    pid = project["id"]
    other = store.create_project("Other")["id"]
    store.create_label(pid, "shared")
    assert store.list_labels(other) == []


def test_set_task_labels_replaces_and_serializes(store, project):
    pid = project["id"]
    l1 = store.create_label(pid, "frontend")
    l2 = store.create_label(pid, "backend")
    t = store.create_task(pid, "task one")
    t = store.set_task_labels(pid, t["number"], [l1["id"]])
    assert [l["name"] for l in t["labels"]] == ["frontend"]
    # replacing drops the old set
    t = store.set_task_labels(pid, t["number"], [l2["id"]])
    assert [l["name"] for l in t["labels"]] == ["backend"]
    # duplicate ids are deduped
    t = store.set_task_labels(pid, t["number"], [l1["id"], l1["id"]])
    assert len(t["labels"]) == 1


def test_set_task_labels_requires_project_labels(store, project):
    pid = project["id"]
    other = store.create_project("Other")["id"]
    foreign = store.create_label(other, "foreign")["id"]
    t = store.create_task(pid, "t")
    with pytest.raises(ValidationError):
        store.set_task_labels(pid, t["number"], [foreign])


def test_clearing_labels(store, project):
    pid = project["id"]
    l = store.create_label(pid, "temp")["id"]
    t = store.create_task(pid, "t")
    store.set_task_labels(pid, t["number"], [l])
    t = store.set_task_labels(pid, t["number"], [])
    assert t["labels"] == []


def test_list_tasks_by_label(store, project):
    pid = project["id"]
    l = store.create_label(pid, "urgent")["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    store.create_task(pid, "c")
    store.set_task_labels(pid, a["number"], [l])
    store.set_task_labels(pid, b["number"], [l])
    got = store.list_tasks(pid, label="urgent")
    assert {t["title"] for t in got} == {"a", "b"}


def test_list_tasks_by_unknown_label(store, project):
    with pytest.raises(ValidationError):
        store.list_tasks(project["id"], label="nope")


def test_list_tasks_by_label_combined_with_state_and_archived(store, project):
    pid = project["id"]
    l = store.create_label(pid, "keep")["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    store.set_task_labels(pid, a["number"], [l])
    store.set_task_labels(pid, b["number"], [l])
    store.move_task(pid, a["number"], "Todo")
    store.archive_task(pid, b["number"])

    # label filter alone hides archived by default
    got = store.list_tasks(pid, label="keep")
    assert {t["title"] for t in got} == {"a"}
    # archived tasks carrying the label reappear when requested
    got = store.list_tasks(pid, label="keep", include_archived=True)
    assert {t["title"] for t in got} == {"a", "b"}
    # state + label combine
    got = store.list_tasks(pid, state="Todo", label="keep")
    assert {t["title"] for t in got} == {"a"}


def test_labels_follow_task_delete(store, project):
    pid = project["id"]
    l = store.create_label(pid, "temp")["id"]
    t = store.create_task(pid, "t")
    store.set_task_labels(pid, t["number"], [l])
    store.delete_task(pid, t["number"], confirm=True)
    # orphaned link rows are gone but the label itself survives
    assert store.list_tasks(pid) == []
    assert store.list_labels(pid) == [{"id": l, "name": "temp"}]


def test_labels_embedded_in_get_task_and_project(store, project):
    pid = project["id"]
    l = store.create_label(pid, "frontend")["id"]
    t = store.create_task(pid, "t")
    store.set_task_labels(pid, t["number"], [l])
    assert store.get_task(pid, t["number"])["labels"][0]["name"] == "frontend"
    proj = store.get_project(pid)
    assert proj["tasks"][0]["labels"][0]["name"] == "frontend"


# -- REST API ---------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pid(client):
    r = client.post("/api/projects", json={"name": "Label Demo"})
    return r.json()["id"]


def test_labels_over_api(client, pid):
    r = client.post(f"/api/projects/{pid}/labels", json={"name": "frontend"})
    assert r.status_code == 201
    lid = r.json()["id"]

    assert client.get(f"/api/projects/{pid}/labels").json() == [
        {"id": lid, "name": "frontend"}
    ]

    # case-insensitive duplicate -> conflict
    assert client.post(f"/api/projects/{pid}/labels", json={"name": "Frontend"}).status_code == 409
    # empty name -> validation
    assert client.post(f"/api/projects/{pid}/labels", json={"name": "  "}).status_code == 400

    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [lid]})
    assert r.status_code == 200
    assert r.json()["labels"] == [{"id": lid, "name": "frontend"}]

    r = client.get(f"/api/projects/{pid}/tasks?label=frontend")
    assert r.status_code == 200
    assert [t["number"] for t in r.json()] == [1]

    # foreign label id rejected
    r = client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [999]})
    assert r.status_code == 400


def test_project_isolation_of_labels_over_api(client):
    a = client.post("/api/projects", json={"name": "A"}).json()["id"]
    b = client.post("/api/projects", json={"name": "B"}).json()["id"]
    client.post(f"/api/projects/{a}/labels", json={"name": "shared"})
    assert client.get(f"/api/projects/{b}/labels").json() == []


def test_label_endpoints_404_on_unknown_project(client):
    assert client.get("/api/projects/999/labels").status_code == 404
    assert client.post("/api/projects/999/labels", json={"name": "x"}).status_code == 404
    assert (
        client.put("/api/projects/999/tasks/1/labels", json={"label_ids": []}).status_code
        == 404
    )


# -- MCP --------------------------------------------------------------------

def _call(store, tool, **args):
    server = build_server(store)
    blocks = asyncio.run(server.call_tool(tool, args))
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return blocks


def test_label_tools_are_registered(store, project):
    server = build_server(store)

    async def names():
        return sorted(t.name for t in await server.list_tools())

    names_list = asyncio.run(names())
    for tool in ("create_label", "list_labels", "set_task_labels"):
        assert tool in names_list


def test_label_tools_expose_project_by_name(store, project):
    server = build_server(store)

    async def schemas():
        return {t.name: t.inputSchema for t in await server.list_tools()}

    schemas_by_name = asyncio.run(schemas())
    for name in ("create_label", "list_labels", "set_task_labels"):
        props = schemas_by_name[name]["properties"]
        assert "project" in props
        assert "project_id" not in props


def test_create_list_set_labels_via_mcp(store, project):
    t = store.create_task(project["id"], "t")
    res = _call(store, "create_label", project="demo", name="frontend")
    meta = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    lid = meta["id"]

    assert meta["name"] == "frontend"

    res = _call(store, "list_labels", project=project["name"])
    labels = [json.loads(c.text) for c in res if isinstance(c, TextContent)]
    assert labels == [{"id": lid, "name": "frontend"}]

    res = _call(store, "set_task_labels", project=project["name"], number=t["number"], label_ids=[lid])
    task = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert [l["name"] for l in task["labels"]] == ["frontend"]

    res = _call(store, "list_tasks", project=project["name"], label="frontend")
    tasks = [json.loads(c.text) for c in res if isinstance(c, TextContent)]
    assert [t["title"] for t in tasks] == ["t"]


def test_list_tasks_by_label_unknown_via_mcp(store, project):
    res = _call(store, "list_tasks", project=project["name"], label="nope")
    data = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]