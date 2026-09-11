"""Core domain rules: numbering, project isolation, types, ordering."""

import pytest

from yask.store import Conflict, NotFound, ValidationError


def test_task_numbers_start_at_one_and_increase(store, project):
    a = store.create_task(project["id"], "one")
    b = store.create_task(project["id"], "two")
    c = store.create_task(project["id"], "three")
    assert (a["number"], b["number"], c["number"]) == (1, 2, 3)


def test_task_numbers_are_per_project(store):
    p1 = store.create_project("A")["id"]
    p2 = store.create_project("B")["id"]
    a1 = store.create_task(p1, "a1")
    b1 = store.create_task(p2, "b1")
    a2 = store.create_task(p1, "a2")
    assert a1["number"] == 1
    assert b1["number"] == 1
    assert a2["number"] == 2
    # completely separate state
    assert store.list_tasks(p1)[0]["title"] == "a1"
    assert store.list_tasks(p2)[0]["title"] == "b1"


def test_numbers_never_reused_after_delete(store, project):
    pid = project["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    c = store.create_task(pid, "c")
    with pytest.raises(ValidationError):
        store.delete_task(pid, b["number"], confirm=True)
    store.archive_task(pid, b["number"], confirm=True)
    store.delete_task(pid, b["number"], confirm=True)
    d = store.create_task(pid, "d")
    assert d["number"] == 4


def test_seed_task_types(store):
    types = store.list_task_types()
    names = [t["name"] for t in types]
    assert {"Story", "Task", "Bug", "Epic"} <= set(names)
    epic = next(t for t in types if t["name"] == "Epic")
    assert epic["is_epic"] == 1


def test_task_types_are_extensible(store, project):
    t = store.create_task_type("Spike")
    task = store.create_task(project["id"], "spike", type="Spike")
    assert task["type"] == "Spike"
    store.rename_task_type(t["id"], "Spike2")
    assert store.get_task(project["id"], task["number"])["type"] == "Spike2"


def test_cannot_rename_type_to_existing(store):
    t = store.create_task_type("Spike")
    with pytest.raises(Conflict):
        store.rename_task_type(t["id"], "Story")


def test_cannot_delete_type_in_use(store, project):
    t = store.create_task_type("Spike")
    store.create_task(project["id"], "s", type="Spike")
    with pytest.raises(Conflict):
        store.delete_task_type(t["id"])


def test_cannot_delete_epic_type(store):
    epic = next(t for t in store.list_task_types() if t["name"] == "Epic")
    with pytest.raises(ValidationError):
        store.delete_task_type(epic["id"])


def test_all_states_available(store):
    from yask import db

    assert db.WORKFLOW_STATES == [
        "Backlog", "Todo", "Planning", "In progress", "Review", "Done",
    ]
    assert db.ARCHIVED_STATE == "Archived"


def test_new_tasks_always_landed_in_backlog(store, project):
    # new tasks can only be added to the Backlog (#1); there is no
    # state parameter to create elsewhere, and history records the
    # single Backlog entry
    t = store.create_task(project["id"], "x")
    assert t["state"] == "Backlog"
    assert [h["to_state"] for h in store.get_history(project["id"], t["number"])] == [
        "Backlog"
    ]
    # positioning within the Backlog still works
    a = store.create_task(project["id"], "a")
    c = store.create_task(project["id"], "c", before_number=a["number"])
    assert [h["state"] for h in store.list_tasks(project["id"])] == [
        "Backlog",
        "Backlog",
        "Backlog",
    ]
    assert [t["number"] for t in store.list_tasks(project["id"])] == [
        t["number"],
        c["number"],
        a["number"],
    ]


def test_sorting_preserved_top_down(store, project):
    pid = project["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    c = store.create_task(pid, "c")
    # move c to the front (before a)
    store.reorder_task(pid, c["number"], before_number=a["number"])
    order = [t["number"] for t in store.list_tasks(pid)]
    assert order == [c["number"], a["number"], b["number"]]
    # inserting d between a and b
    d = store.create_task(pid, "d", after_number=a["number"])
    order = [t["number"] for t in store.list_tasks(pid)]
    assert order == [c["number"], a["number"], d["number"], b["number"]]


def test_reorder_within_column_stable(store, project):
    pid = project["id"]
    nums = [store.create_task(pid, f"t{i}")["number"] for i in range(4)]
    store.reorder_task(pid, nums[0], after_number=nums[2])
    order = [t["number"] for t in store.list_tasks(pid)]
    assert order == [nums[1], nums[2], nums[0], nums[3]]


def test_reorder_self_reference_is_noop(store, project):
    """Reordering relative to itself is a clean no-op (#51), not a 500."""
    pid = project["id"]
    nums = [store.create_task(pid, f"t{i}")["number"] for i in range(4)]
    before = store.get_task(pid, nums[0])
    out = store.reorder_task(pid, nums[0], after_number=nums[0])
    assert out["number"] == nums[0]
    out = store.reorder_task(pid, nums[0], before_number=nums[0])
    assert out["number"] == nums[0]
    # column order unchanged, and a no-op performs no writes
    order = [t["number"] for t in store.list_tasks(pid)]
    assert order == nums
    assert store.get_task(pid, nums[0])["updated_at"] == before["updated_at"]
    # both before and after set is still a validation error, even self-set
    with pytest.raises(ValidationError):
        store.reorder_task(pid, nums[0], before_number=nums[0], after_number=nums[0])


def test_move_task_self_position_reference_rejected(store, project):
    """A self position reference in a move is a 4xx ValidationError, not a 500."""
    pid = project["id"]
    a = store.create_task(pid, "a")
    with pytest.raises(ValidationError):
        store.move_task(pid, a["number"], "Todo", after_number=a["number"])
    with pytest.raises(ValidationError):
        store.move_task(pid, a["number"], "Todo", before_number=a["number"])


def test_project_overviews_empty_store(store):
    assert store.list_project_overviews() == []


def test_project_overviews_counts_and_order(store):
    alpha = store.create_project("alpha")["id"]
    beta = store.create_project("Beta")["id"]
    zeta = store.create_project("zeta")["id"]

    store.create_task(alpha, "a1")
    store.create_task(alpha, "a2")
    a3 = store.create_task(alpha, "a3")
    store.move_task(alpha, a3["number"], "Todo", confirm=True)
    a4 = store.create_task(alpha, "a4")
    store.move_task(alpha, a4["number"], "In progress", confirm=True)
    a5 = store.create_task(alpha, "a5")
    store.move_task(alpha, a5["number"], "Blocked")
    a6 = store.create_task(alpha, "a6")
    store.archive_task(alpha, a6["number"], confirm=True)

    b1 = store.create_task(beta, "b1")
    store.move_task(beta, b1["number"], "Done", confirm=True)

    # zeta stays taskless

    overviews = store.list_project_overviews()
    # same name order as list_projects (case-insensitive)
    assert [(o["id"], o["name"]) for o in overviews] == [
        (alpha, "alpha"),
        (beta, "Beta"),
        (zeta, "zeta"),
    ]
    assert [o["name"] for o in overviews] == [
        p["name"] for p in store.list_projects()
    ]

    # zero-count states absent; Archived tasks excluded
    assert overviews[0]["states"] == {
        "Backlog": 2,
        "Todo": 1,
        "In progress": 1,
        "Blocked": 1,
    }
    # canonical state order inside the dict (Blocked last)
    assert list(overviews[0]["states"]) == [
        "Backlog",
        "Todo",
        "In progress",
        "Blocked",
    ]
    assert overviews[1]["states"] == {"Done": 1}
    # a project with no visible tasks still appears, without a state segment
    assert overviews[2]["states"] == {}


def test_list_in_progress_empty(store, project):
    assert store.list_in_progress(project["id"]) == []


def test_list_in_progress_states_and_isolation(store):
    alpha = store.create_project("alpha")["id"]
    beta = store.create_project("beta")["id"]

    # alpha: one task in every state
    backlog = store.create_task(alpha, "backlog")
    todo = store.create_task(alpha, "todo")
    store.move_task(alpha, todo["number"], "Todo", confirm=True)
    planning = store.create_task(alpha, "planning")
    store.move_task(alpha, planning["number"], "Planning", confirm=True)
    working = store.create_task(alpha, "working")
    store.move_task(alpha, working["number"], "In progress", confirm=True)
    review = store.create_task(alpha, "review")
    store.move_task(alpha, review["number"], "Review", confirm=True)
    done = store.create_task(alpha, "done")
    store.move_task(alpha, done["number"], "Done", confirm=True)
    blocked = store.create_task(alpha, "blocked")
    store.move_task(alpha, blocked["number"], "Blocked")
    archived = store.create_task(alpha, "archived")
    store.archive_task(alpha, archived["number"], confirm=True)

    # beta: a Todo task that must not leak into alpha's view
    other = store.create_task(beta, "other todo")
    store.move_task(beta, other["number"], "Todo", confirm=True)

    out = store.list_in_progress(alpha)
    assert out == [
        {"number": todo["number"], "title": "todo", "state": "Todo"},
        {"number": planning["number"], "title": "planning", "state": "Planning"},
        {"number": working["number"], "title": "working", "state": "In progress"},
        {"number": review["number"], "title": "review", "state": "Review"},
    ]
    # the excluded states never appear
    for t in out:
        assert t["state"] not in ("Backlog", "Done", "Blocked", "Archived")
    # per-project isolation
    assert all(t["title"] != "other todo" for t in out)


def test_list_in_progress_ordering(store, project):
    pid = project["id"]
    # Todo: create a then b, then move b before a (column order != creation
    # order) to prove sort_order is honored
    a = store.create_task(pid, "a")
    store.move_task(pid, a["number"], "Todo", confirm=True)
    b = store.create_task(pid, "b")
    store.move_task(pid, b["number"], "Todo", confirm=True)
    store.reorder_task(pid, b["number"], before_number=a["number"])
    c = store.create_task(pid, "c")
    store.move_task(pid, c["number"], "Planning", confirm=True)
    d = store.create_task(pid, "d")
    store.move_task(pid, d["number"], "In progress", confirm=True)
    e = store.create_task(pid, "e")
    store.move_task(pid, e["number"], "Review", confirm=True)

    out = store.list_in_progress(pid)
    # states in workflow order; within Todo, b (reordered first) precedes a
    assert [(t["state"], t["title"]) for t in out] == [
        ("Todo", "b"),
        ("Todo", "a"),
        ("Planning", "c"),
        ("In progress", "d"),
        ("Review", "e"),
    ]


def test_list_in_progress_unknown_project(store):
    with pytest.raises(NotFound):
        store.list_in_progress(999)


def test_find_tasks_by_title_case_insensitive_and_number_order(store):
    pid = store.create_project("yask")["id"]
    a = store.create_task(pid, "Fix the bug")
    store.move_task(pid, a["number"], "Todo", confirm=True)
    store.create_task(pid, "other")
    c = store.create_task(pid, "fix the BUG")
    out = store.find_tasks_by_title(pid, "fIx tHe BuG")
    assert out == [
        {"number": a["number"], "title": "Fix the bug", "state": "Todo"},
        {"number": c["number"], "title": "fix the BUG", "state": "Backlog"},
    ]


def test_find_tasks_by_title_archived_excluded_and_isolated(store):
    a = store.create_project("alpha")["id"]
    b = store.create_project("beta")["id"]
    visible = store.create_task(a, "shared")
    gone = store.create_task(a, "shared")
    store.archive_task(a, gone["number"], confirm=True)
    other = store.create_task(b, "shared")
    # archived excluded; beta's same-title task does not leak
    assert store.find_tasks_by_title(a, "shared") == [
        {"number": visible["number"], "title": "shared", "state": "Backlog"}
    ]
    assert store.find_tasks_by_title(b, "shared") == [
        {"number": other["number"], "title": "shared", "state": "Backlog"}
    ]


def test_find_tasks_by_title_no_match_and_unknown_project(store, project):
    store.create_task(project["id"], "something else")
    assert store.find_tasks_by_title(project["id"], "missing") == []
    with pytest.raises(NotFound):
        store.find_tasks_by_title(999, "missing")


def test_get_task_attachment_meta_and_bytes(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    meta = store.add_attachment(
        pid, t["number"], "plan.md", "text/markdown", b"# Plan"
    )
    got, data = store.get_task_attachment(pid, t["number"], meta["id"])
    assert data == b"# Plan"
    base, _ = store.get_attachment(meta["id"])
    assert got == base


def test_get_task_attachment_scoped_to_task(store):
    a = store.create_project("alpha")["id"]
    b = store.create_project("beta")["id"]
    ta = store.create_task(a, "a")
    tb = store.create_task(b, "b")
    meta = store.add_attachment(a, ta["number"], "a.md", "text/markdown", b"a")
    # an id belonging to a different task is not found
    with pytest.raises(NotFound):
        store.get_task_attachment(b, tb["number"], meta["id"])
    # unknown ids are not found
    with pytest.raises(NotFound):
        store.get_task_attachment(b, tb["number"], 999)


# --- telegram subscriptions ---------------------------------------------------


def test_subscribe_project(store):
    pid = store.create_project("alpha")["id"]
    sub = store.subscribe_project(7, pid)
    assert sub["chat_id"] == 7
    assert sub["project_id"] == pid
    assert sub["project_name"] == "alpha"
    assert sub["created_at"]


def test_subscribe_project_unknown_raises(store):
    with pytest.raises(NotFound):
        store.subscribe_project(7, 999)


def test_subscribe_project_idempotent(store):
    pid = store.create_project("alpha")["id"]
    first = store.subscribe_project(7, pid)
    second = store.subscribe_project(7, pid)
    # the repeat keeps the existing row and its original created_at
    assert second == first
    assert store.list_subscriptions(7) == [
        {"project_id": pid, "project_name": "alpha", "created_at": first["created_at"]}
    ]


def test_unsubscribe_project(store):
    pid = store.create_project("alpha")["id"]
    store.subscribe_project(7, pid)
    assert store.unsubscribe_project(7, pid) == {"applied": True, "removed": True}
    # nothing left to remove
    assert store.unsubscribe_project(7, pid) == {"applied": True, "removed": False}


def test_unsubscribe_project_unknown_raises(store):
    with pytest.raises(NotFound):
        store.unsubscribe_project(7, 999)


def test_list_subscriptions_per_chat_in_name_order(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    z_sub = store.subscribe_project(7, zeta)
    a_sub = store.subscribe_project(7, alpha)
    store.subscribe_project(8, zeta)  # a different chat

    subs = store.list_subscriptions(7)
    # name order, project name joined, only this chat's rows
    assert subs == [
        {"project_id": alpha, "project_name": "alpha", "created_at": a_sub["created_at"]},
        {"project_id": zeta, "project_name": "zeta", "created_at": z_sub["created_at"]},
    ]
    assert store.list_subscriptions(8) == [
        {"project_id": zeta, "project_name": "zeta", "created_at": z_sub["created_at"]}
    ]
    assert store.list_subscriptions(99) == []


def test_subscribed_chats(store):
    zeta = store.create_project("zeta")["id"]
    alpha = store.create_project("alpha")["id"]
    store.subscribe_project(7, alpha)
    store.subscribe_project(-100, zeta)  # groups/supergroups have negative ids
    store.subscribe_project(3, alpha)
    assert store.subscribed_chats(alpha) == [3, 7]
    assert store.subscribed_chats(zeta) == [-100]
    empty = store.create_project("empty")["id"]
    assert store.subscribed_chats(empty) == []
    with pytest.raises(NotFound):
        store.subscribed_chats(999)


def test_subscription_rows_follow_project_cascade(store):
    pid = store.create_project("alpha")["id"]
    store.subscribe_project(7, pid)
    store.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    assert store.list_subscriptions(7) == []
    assert store.conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_subscriptions"
    ).fetchone()["c"] == 0


# --- state-change detection (the notification cursor) --------------------------


def test_max_state_history_id(store):
    assert store.max_state_history_id() == 0
    pid = store.create_project("alpha")["id"]
    t = store.create_task(pid, "t")
    store.move_task(pid, t["number"], "Todo", confirm=True)
    # the creation row comes first, the move row is the newest
    history = store.get_history(pid, t["number"])
    assert store.max_state_history_id() == history[-1]["id"]


def test_new_state_changes(store):
    assert store.new_state_changes(0) == []

    alpha = store.create_project("alpha")["id"]
    beta = store.create_project("beta")["id"]

    # creation is not a state change (from_state NULL): excluded
    store.create_task(alpha, "created only")
    moved = store.create_task(alpha, "moved")
    store.move_task(alpha, moved["number"], "Todo", confirm=True)
    blocked = store.create_task(alpha, "blocked")
    store.move_task(alpha, blocked["number"], "Blocked")
    archived = store.create_task(alpha, "archived")
    store.archive_task(alpha, archived["number"], confirm=True)
    store.restore_task(alpha, archived["number"], confirm=True)
    beta_task = store.create_task(beta, "beta moved")
    store.move_task(beta, beta_task["number"], "Todo", confirm=True)

    changes = store.new_state_changes(0)
    # every transition is present, in history (id) order; per-project fields
    # stay isolated
    assert [
        (c["project_name"], c["project_id"], c["number"], c["from_state"], c["to_state"])
        for c in changes
    ] == [
        ("alpha", alpha, moved["number"], "Backlog", "Todo"),
        ("alpha", alpha, blocked["number"], "Backlog", "Blocked"),
        ("alpha", alpha, archived["number"], "Backlog", "Archived"),
        ("alpha", alpha, archived["number"], "Archived", "Backlog"),
        ("beta", beta, beta_task["number"], "Backlog", "Todo"),
    ]
    # lean dict fields
    first = changes[0]
    assert set(first) == {
        "id",
        "project_id",
        "project_name",
        "number",
        "title",
        "from_state",
        "to_state",
        "changed_at",
    }
    assert first["title"] == "moved"
    # ordered by id (strictly increasing)
    assert [c["id"] for c in changes] == sorted(c["id"] for c in changes)
    # since_id filters to the changes recorded after it
    mid = changes[0]["id"]
    tail = store.new_state_changes(mid)
    assert [c["id"] for c in tail] == [c["id"] for c in changes[1:]]
    assert all(c["id"] > mid for c in tail)
    # nothing after the newest change
    assert store.new_state_changes(changes[-1]["id"]) == []
