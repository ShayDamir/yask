"""Tests for the MCP tool surface (build_server + call_tool)."""

import asyncio
import base64
import json

from mcp.types import ImageContent, TextContent

from yask.mcp_server import build_server

# 1x1 transparent PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def call(store, name, **args):
    server = build_server(store)
    blocks = asyncio.run(server.call_tool(name, args))
    assert blocks
    # call_tool returns either a flat list of content blocks or a tuple
    # (blocks, structured_result); normalize to the flat list.
    return blocks[0] if isinstance(blocks, tuple) else blocks


def texts(blocks):
    return [c for c in blocks if isinstance(c, TextContent)]


def test_get_attachment_text(store, project):
    t = store.create_task(project["id"], "T")
    att = store.add_attachment(
        project["id"], t["number"], "notes.md", "text/markdown", b"# Hello\n\nworld"
    )
    res = call(store, "get_attachment", attachment_id=att["id"])
    data = json.loads(texts(res)[0].text)
    assert data["filename"] == "notes.md"
    assert data["content_type"] == "text/markdown"
    assert data["text"] == "# Hello\n\nworld"


def test_get_attachment_image(store, project):
    t = store.create_task(project["id"], "T")
    att = store.add_attachment(project["id"], t["number"], "px.png", "image/png", PNG_1X1)
    res = call(store, "get_attachment", attachment_id=att["id"])
    imgs = [c for c in res if isinstance(c, ImageContent)]
    assert len(texts(res)) == 1
    assert len(imgs) == 1
    meta = json.loads(texts(res)[0].text)
    assert meta["filename"] == "px.png"
    assert meta["size"] == len(PNG_1X1)
    assert imgs[0].mimeType == "image/png"
    assert base64.b64decode(imgs[0].data) == PNG_1X1


def test_get_attachment_missing(store, project):
    res = call(store, "get_attachment", attachment_id=999)
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]


def test_get_attachment_is_registered(store, project):
    server = build_server(store)

    async def names():
        return sorted(t.name for t in await server.list_tools())

    names_list = asyncio.run(names())
    assert "get_attachment" in names_list
    assert "delete_attachment" in names_list


def test_delete_attachment(store, project):
    t = store.create_task(project["id"], "T")
    att = store.add_attachment(
        project["id"], t["number"], "x.md", "text/markdown", b"bye"
    )
    res = call(store, "delete_attachment", attachment_id=att["id"])
    assert json.loads(texts(res)[0].text) == {"deleted": att["id"]}
    assert store.list_attachments(project["id"], t["number"]) == []
    res = call(store, "delete_attachment", attachment_id=att["id"])
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]


PROJECT_SCOPED = (
    "get_project",
    "list_tasks",
    "create_task",
    "update_task",
    "set_prerequisites",
    "move_task",
    "archive_task",
    "restore_task",
    "delete_task",
    "reorder_task",
    "get_task_history",
    "add_attachment",
    "list_attachments",
)
NOT_PROJECT_SCOPED = ("create_project", "list_task_types", "add_task_type",
                      "get_attachment", "delete_attachment")


def test_tools_expose_project_by_name(store, project):
    server = build_server(store)

    async def schemas():
        return {t.name: t.inputSchema for t in await server.list_tools()}

    schemas_by_name = asyncio.run(schemas())
    for name in PROJECT_SCOPED:
        props = schemas_by_name[name]["properties"]
        assert "project" in props, f"{name} should expose 'project'"
        assert "project_id" not in props, f"{name} must not expose 'project_id'"
        # annotation is str | int, which FastMCP renders as anyOf
        union = props["project"]["anyOf"]
        assert {"type": "string"} in union and {"type": "integer"} in union, (
            f"{name} 'project' must accept name or id"
        )
    for name in NOT_PROJECT_SCOPED:
        assert "project" not in schemas_by_name[name]["properties"]


def test_tools_resolve_project_by_name(store, project):
    store.create_task(project["id"], "Resolve me")
    # list_tasks converts each list item to its own text block
    res = call(store, "list_tasks", project="demo")  # created with name "Demo"
    titles = [data["title"] for tb in texts(res) for data in [json.loads(tb.text)]]
    assert titles == ["Resolve me"]
    # numeric id still works
    res = call(store, "get_project", project=project["id"])
    assert json.loads(texts(res)[0].text)["name"] == "Demo"


def test_tools_reject_unknown_project(store, project):
    res = call(store, "get_project", project="nope")
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]


def test_update_task_type_via_mcp(store, project):
    t = store.create_task(project["id"], "Retype me", type="Story")
    res = call(store, "update_task", project=project["name"],
               number=t["number"], type="Bug")
    data = json.loads(texts(res)[0].text)
    assert data["type"] == "Bug"
    assert store.get_task(project["id"], t["number"])["type"] == "Bug"


def test_all_digit_project_name_resolves(store):
    p = store.create_project("123")
    store.create_task(p["id"], "Numeric name task")
    res = call(store, "list_tasks", project="123")
    titles = [data["title"] for tb in texts(res) for data in [json.loads(tb.text)]]
    assert titles == ["Numeric name task"]
