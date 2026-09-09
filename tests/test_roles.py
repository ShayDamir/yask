"""Tests for per-project user-story role presets: store, REST API and MCP."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from mcp.types import TextContent

from yask.api import create_app
from yask.mcp_server import build_server
from yask.store import NotFound, ValidationError


# -- store ------------------------------------------------------------------

def test_set_and_list_project_roles(store, project):
    pid = project["id"]
    roles = store.set_project_roles(pid, ["reporter", "developer"])
    assert [r["name"] for r in roles] == ["reporter", "developer"]
    assert store.list_project_roles(pid) == roles


def test_project_roles_deduped_case_insensitive_preserving_order(store, project):
    pid = project["id"]
    roles = store.set_project_roles(pid, ["Dev", "dev", "BACKEND", "backend"])
    assert [r["name"] for r in roles] == ["Dev", "BACKEND"]


def test_project_roles_reject_empty_name(store, project):
    with pytest.raises(ValidationError):
        store.set_project_roles(project["id"], ["ok", "   "])


def test_project_roles_reject_slash_name(store, project):
    # '/' would make the role unreachable via DELETE .../roles/{name}
    with pytest.raises(ValidationError):
        store.set_project_roles(project["id"], ["a/b"])


def test_project_roles_replace_all(store, project):
    pid = project["id"]
    store.set_project_roles(pid, ["a", "b"])
    store.set_project_roles(pid, ["c"])
    assert [r["name"] for r in store.list_project_roles(pid)] == ["c"]


def test_project_roles_are_project_scoped(store, project):
    pid = project["id"]
    other = store.create_project("Other")["id"]
    store.set_project_roles(pid, ["shared"])
    assert store.list_project_roles(other) == []


def test_remove_project_role(store, project):
    pid = project["id"]
    store.set_project_roles(pid, ["a", "b", "c"])
    res = store.remove_project_role(pid, "b")
    assert res["applied"] is True
    assert res["name"] == "b"
    assert res["remaining"] == 2
    assert [r["name"] for r in store.list_project_roles(pid)] == ["a", "c"]


def test_remove_project_role_case_insensitive(store, project):
    pid = project["id"]
    store.set_project_roles(pid, ["Reporter"])
    store.remove_project_role(pid, "reporter")
    assert store.list_project_roles(pid) == []


def test_remove_project_role_unknown_raises_not_found(store, project):
    with pytest.raises(NotFound):
        store.remove_project_role(project["id"], "ghost")


def test_project_roles_embedded_in_get_project(store, project):
    pid = project["id"]
    store.set_project_roles(pid, ["reporter"])
    proj = store.get_project(pid)
    assert proj["roles"] == [{"id": proj["roles"][0]["id"], "name": "reporter"}]


def test_empty_roles_is_valid(store, project):
    assert store.set_project_roles(project["id"], []) == []
    assert store.list_project_roles(project["id"]) == []


# -- REST API ---------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pid(client):
    r = client.post("/api/projects", json={"name": "Roles Demo"})
    return r.json()["id"]


def test_project_roles_over_api(client, pid):
    # empty project -> empty list
    assert client.get(f"/api/projects/{pid}/roles").json() == []

    # set presets (replaces all)
    r = client.put(f"/api/projects/{pid}/roles", json={"names": ["reporter", "QA Lead"]})
    assert r.status_code == 200
    assert [role["name"] for role in r.json()] == ["reporter", "QA Lead"]
    assert client.get(f"/api/projects/{pid}/roles").json() == [
        {"id": 1, "name": "reporter"},
        {"id": 2, "name": "QA Lead"},
    ]

    # case-insensitive duplicate is de-duplicated on the server
    r = client.put(f"/api/projects/{pid}/roles", json={"names": ["Reporter", "Dev"]})
    assert r.status_code == 200
    assert [role["name"] for role in r.json()] == ["Reporter", "Dev"]

    # empty name -> 400
    assert client.put(f"/api/projects/{pid}/roles", json={"names": ["ok", "  "]}).status_code == 400
    # '/' cannot be addressed in the delete URL -> 400
    assert client.put(f"/api/projects/{pid}/roles", json={"names": ["a/b"]}).status_code == 400

    # clearing is allowed
    assert client.put(f"/api/projects/{pid}/roles", json={"names": []}).status_code == 200
    assert client.get(f"/api/projects/{pid}/roles").json() == []


def test_delete_project_role_over_api(client, pid):
    client.put(f"/api/projects/{pid}/roles", json={"names": ["a", "b"]})
    r = client.delete(f"/api/projects/{pid}/roles/b")
    assert r.status_code == 200
    assert r.json()["applied"] is True
    assert r.json()["remaining"] == 1
    assert [role["name"] for role in client.get(f"/api/projects/{pid}/roles").json()] == ["a"]


def test_delete_project_role_with_special_characters(client, pid):
    # spaces and '#' are legal in role names when percent-encoded
    # (mirrors the frontend's encodeURIComponent)
    from urllib.parse import quote

    name = "QA lead #1"
    client.put(f"/api/projects/{pid}/roles", json={"names": [name, "other"]})
    r = client.delete(f"/api/projects/{pid}/roles/{quote(name)}")
    assert r.status_code == 200
    assert r.json()["name"] == name
    assert [role["name"] for role in client.get(f"/api/projects/{pid}/roles").json()] == ["other"]


def test_delete_project_role_over_api_unknown(client, pid):
    client.put(f"/api/projects/{pid}/roles", json={"names": ["a"]})
    assert client.delete(f"/api/projects/{pid}/roles/ghost").status_code == 404


def test_project_roles_isolation_over_api(client):
    a = client.post("/api/projects", json={"name": "A"}).json()["id"]
    b = client.post("/api/projects", json={"name": "B"}).json()["id"]
    client.put(f"/api/projects/{a}/roles", json={"names": ["shared"]})
    assert client.get(f"/api/projects/{b}/roles").json() == []


def test_project_role_endpoints_404_on_unknown_project(client):
    assert client.get("/api/projects/999/roles").status_code == 404
    assert client.put("/api/projects/999/roles", json={"names": []}).status_code == 404
    assert client.delete("/api/projects/999/roles/x").status_code == 404


# -- MCP --------------------------------------------------------------------

def _call(store, tool, **args):
    server = build_server(store)
    blocks = asyncio.run(server.call_tool(tool, args))
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return blocks


def test_role_tools_are_registered(store, project):
    server = build_server(store)

    async def names():
        return sorted(t.name for t in await server.list_tools())

    names_list = asyncio.run(names())
    for tool in ("list_project_roles", "set_project_roles", "remove_project_role"):
        assert tool in names_list


def test_role_tools_expose_project_by_name(store, project):
    server = build_server(store)

    async def schemas():
        return {t.name: t.inputSchema for t in await server.list_tools()}

    schemas_by_name = asyncio.run(schemas())
    for name in ("list_project_roles", "set_project_roles", "remove_project_role"):
        props = schemas_by_name[name]["properties"]
        assert "project" in props
        assert "project_id" not in props


def test_set_list_remove_roles_via_mcp(store, project):
    res = _call(store, "set_project_roles", project="demo", names=["reporter", "dev"])
    roles = [json.loads(c.text) for c in res if isinstance(c, TextContent)]
    assert [r["name"] for r in roles] == ["reporter", "dev"]

    res = _call(store, "list_project_roles", project=project["name"])
    listed = [json.loads(c.text) for c in res if isinstance(c, TextContent)]
    assert [r["name"] for r in listed] == ["reporter", "dev"]  # insertion order preserved

    res = _call(store, "remove_project_role", project=project["name"], name="dev")
    data = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert data["applied"] is True
    assert data["remaining"] == 1

    res = _call(store, "list_project_roles", project=project["name"])
    listed = [json.loads(c.text) for c in res if isinstance(c, TextContent)]
    assert [r["name"] for r in listed] == ["reporter"]


def test_remove_unknown_project_role_via_mcp(store, project):
    res = _call(store, "remove_project_role", project=project["name"], name="ghost")
    data = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]


def test_set_roles_error_surfaces_readable_payload_via_mcp(store, project):
    # Domain errors in this list-returning tool must surface as the agreed
    # readable {"ok": False, "error": ...} payload, not a pydantic
    # output-validation ToolError (see #13).
    res = _call(store, "set_project_roles", project=project["name"], names=["ok", "  "])
    data = json.loads([c for c in res if isinstance(c, TextContent)][0].text)
    assert data["ok"] is False
    assert "error" in data
