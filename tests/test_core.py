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
