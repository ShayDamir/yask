"""The Blocked holding state (#24).

Blocked is a holding state parallel to Archived: it is not a forward workflow
stage, is skipped by the pick loop, is never pulled forward through the
prerequisite cascade, and a move to it affects only the one task.
"""

import pytest

from yask import db
from yask.store import ValidationError

from tests.test_mcp import result as mcp_result


def _mk(store, pid, title, state):
    t = store.create_task(pid, title)
    if state != "Backlog":
        store.move_task(pid, t["number"], state, confirm=True)
    return store.get_task(pid, t["number"])


def test_block_is_a_valid_state_constant(store):
    assert db.BLOCKED_STATE == "Blocked"
    assert db.BLOCKED_STATE in db.ALL_STATES
    assert db.BLOCKED_STATE not in db.WORKFLOW_STATES  # holding, not a forward stage
    assert db.BLOCKED_STATE in db.HOLDING_STATES


def test_move_to_block_single_task_no_confirmation(store, project):
    pid = project["id"]
    t = store.create_task(pid, "solo")
    res = store.move_task(pid, t["number"], "Blocked")  # no confirm arg
    assert res["applied"] is True
    assert [a["number"] for a in res["affected"]] == [t["number"]]
    assert store.get_task(pid, t["number"])["state"] == "Blocked"
    hist = store.get_history(pid, t["number"])
    assert hist[-1]["from_state"] == "Backlog"
    assert hist[-1]["to_state"] == "Blocked"
    assert hist[-1]["source"] == "web"


def test_block_does_not_pull_backlog_prereq(store, project):
    pid = project["id"]
    t = store.create_task(pid, "task")
    p = store.create_task(pid, "prereq")  # Backlog, earlier than Blocked is irrelevant
    store.set_prerequisites(pid, t["number"], [p["number"]])
    res = store.move_task(pid, t["number"], "Blocked")
    assert [a["number"] for a in res["affected"]] == [t["number"]]
    assert store.get_task(pid, p["number"])["state"] == "Backlog"


def test_move_out_of_blocked_pulls_forward_and_ignores_holding_prereqs(store, project):
    pid = project["id"]
    t = _mk(store, pid, "task", "Blocked")
    # an earlier-stage (Backlog) prerequisite that should be pulled to Todo
    p_back = store.create_task(pid, "prereq-backlog")
    # a prerequisite already Blocked that must be left alone
    p_block = _mk(store, pid, "prereq-blocked", "Blocked")
    store.set_prerequisites(pid, t["number"], [p_back["number"], p_block["number"]])

    res = store.move_task(pid, t["number"], "Todo", confirm=True)
    moved = {a["number"] for a in res["affected"]}
    assert moved == {t["number"], p_back["number"]}
    assert store.get_task(pid, t["number"])["state"] == "Todo"
    assert store.get_task(pid, p_back["number"])["state"] == "Todo"
    assert store.get_task(pid, p_block["number"])["state"] == "Blocked"


def test_list_tasks_by_blocked_state(store, project):
    pid = project["id"]
    t = store.create_task(pid, "block me")
    store.move_task(pid, t["number"], "Blocked")
    other = store.create_task(pid, "keep going")
    store.move_task(pid, other["number"], "Todo")

    blocked = store.list_tasks(pid, state="Blocked")
    assert [b["number"] for b in blocked] == [t["number"]]
    # Blocked is not hidden by default (only Archived is).
    default = store.list_tasks(pid)
    assert any(b["number"] == t["number"] for b in default)


def test_get_next_task_skips_blocked(store, project):
    # A Blocked task is never returned; a Todo task is returned instead.
    blocked = store.create_task(project["id"], "blocked one")
    store.move_task(project["id"], blocked["number"], "Blocked")
    todo = store.create_task(project["id"], "todo")
    store.move_task(project["id"], todo["number"], "Todo")
    got = mcp_result(store, "get_next_task", project=project["id"])
    assert got is not None
    assert got["number"] == todo["number"]


def test_get_next_task_blocked_only_returns_null(store, project):
    # A project whose only forward-state task is Blocked yields nothing.
    blocked = store.create_task(project["id"], "blocked one")
    store.move_task(project["id"], blocked["number"], "Blocked")
    assert mcp_result(store, "get_next_task", project=project["id"]) is None


def test_restore_task_rejects_blocked(store, project):
    pid = project["id"]
    t = store.create_task(pid, "blocked one")
    store.move_task(pid, t["number"], "Blocked")
    with pytest.raises(ValidationError):
        store.restore_task(pid, t["number"])


def test_mcp_move_to_block_applies(store, project):
    number = store.create_task(project["id"], "t")["number"]
    data = mcp_result(store, "move_task", project=project["name"],
                      number=number, to_state="Blocked")
    assert data["applied"] is True
    assert store.get_task(project["id"], number)["state"] == "Blocked"
