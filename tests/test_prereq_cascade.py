"""Prerequisite cascade: moving a task pulls along laggard prerequisites.

This is the subtle part of the spec; the README's exact worked example is
tested in test_readme_example.
"""

import pytest

from yask.store import ConfirmationRequired, CycleError, ValidationError


def _mk(store, pid, title, state, **kw):
    t = store.create_task(pid, title, **kw)
    if state != "Backlog":
        store.move_task(pid, t["number"], state, confirm=True)
    return store.get_task(pid, t["number"])


def test_readme_example(store, project):
    """Task in Backlog, prereqs in Planning/Backlog/Review. Move to Todo:
    only the Backlog prereq moves along; the others stay put."""
    pid = project["id"]
    t = store.create_task(pid, "task", type="Task")
    p_plan = _mk(store, pid, "prereq-planning", "Planning")
    p_back = store.create_task(pid, "prereq-backlog")  # stays Backlog
    p_rev = _mk(store, pid, "prereq-review", "Review")
    store.set_prerequisites(
        pid, t["number"],
        [p_plan["number"], p_back["number"], p_rev["number"]],
    )

    # multiple tasks affected -> confirmation required
    with pytest.raises(ConfirmationRequired) as exc:
        store.move_task(pid, t["number"], "Todo")
    affected = {a["number"] for a in exc.value.affected}
    assert affected == {t["number"], p_back["number"]}

    res = store.move_task(pid, t["number"], "Todo", confirm=True)
    assert res["applied"] is True
    by_num = {
        n: store.get_task(pid, n)["state"]
        for n in (t["number"], p_plan["number"], p_back["number"], p_rev["number"])
    }
    assert by_num[t["number"]] == "Todo"
    assert by_num[p_back["number"]] == "Todo"  # pulled along
    assert by_num[p_plan["number"]] == "Planning"  # already past Todo
    assert by_num[p_rev["number"]] == "Review"  # already past Todo


def test_move_without_prereqs_needs_no_confirmation(store, project):
    pid = project["id"]
    t = store.create_task(pid, "solo")
    res = store.move_task(pid, t["number"], "Todo")  # no confirm arg
    assert res["applied"] is True
    assert store.get_task(pid, t["number"])["state"] == "Todo"


def test_prereq_earlier_than_target_is_moved(store, project):
    pid = project["id"]
    t = _mk(store, pid, "task", "Todo")
    p = store.create_task(pid, "prereq")  # Backlog, earlier than Planning
    store.set_prerequisites(pid, t["number"], [p["number"]])
    res = store.move_task(pid, t["number"], "Planning", confirm=True)
    assert {a["number"] for a in res["affected"]} == {t["number"], p["number"]}
    assert store.get_task(pid, p["number"])["state"] == "Planning"


def test_prereq_at_target_stage_stays(store, project):
    pid = project["id"]
    t = store.create_task(pid, "task")  # Backlog
    p = _mk(store, pid, "prereq", "Planning")  # already at the target stage
    store.set_prerequisites(pid, t["number"], [p["number"]])
    res = store.move_task(pid, t["number"], "Planning")
    # only the task itself is affected
    assert [a["number"] for a in res["affected"]] == [t["number"]]
    assert store.get_task(pid, p["number"])["state"] == "Planning"


def test_backward_move_pulls_nothing(store, project):
    pid = project["id"]
    t = _mk(store, pid, "task", "Planning")
    p = _mk(store, pid, "prereq", "Todo")
    store.set_prerequisites(pid, t["number"], [p["number"]])
    res = store.move_task(pid, t["number"], "Backlog")
    assert [a["number"] for a in res["affected"]] == [t["number"]]
    assert store.get_task(pid, p["number"])["state"] == "Todo"


def test_cascade_is_transitive(store, project):
    pid = project["id"]
    t = store.create_task(pid, "top")
    mid = store.create_task(pid, "mid")
    leaf = store.create_task(pid, "leaf")
    store.set_prerequisites(pid, t["number"], [mid["number"]])
    store.set_prerequisites(pid, mid["number"], [leaf["number"]])
    res = store.move_task(pid, t["number"], "Todo", confirm=True)
    moved = {a["number"] for a in res["affected"]}
    assert moved == {t["number"], mid["number"], leaf["number"]}
    for n in moved:
        assert store.get_task(pid, n)["state"] == "Todo"


def test_archived_prereq_is_left_alone(store, project):
    pid = project["id"]
    t = store.create_task(pid, "task")
    p = store.create_task(pid, "prereq")
    store.set_prerequisites(pid, t["number"], [p["number"]])
    store.archive_task(pid, p["number"], confirm=True)
    res = store.move_task(pid, t["number"], "Todo", confirm=True)
    assert [a["number"] for a in res["affected"]] == [t["number"]]
    assert store.get_task(pid, p["number"])["state"] == "Archived"


def test_prereq_cannot_be_itself(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    with pytest.raises(ValidationError):
        store.set_prerequisites(pid, t["number"], [t["number"]])


def test_prereq_cannot_create_cycle(store, project):
    pid = project["id"]
    a = store.create_task(pid, "a")
    b = store.create_task(pid, "b")
    store.set_prerequisites(pid, a["number"], [b["number"]])
    with pytest.raises(CycleError):
        store.set_prerequisites(pid, b["number"], [a["number"]])


def test_prereq_must_exist_in_project(store, project):
    pid = project["id"]
    other = store.create_project("Other")["id"]
    foreign = store.create_task(other, "foreign")
    t = store.create_task(pid, "t")
    with pytest.raises(Exception):
        store.set_prerequisites(pid, t["number"], [foreign["number"]])
