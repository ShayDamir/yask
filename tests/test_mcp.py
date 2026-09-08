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
    return blocks


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
