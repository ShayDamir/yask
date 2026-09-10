"""Tests for labels: store, REST API and MCP surface."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from mcp.types import TextContent

from yask.api import create_app
from yask import db
from yask.mcp_server import build_server
from yask.store import Conflict, NotFound, ValidationError


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
    store.archive_task(pid, t["number"], confirm=True)
    store.delete_task(pid, t["number"], confirm=True)
    # orphaned link rows are gone but the label itself survives
    assert store.list_tasks(pid) == []
    assert store.list_labels(pid) == [{"id": l, "name": "temp", "color": ""}]


def test_labels_embedded_in_get_task_and_project(store, project):
    pid = project["id"]
    l = store.create_label(pid, "frontend")["id"]
    t = store.create_task(pid, "t")
    store.set_task_labels(pid, t["number"], [l])
    assert store.get_task(pid, t["number"])["labels"][0]["name"] == "frontend"
    proj = store.get_project(pid)
    assert proj["tasks"][0]["labels"][0]["name"] == "frontend"


def test_delete_label_detaches_from_all_tasks(store, project):
    pid = project["id"]
    l = store.create_label(pid, "temp")
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    store.set_task_labels(pid, a["number"], [l["id"]])
    store.set_task_labels(pid, b["number"], [l["id"]])
    c = store.create_task(pid, "c")

    res = store.delete_label(pid, l["id"])
    assert res["applied"] is True
    assert res["name"] == "temp"
    assert res["detached_tasks"] == 2

    # the label is gone from the project
    assert store.list_labels(pid) == []
    # detached from the tasks it was applied to
    assert store.get_task(pid, a["number"])["labels"] == []
    assert store.get_task(pid, b["number"])["labels"] == []
    # a task that never had the label is unaffected
    assert store.get_task(pid, c["number"])["labels"] == []


def test_delete_label_unknown_raises_not_found(store, project):
    with pytest.raises(NotFound):
        store.delete_label(project["id"], 9999)


def test_create_label_with_color(store, project):
    pid = project["id"]
    a = store.create_label(pid, "frontend", "#ff0000")
    assert a["color"] == "#FF0000"
    # default color is "no color"
    b = store.create_label(pid, "backend")
    assert b["color"] == ""
    # #RGB is expanded and uppercased
    c = store.create_label(pid, "backend2", "#0aF")
    assert c["color"] == "#00AAFF"
    # blank/whitespace color normalizes to ""
    d = store.create_label(pid, "backend3", "   ")
    assert d["color"] == ""
    e = store.create_label(pid, "backend4", None)
    assert e["color"] == ""


def test_list_labels_returns_color(store, project):
    pid = project["id"]
    store.create_label(pid, "frontend", "#123456")
    store.create_label(pid, "backend")
    labels = store.list_labels(pid)
    assert {l["name"]: l["color"] for l in labels} == {
        "frontend": "#123456",
        "backend": "",
    }


def test_update_label_changes_color(store, project):
    pid = project["id"]
    l = store.create_label(pid, "frontend")
    assert l["color"] == ""
    updated = store.update_label(pid, l["id"], "#00ff00")
    assert updated["color"] == "#00FF00"
    # name is echoed and unchanged
    assert updated["name"] == "frontend"
    # #RGB is expanded
    store.update_label(pid, l["id"], "#0f0")
    assert store.list_labels(pid)[0]["color"] == "#00FF00"
    # clearing color back to ""
    store.update_label(pid, l["id"], "")
    assert store.list_labels(pid)[0]["color"] == ""


def test_update_label_color_validates_membership(store, project):
    pid = project["id"]
    other = store.create_project("Other")["id"]
    foreign = store.create_label(other, "foreign")["id"]
    # a label not in this project -> not found
    with pytest.raises(NotFound):
        store.update_label(pid, foreign, "#123456")
    # unknown label -> not found
    with pytest.raises(NotFound):
        store.update_label(pid, 9999, "#123456")
    # invalid color -> validation error
    l = store.create_label(pid, "frontend")
    with pytest.raises(ValidationError):
        store.update_label(pid, l["id"], "not-a-color")


def test_label_color_embedded_in_get_task_and_project(store, project):
    pid = project["id"]
    l = store.create_label(pid, "frontend", "#abcdef")
    t = store.create_task(pid, "t")
    store.set_task_labels(pid, t["number"], [l["id"]])
    got = store.get_task(pid, t["number"])
    assert got["labels"][0]["color"] == "#ABCDEF"
    proj = store.get_project(pid)
    assert proj["tasks"][0]["labels"][0]["color"] == "#ABCDEF"


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
        {"id": lid, "name": "frontend", "color": ""}
    ]

    # case-insensitive duplicate -> conflict
    assert client.post(f"/api/projects/{pid}/labels", json={"name": "Frontend"}).status_code == 409
    # empty name -> validation
    assert client.post(f"/api/projects/{pid}/labels", json={"name": "  "}).status_code == 400

    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [lid]})
    assert r.status_code == 200
    assert r.json()["labels"] == [{"id": lid, "name": "frontend", "color": ""}]

    r = client.get(f"/api/projects/{pid}/tasks?label=frontend")
    assert r.status_code == 200
    assert [t["number"] for t in r.json()] == [1]

    # foreign label id rejected
    r = client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [999]})
    assert r.status_code == 400


def test_label_color_over_api(client, pid):
    # create with color is echoed back (normalized)
    r = client.post(f"/api/projects/{pid}/labels", json={"name": "urgent", "color": "#abc"})
    assert r.status_code == 201
    lid = r.json()["id"]
    assert r.json()["color"] == "#AABBCC"

    # listed color present and normalized
    listed = client.get(f"/api/projects/{pid}/labels").json()
    assert listed == [{"id": lid, "name": "urgent", "color": "#AABBCC"}]

    # embed a colored label on a task
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [lid]})
    assert r.json()["labels"][0]["color"] == "#AABBCC"

    # PUT update changes the color
    r = client.put(f"/api/projects/{pid}/labels/{lid}", json={"color": "#010203"})
    assert r.status_code == 200
    assert r.json()["color"] == "#010203"
    assert client.get(f"/api/projects/{pid}/labels").json()[0]["color"] == "#010203"

    # PUT with no color body clears it
    r = client.put(f"/api/projects/{pid}/labels/{lid}", json={})
    assert r.json()["color"] == ""

    # invalid color -> 400
    r = client.put(f"/api/projects/{pid}/labels/{lid}", json={"color": "nope"})
    assert r.status_code == 400

    # create with invalid color -> 400
    r = client.post(f"/api/projects/{pid}/labels", json={"name": "bad", "color": "nope"})
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


def test_delete_label_over_api(client, pid):
    r = client.post(f"/api/projects/{pid}/labels", json={"name": "temp"})
    lid = r.json()["id"]
    client.post(f"/api/projects/{pid}/tasks", json={"title": "a"})
    client.put(f"/api/projects/{pid}/tasks/1/labels", json={"label_ids": [lid]})

    r = client.delete(f"/api/projects/{pid}/labels/{lid}")
    assert r.status_code == 200
    assert r.json()["detached_tasks"] == 1
    assert client.get(f"/api/projects/{pid}/labels").json() == []
    # the link is gone from the task
    assert client.get(f"/api/projects/{pid}/tasks/1").json()["labels"] == []


def test_delete_label_over_api_unknown_project(client):
    assert client.delete("/api/projects/999/labels/1").status_code == 404


def test_delete_label_over_api_unknown_label(client, pid):
    assert client.delete(f"/api/projects/{pid}/labels/9999").status_code == 404


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
    for tool in ("create_label", "list_labels", "set_task_labels", "delete_label", "update_label"):
        assert tool in names_list


def test_label_tools_expose_project_by_name(store, project):
    server = build_server(store)

    async def schemas():
        return {t.name: t.inputSchema for t in await server.list_tools()}

    schemas_by_name = asyncio.run(schemas())
    for name in ("create_label", "list_labels", "set_task_labels", "delete_label"):
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
    assert labels == [{"id": lid, "name": "frontend", "color": ""}]

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


def test_delete_label_via_mcp(store, project):
    l = _call(store, "create_label", project="demo", name="temp")
    lid = json.loads([c for c in l if isinstance(c, TextContent)][0].text)["id"]
    t = store.create_task(project["id"], "t")
    _call(store, "set_task_labels", project=project["name"], number=t["number"], label_ids=[lid])

    res = _call(store, "delete_label", project=project["name"], label_id=lid)
    data = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert data["applied"] is True
    assert data["detached_tasks"] == 1

    labels = [
        json.loads(c.text)
        for c in _call(store, "list_labels", project=project["name"])
        if isinstance(c, TextContent)
    ]
    assert labels == []

    tasks = [
        json.loads(c.text)
        for c in _call(store, "list_tasks", project=project["name"])
        if isinstance(c, TextContent)
    ]
    assert tasks[0]["labels"] == []


def test_label_color_via_mcp(store, project):
    # create with color, echoed normalized
    l = _call(store, "create_label", project="demo", name="frontend", color="#abc")
    meta = json.loads([c for c in l if isinstance(c, TextContent)][0].text)
    lid = meta["id"]
    assert meta["color"] == "#AABBCC"

    # listed color present
    labels = [
        json.loads(c.text)
        for c in _call(store, "list_labels", project=project["name"])
        if isinstance(c, TextContent)
    ]
    assert labels == [{"id": lid, "name": "frontend", "color": "#AABBCC"}]

    # update_label changes it
    r = _call(store, "update_label", project=project["name"], label_id=lid, color="#010203")
    data = json.loads([c for c in r if isinstance(c, TextContent)][0].text)
    assert data["color"] == "#010203"

    # invalid color surfaces a readable error payload
    r = _call(store, "update_label", project=project["name"], label_id=lid, color="nope")
    data = json.loads([c for c in r if isinstance(c, TextContent)][0].text)
    assert data["ok"] is False
    assert "invalid color" in data["error"]


def test_migration_adds_color_column_to_existing_database(tmp_path):
    """A pre-existing DB (labels table without color) gains the column."""
    path = tmp_path / "legacy-labels.db"
    # Old schema: labels without a color column.
    conn = db.connect(path)
    pid = conn.execute(
        "INSERT INTO projects(name, next_task_number, created_at)"
        " VALUES ('Legacy', 1, '2020-01-01T00:00:00Z')"
    ).lastrowid
    conn.execute(
        "INSERT INTO labels(project_id, name) VALUES (?, ?)", (pid, "old")
    )
    conn.commit()
    conn.close()

    # Re-open: _ensure_missing_columns should add the color column.
    conn = db.connect(path)
    label_cols = {r["name"] for r in conn.execute("PRAGMA table_info(labels)")}
    assert "color" in label_cols
    legacy = conn.execute("SELECT color FROM labels WHERE id = 1").fetchone()
    assert legacy["color"] == ""  # legacy rows default to "no color"
    conn.close()

    # And a Store/REST on that DB sees the column.
    app = create_app(path)
    with TestClient(app) as c:
        listed = c.get(f"/api/projects/{pid}/labels").json()
        assert listed == [{"id": 1, "name": "old", "color": ""}]
