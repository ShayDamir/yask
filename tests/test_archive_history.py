"""Archiving, restoration, permanent deletion, history, attachments."""

import pytest

from yask.store import ConfirmationRequired, NotFound, ValidationError


def test_any_task_can_be_archived(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    store.move_task(pid, t["number"], "In progress", confirm=True)
    res = store.archive_task(pid, t["number"], confirm=True)
    assert res["applied"]
    assert store.get_task(pid, t["number"])["state"] == "Archived"


def test_archived_hidden_by_default(store, project):
    pid = project["id"]
    a = store.create_task(pid, "visible")
    b = store.create_task(pid, "hidden")
    store.archive_task(pid, b["number"], confirm=True)
    titles = [t["title"] for t in store.list_tasks(pid)]
    assert titles == ["visible"]
    all_titles = [t["title"] for t in store.list_tasks(pid, include_archived=True)]
    assert set(all_titles) == {"visible", "hidden"}


def test_archiving_epic_archives_subtree_with_confirmation(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    e2 = store.create_task(pid, "e2", type="Epic", parent_number=e["number"])
    s = store.create_task(pid, "s", type="Story", parent_number=e["number"])
    with pytest.raises(ConfirmationRequired) as exc:
        store.archive_task(pid, e["number"])
    assert {a["number"] for a in exc.value.affected} == {
        e["number"], e2["number"], s["number"],
    }
    store.archive_task(pid, e["number"], confirm=True)
    for n in (e["number"], e2["number"], s["number"]):
        assert store.get_task(pid, n)["state"] == "Archived"


def test_restore_moves_to_backlog(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    store.move_task(pid, t["number"], "Done", confirm=True)
    store.archive_task(pid, t["number"], confirm=True)
    store.restore_task(pid, t["number"])
    assert store.get_task(pid, t["number"])["state"] == "Backlog"


def test_restore_to_specific_state(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    store.archive_task(pid, t["number"], confirm=True)
    store.restore_task(pid, t["number"], to_state="Review")
    assert store.get_task(pid, t["number"])["state"] == "Review"


def test_restore_requires_archived(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    with pytest.raises(ValidationError):
        store.restore_task(pid, t["number"])


def test_delete_permanent_single(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    store.archive_task(pid, t["number"], confirm=True)
    store.delete_task(pid, t["number"], confirm=True)
    with pytest.raises(NotFound):
        store.get_task(pid, t["number"])


def test_delete_requires_archived(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    with pytest.raises(ValidationError):
        store.delete_task(pid, t["number"], confirm=True)
    # still exists, not deleted
    assert store.get_task(pid, t["number"]) is not None
    # archiving then deleting works
    store.archive_task(pid, t["number"], confirm=True)
    store.delete_task(pid, t["number"], confirm=True)
    with pytest.raises(NotFound):
        store.get_task(pid, t["number"])


def test_delete_epic_removes_subtree_with_confirmation(store, project):
    pid = project["id"]
    e = store.create_task(pid, "e", type="Epic")
    s = store.create_task(pid, "s", type="Story", parent_number=e["number"])
    store.archive_task(pid, e["number"], confirm=True)
    with pytest.raises(ConfirmationRequired) as exc:
        store.delete_task(pid, e["number"])
    assert {a["number"] for a in exc.value.affected} == {e["number"], s["number"]}
    store.delete_task(pid, e["number"], confirm=True)
    for n in (e["number"], s["number"]):
        with pytest.raises(NotFound):
            store.get_task(pid, n)


def test_state_history_records_every_change_with_timestamp(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    store.move_task(pid, t["number"], "Todo", confirm=True)
    store.move_task(pid, t["number"], "Done", confirm=True)
    store.archive_task(pid, t["number"], confirm=True)
    hist = store.get_history(pid, t["number"])
    transitions = [(h["from_state"], h["to_state"]) for h in hist]
    assert transitions == [
        (None, "Backlog"),
        ("Backlog", "Todo"),
        ("Todo", "Done"),
        ("Done", "Archived"),
    ]
    for h in hist:
        assert h["changed_at"]  # timestamp present


def test_cascade_state_changes_each_recorded(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    p = store.create_task(pid, "p")
    store.set_prerequisites(pid, t["number"], [p["number"]])
    store.move_task(pid, t["number"], "Todo", confirm=True)
    hist_t = store.get_history(pid, t["number"])
    hist_p = store.get_history(pid, p["number"])
    assert ("Backlog", "Todo") in [(h["from_state"], h["to_state"]) for h in hist_t]
    assert ("Backlog", "Todo") in [(h["from_state"], h["to_state"]) for h in hist_p]


# -- attachments -------------------------------------------------------------

def test_attachment_roundtrip_markdown(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    meta = store.add_attachment(
        pid, t["number"], "notes.md", "text/markdown", b"# Hello\n\n- world"
    )
    got_meta, data = store.get_attachment(meta["id"])
    assert data == b"# Hello\n\n- world"
    assert got_meta["filename"] == "notes.md"
    assert got_meta["content_type"] == "text/markdown"
    assert store.get_task(pid, t["number"])["attachments"][0]["id"] == meta["id"]


def test_attachment_roundtrip_image(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    meta = store.add_attachment(pid, t["number"], "img.png", "image/png", png)
    _, data = store.get_attachment(meta["id"])
    assert data == png


def test_attachment_type_restricted(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    with pytest.raises(ValidationError):
        store.add_attachment(pid, t["number"], "x.exe", "application/octet-stream", b"x")


def test_attachment_delete(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t")
    meta = store.add_attachment(pid, t["number"], "a.md", "text/markdown", b"a")
    store.delete_attachment(meta["id"])
    with pytest.raises(NotFound):
        store.get_attachment(meta["id"])


def test_last_attachment_uses_internal_task_id(store, project):
    """last_attachment must query by internal id, not public number.

    Creates two projects so that task numbers diverge from internal ids:
    project A gets task #1 (id 1), then project B gets task #1 (id 2).
    Attaching different files to each task and calling last_attachment for
    B's task must return B's file, not A's (the old code would return A's
    because it used the public number 1 to query task_id).
    """
    from yask.store import NotFound

    pid_a = project["id"]
    t_a = store.create_task(pid_a, "task_a")
    # Create a second project — its first task has number 1 but internal id 2
    pid_b = store.create_project("proj_b")["id"]
    t_b = store.create_task(pid_b, "task_b")
    assert t_b["id"] != t_b["number"], "guard: internal id must differ from public number"

    store.add_attachment(pid_a, t_a["number"], "a_file.md", "text/markdown", b"a content")
    store.add_attachment(pid_b, t_b["number"], "b_file.md", "text/markdown", b"b content")

    meta, data = store.last_attachment(pid_b, t_b["number"])
    assert meta["filename"] == "b_file.md"
    assert data == b"b content"


def test_description_field_optional(store, project):
    pid = project["id"]
    t = store.create_task(pid, "t", description="a plain description")
    assert store.get_task(pid, t["number"])["description"] == "a plain description"
    s = store.create_task(pid, "s")
    assert store.get_task(pid, s["number"])["description"] == ""
    store.update_task(pid, t["number"], description="updated")
    assert store.get_task(pid, t["number"])["description"] == "updated"
