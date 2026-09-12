"""Tests for logging the source (web/mcp/telegram) of task actions (#22).

Every recorded change — a task's creation and each state transition — should
say whether it came from the web UI (REST API), the MCP surface or the
Telegram bot.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from yask import db
from yask.api import create_app
from yask.mcp_server import build_server
from yask.store import Store, ValidationError


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pid(client):
    r = client.post("/api/projects", json={"name": "Source Demo"})
    assert r.status_code == 201
    return r.json()["id"]


def call(store, name, **args):
    """Drive one MCP tool; build_server stamps the store's source to mcp."""
    server = build_server(store)
    blocks = asyncio.run(server.call_tool(name, args))
    return blocks[0] if isinstance(blocks, tuple) else blocks


def srcs(store, pid, number):
    return [e["source"] for e in store.get_history(pid, number)]


def test_creation_logs_source_and_created_by(store, project):
    # default store is web
    t = store.create_task(project["id"], "First")
    assert t["created_by"] == "web"
    hist = store.get_history(project["id"], t["number"])
    assert hist and hist[0]["source"] == "web"


def test_every_state_change_carries_source(store, project):
    t = store.create_task(project["id"], "T")
    pid = project["id"]
    store.move_task(pid, t["number"], "Todo", confirm=True)
    store.move_task(pid, t["number"], "Done", confirm=True)
    store.archive_task(pid, t["number"], confirm=True)
    assert all(s == "web" for s in srcs(store, pid, t["number"]))


def test_store_rejects_unknown_source(store):
    with pytest.raises(ValidationError):
        Store(store.conn, source="cli")


def test_mcp_actions_logged_as_mcp(store, project):
    pid = project["id"]
    # First MCP call flips store.source to "mcp" (build_server sets it).
    call(store, "list_projects", project=project["name"])
    # now store actions are attributed to mcp
    t = store.create_task(pid, "Via MCP")
    assert t["created_by"] == "mcp"
    assert all(s == "mcp" for s in srcs(store, pid, t["number"]))
    # and an MCP-tool-driven move is attributed to mcp too
    call(
        store,
        "move_task",
        project=project["name"],
        number=t["number"],
        to_state="Todo",
        confirm=True,
    )
    assert all(s == "mcp" for s in srcs(store, pid, t["number"]))


def test_telegram_actions_logged_as_telegram(store, project):
    """The bot process opens its store with source='telegram'; its actions —
    a creation and a move alike — are attributed to telegram."""
    bot = Store(store.conn, source="telegram")
    pid = project["id"]
    t = bot.create_task(pid, "From the bot")
    assert t["created_by"] == "telegram"
    bot.move_task(pid, t["number"], "Todo", confirm=True)
    assert all(s == "telegram" for s in srcs(store, pid, t["number"]))


def test_api_actions_logged_as_web(client, pid):
    r = client.post(f"/api/projects/{pid}/tasks", json={"title": "first"})
    assert r.status_code == 201
    t = r.json()
    assert t["created_by"] == "web"
    hist = client.get(f"/api/projects/{pid}/tasks/{t['number']}/history").json()
    assert hist and all(e["source"] == "web" for e in hist)
    # a state change via the API is also attributed to web
    client.post(f"/api/projects/{pid}/tasks/{t['number']}/move", json={"to_state": "Todo"})
    hist = client.get(f"/api/projects/{pid}/tasks/{t['number']}/history").json()
    assert all(e["source"] == "web" for e in hist)


def test_cascade_prereqs_share_source(store, project):
    pid = project["id"]
    # flip the store to mcp (as build_server would) so every action here is mcp
    call(store, "list_projects", project=project["name"])
    prereq = store.create_task(pid, "prereq")
    main = store.create_task(pid, "main")
    store.set_prerequisites(pid, main["number"], [prereq["number"]])
    # move main through MCP; cascade should pull the Backlog prereq too
    call(
        store,
        "move_task",
        project=project["name"],
        number=main["number"],
        to_state="Todo",
        confirm=True,
    )
    for m in (main, prereq):
        assert all(s == "mcp" for s in srcs(store, pid, m["number"]))


def test_migration_backfills_existing_database(tmp_path):
    path = tmp_path / "legacy.db"
    # Build a DB with the old schema (no created_by / source columns).
    conn = db.connect(path)
    pid = conn.execute(
        "INSERT INTO projects(name, next_task_number, created_at)"
        " VALUES ('Legacy', 1, '2020-01-01T00:00:00Z')"
    ).lastrowid
    # connect() already seeds task_types; use an existing id (Task).
    conn.execute(
        "INSERT INTO tasks(project_id, number, title, description, type_id, state,"
        " estimate, parent_id, sort_order, created_at, updated_at)"
        " VALUES (?, 1, 'old', '', 2, 'Backlog', NULL, NULL, 1,"
        " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO state_history(task_id, from_state, to_state, changed_at)"
        " VALUES (1, NULL, 'Backlog', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Re-open with connect(): the migration should add the columns.
    conn = db.connect(path)
    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    hist_cols = {r["name"] for r in conn.execute("PRAGMA table_info(state_history)")}
    assert "created_by" in task_cols
    assert "source" in hist_cols
    legacy_task = conn.execute("SELECT created_by FROM tasks WHERE id = 1").fetchone()
    legacy_hist = conn.execute("SELECT source FROM state_history WHERE id = 1").fetchone()
    assert legacy_task["created_by"] == "unknown"
    assert legacy_hist["source"] == "unknown"
    conn.close()
