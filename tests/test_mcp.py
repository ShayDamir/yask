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


def test_add_attachment_from_file_path(store, project, tmp_path):
    t = store.create_task(project["id"], "T")
    md_file = tmp_path / "notes.md"
    md_file.write_bytes(b"# From file\n")
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        file_path=str(md_file),
    )
    meta = json.loads(texts(res)[0].text)
    assert meta["filename"] == "notes.md"
    assert meta["content_type"] == "text/markdown"
    got_meta, data = store.get_attachment(meta["id"])
    assert data == b"# From file\n"
    # viewer round-trip confirms the stored type is usable
    view = call(store, "get_attachment", attachment_id=meta["id"])
    assert json.loads(texts(view)[0].text)["text"] == "# From file\n"


def test_add_attachment_file_path_explicit_filename_content_type(store, project, tmp_path):
    t = store.create_task(project["id"], "T")
    png_file = tmp_path / "logo.png"
    png_file.write_bytes(PNG_1X1)
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        file_path=str(png_file),
        filename="renamed.png",
        content_type="image/png",
    )
    meta = json.loads(texts(res)[0].text)
    assert meta["filename"] == "renamed.png"
    assert meta["content_type"] == "image/png"


def test_add_attachment_file_path_infers_image_type(store, project, tmp_path):
    t = store.create_task(project["id"], "T")
    png_file = tmp_path / "logo.png"
    png_file.write_bytes(PNG_1X1)
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        file_path=str(png_file),
    )
    meta = json.loads(texts(res)[0].text)
    assert meta["filename"] == "logo.png"  # basename used by default
    assert meta["content_type"] == "image/png"  # inferred from extension


def test_add_attachment_file_path_missing(store, project, tmp_path):
    t = store.create_task(project["id"], "T")
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        file_path=str(tmp_path / "nope.md"),
    )
    assert json.loads(texts(res)[0].text)["ok"] is False


def test_add_attachment_requires_data_base64_or_file_path(store, project):
    t = store.create_task(project["id"], "T")
    res = call(store, "add_attachment", project=project["name"], number=t["number"])
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "data_base64 or file_path" in data["error"]


def test_add_attachment_unknown_extension(store, project, tmp_path):
    t = store.create_task(project["id"], "T")
    f = tmp_path / "x.weird"
    f.write_bytes(b"hi")
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        file_path=str(f),
    )
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "content type" in data["error"]


def test_add_attachment_data_base64_roundtrip(store, project):
    t = store.create_task(project["id"], "T")
    content = b"# b64 note"
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        filename="b64.md",
        data_base64=base64.b64encode(content).decode(),
    )
    meta = json.loads(texts(res)[0].text)
    assert meta["content_type"] == "text/markdown"
    got_meta, data = store.get_attachment(meta["id"])
    assert data == content


def test_add_attachment_bad_base64(store, project):
    t = store.create_task(project["id"], "T")
    res = call(
        store,
        "add_attachment",
        project=project["name"],
        number=t["number"],
        filename="bad.md",
        data_base64="@@not-base64@@",
    )
    data = json.loads(texts(res)[0].text)
    assert data["ok"] is False
    assert "data_base64" in data["error"]


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
    "create_label",
    "list_labels",
    "set_task_labels",
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
