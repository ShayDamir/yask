"""Length limits on free-text fields (task #126).

The store is the single enforcement point for the limits in
``yask.spec.FIELD_LIMITS``: a value beyond its limit raises
``ValidationError``, which every entry point already surfaces (REST 400,
MCP error object, bot domain message). These are store-level boundary
tests — exactly the limit passes, limit+1 is rejected. The REST and MCP
surfaces are covered in ``test_api.py`` / ``test_mcp.py``; the web
``maxlength`` mirror is pinned by ``test_codegen.py``.
"""

import pytest

from yask import spec
from yask.store import ValidationError

LIMITS = spec.FIELD_LIMITS


def test_create_task_title_boundary(store, project):
    pid = project["id"]
    ok = store.create_task(pid, "a" * LIMITS["title"])
    assert ok["title"] == "a" * LIMITS["title"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.create_task(pid, "a" * (LIMITS["title"] + 1))


def test_create_task_description_boundary(store, project):
    pid = project["id"]
    ok = store.create_task(pid, "t", description="x" * LIMITS["description"])
    assert len(ok["description"]) == LIMITS["description"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.create_task(pid, "t2", description="x" * (LIMITS["description"] + 1))


def test_update_task_title_boundary(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    ok = store.update_task(pid, t["number"], title="a" * LIMITS["title"])
    assert ok["title"] == "a" * LIMITS["title"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.update_task(pid, t["number"], title="a" * (LIMITS["title"] + 1))
    # the failed update changed nothing
    assert store.get_task(pid, t["number"])["title"] == "a" * LIMITS["title"]


def test_update_task_description_boundary(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    ok = store.update_task(pid, t["number"], description="x" * LIMITS["description"])
    assert len(ok["description"]) == LIMITS["description"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.update_task(
            pid, t["number"], description="x" * (LIMITS["description"] + 1)
        )
    # the failed update changed nothing
    assert len(store.get_task(pid, t["number"])["description"]) == LIMITS["description"]
    # description=None means "unchanged" and stays free of the check:
    # re-saving a task that already holds a limit-sized description is fine.
    ok2 = store.update_task(pid, t["number"], description=None)
    assert len(ok2["description"]) == LIMITS["description"]


def test_create_project_name_boundary(store):
    ok = store.create_project("p" * LIMITS["projectName"])
    assert ok["name"] == "p" * LIMITS["projectName"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.create_project("p" * (LIMITS["projectName"] + 1))


def test_task_type_name_boundary(store):
    ok = store.create_task_type("t" * LIMITS["taskTypeName"])
    assert ok["name"] == "t" * LIMITS["taskTypeName"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.create_task_type("t" * (LIMITS["taskTypeName"] + 1))
    ok2 = store.rename_task_type(ok["id"], "r" * LIMITS["taskTypeName"])
    assert ok2["name"] == "r" * LIMITS["taskTypeName"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.rename_task_type(ok["id"], "r" * (LIMITS["taskTypeName"] + 1))


def test_create_label_name_boundary(store, project):
    pid = project["id"]
    ok = store.create_label(pid, "l" * LIMITS["labelName"])
    assert ok["name"] == "l" * LIMITS["labelName"]
    with pytest.raises(ValidationError, match="exceeds"):
        store.create_label(pid, "l" * (LIMITS["labelName"] + 1))


def test_set_project_roles_name_boundary(store, project):
    pid = project["id"]
    store.set_project_roles(pid, ["dev"])
    with pytest.raises(ValidationError, match="exceeds"):
        store.set_project_roles(pid, ["dev", "r" * (LIMITS["roleName"] + 1)])
    # raised before _replace_links: the previous roles are untouched
    assert [r["name"] for r in store.list_project_roles(pid)] == ["dev"]
    # a role at exactly the limit is accepted
    ok = store.set_project_roles(pid, ["dev", "r" * LIMITS["roleName"]])
    assert [r["name"] for r in ok] == ["dev", "r" * LIMITS["roleName"]]


def test_add_attachment_filename_rejected_beyond_limit(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    for filename in ("a" * 2048, "a" * 3000 + ".md"):
        with pytest.raises(ValidationError, match="exceeds"):
            store.add_attachment(
                pid, t["number"], filename, "text/markdown", b"# hi"
            )
    # nothing was persisted
    assert store.list_attachments(pid, t["number"]) == []
    # a filename at exactly the limit still attaches
    ok = store.add_attachment(
        pid, t["number"], "a" * (LIMITS["attachmentFilename"] - 3) + ".md",
        "text/markdown", b"# hi",
    )
    assert len(ok["filename"]) == LIMITS["attachmentFilename"]


def test_legacy_oversized_filename_capped_at_read(store, project):
    """A row stored before the limit existed is capped on every read path,
    so the oversized name never reaches the Content-Disposition header, the
    MCP JSON payloads, or the bot's messages."""
    pid = project["id"]
    t = store.create_task(pid, "t")
    legacy_name = "b" * 10240
    cur = store.conn.execute(
        "INSERT INTO attachments(task_id, filename, content_type, data, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (t["id"], legacy_name, "text/markdown", b"legacy",
         "2026-01-01T00:00:00Z"),
    )
    att_id = cur.lastrowid

    expected = legacy_name[: LIMITS["attachmentFilename"]]
    assert len(expected) == LIMITS["attachmentFilename"]

    meta, data = store.get_attachment(att_id)
    assert data == b"legacy"
    assert meta["filename"] == expected

    listed = store.list_attachments(pid, t["number"])
    assert [a["filename"] for a in listed] == [expected]

    last_meta, _ = store.last_attachment(pid, t["number"])
    assert last_meta["filename"] == expected
