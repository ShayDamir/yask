"""Epics: tree structure, containment rules, summed estimates."""

import pytest

from yask.store import CycleError, ValidationError


def test_only_epic_can_contain_tasks(store, project):
    pid = project["id"]
    story = store.create_task(pid, "s", type="Story")
    with pytest.raises(ValidationError):
        store.create_task(pid, "child", type="Task", parent_number=story["number"])


def test_epic_contains_tasks_and_subepics(store, project):
    pid = project["id"]
    e = store.create_task(pid, "epic", type="Epic")
    e2 = store.create_task(pid, "sub-epic", type="Epic", parent_number=e["number"])
    store.create_task(pid, "s1", type="Story", estimate=3, parent_number=e["number"])
    store.create_task(pid, "s2", type="Task", estimate=5, parent_number=e2["number"])
    assert store.get_task(pid, e["number"])["estimate_total"] == 8


def test_epic_estimate_is_sum_of_contained(store, project):
    pid = project["id"]
    e = store.create_task(pid, "epic", type="Epic")
    e2 = store.create_task(pid, "sub", type="Epic", parent_number=e["number"])
    store.create_task(pid, "a", type="Story", estimate=3, parent_number=e["number"])
    store.create_task(pid, "b", type="Task", estimate=5, parent_number=e["number"])
    store.create_task(pid, "c", type="Bug", estimate=2, parent_number=e2["number"])
    e = store.get_task(pid, e["number"])
    # 3 + 5 + 2 (transitive, including the sub-epic's child)
    assert e["estimate_total"] == 10
    assert e["estimate"] is None
    sub = store.get_task(pid, 2)
    assert sub["estimate_total"] == 2


def test_regular_task_estimate_is_its_own(store, project):
    pid = project["id"]
    s = store.create_task(pid, "s", type="Story", estimate=8)
    assert s["estimate"] == 8
    assert s["estimate_total"] == 8


def test_cannot_estimate_an_epic(store, project):
    pid = project["id"]
    with pytest.raises(ValidationError):
        store.create_task(pid, "e", type="Epic", estimate=5)


def test_epic_tree_has_no_cycles(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    e2 = store.create_task(pid, "e2", type="Epic", parent_number=e["number"])
    with pytest.raises(CycleError):
        # would make e contain e2 and e2 contain e
        store.update_task(pid, e["number"], parent_number=e2["number"])


def test_task_cannot_be_its_own_parent(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    with pytest.raises(CycleError):
        store.update_task(pid, e["number"], parent_number=e["number"])


def test_detach_from_epic(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    s = store.create_task(pid, "s", type="Story", estimate=3, parent_number=e["number"])
    store.update_task(pid, s["number"], parent_number=None)
    assert store.get_task(pid, s["number"])["parent_number"] is None
    assert store.get_task(pid, e["number"])["estimate_total"] is None


def test_cannot_demote_epic_with_children(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    store.create_task(pid, "s", type="Story", parent_number=e["number"])
    with pytest.raises(ValidationError):
        store.update_task(pid, e["number"], type="Task")


def test_epic_children_nest_in_project_view(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    store.create_task(pid, "s", type="Story", parent_number=e["number"])
    proj = store.get_project(pid)
    roots = {t["title"]: t for t in proj["tasks"]}
    assert "s" not in roots
    assert roots["e"]["children"][0]["title"] == "s"
