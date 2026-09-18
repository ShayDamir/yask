"""REST API tests via FastAPI TestClient."""

import base64
import concurrent.futures
import threading
import time

import pytest
from fastapi.testclient import TestClient

from yask.api import ATTACHMENT_CSP, ATTACHMENT_DOWNLOAD_CONCURRENCY, CSP, create_app
from yask.store import Store


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "api.db")
    # loopback base URL (task #85): the enforced app rejects non-loopback
    # Host headers, so the whole suite must hit it as a local client
    with TestClient(app, base_url="http://127.0.0.1:4304") as c:
        yield c


@pytest.fixture()
def pid(client):
    r = client.post("/api/projects", json={"name": "API Demo"})
    assert r.status_code == 201
    return r.json()["id"]


def test_projects_crud(client):
    assert client.get("/api/projects").json() == []
    r = client.post("/api/projects", json={"name": "P1"})
    assert r.status_code == 201
    assert client.get("/api/projects").json()[0]["name"] == "P1"
    # duplicate name is a conflict
    assert client.post("/api/projects", json={"name": "p1"}).status_code == 409
    # empty name is a validation error
    assert client.post("/api/projects", json={"name": "  "}).status_code == 400


def test_task_lifecycle_over_api(client, pid):
    r = client.post(f"/api/projects/{pid}/tasks", json={"title": "first", "type": "Story", "estimate": 3})
    assert r.status_code == 201
    t = r.json()
    assert t["number"] == 1
    assert t["state"] == "Backlog"

    # move: single task applies without confirmation
    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo"})
    assert r.json() == {"applied": True, "affected": [
        {"number": 1, "title": "first", "type": "Story", "from": "Backlog", "to": "Todo"}
    ]}

    # invalid state
    assert client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Nope"}).status_code == 400
    # unknown task
    assert client.post(f"/api/projects/{pid}/tasks/99/move", json={"to_state": "Todo"}).status_code == 404

    # update
    r = client.patch(f"/api/projects/{pid}/tasks/1", json={"title": "first!", "estimate": 5, "description": "desc"})
    assert r.status_code == 200
    assert r.json()["title"] == "first!"
    assert r.json()["estimate"] == 5
    assert r.json()["description"] == "desc"

    # history
    hist = client.get(f"/api/projects/{pid}/tasks/1/history").json()
    assert [h["to_state"] for h in hist] == ["Backlog", "Todo"]


def test_create_task_ignores_state_field(client, pid):
    # new tasks can only be added to the Backlog (#1); a stale client
    # sending "state" in the body is ignored and the task lands in Backlog
    r = client.post(
        f"/api/projects/{pid}/tasks", json={"title": "sneaky", "state": "Todo"}
    )
    assert r.status_code == 201
    assert r.json()["state"] == "Backlog"


# -- estimate validation (#88: NaN / ±inf must never be stored) -------------------


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_create_task_rejects_non_finite_estimate_strings(client, pid, bad):
    # pydantic v2 lax mode coerces these JSON strings to float — the store
    # must reject them, not store them
    r = client.post(f"/api/projects/{pid}/tasks", json={"title": "t", "estimate": bad})
    assert r.status_code == 400


def test_create_task_rejects_bare_nan_literal(client, pid):
    # starlette's json.loads also accepts bare NaN/Infinity literals
    r = client.post(
        f"/api/projects/{pid}/tasks",
        content='{"title": "t", "estimate": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_update_task_rejects_non_finite_estimates(client, pid, bad):
    t = client.post(f"/api/projects/{pid}/tasks", json={"title": "t", "estimate": 2})
    n = t.json()["number"]
    r = client.patch(f"/api/projects/{pid}/tasks/{n}", json={"estimate": bad})
    assert r.status_code == 400
    assert client.get(f"/api/projects/{pid}/tasks/{n}").json()["estimate"] == 2


def test_rejected_nan_does_not_poison_epic_total(client, pid):
    epic = client.post(
        f"/api/projects/{pid}/tasks", json={"title": "epic", "type": "Epic"}
    ).json()
    child = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "child", "type": "Story", "estimate": 3,
              "parent_number": epic["number"]},
    ).json()
    r = client.patch(f"/api/projects/{pid}/tasks/{child['number']}", json={"estimate": "nan"})
    assert r.status_code == 400
    assert client.get(f"/api/projects/{pid}/tasks/{epic['number']}").json()["estimate_total"] == 3


def test_move_confirmation_flow(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "main"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "prereq"})
    client.put(f"/api/projects/{pid}/tasks/1/prereqs", json={"prereq_numbers": [2]})

    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo"})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["requires_confirmation"] is True
    assert len(body["affected"]) == 2
    # nothing changed
    assert client.get(f"/api/projects/{pid}/tasks/2").json()["state"] == "Backlog"

    r = client.post(f"/api/projects/{pid}/tasks/1/move", json={"to_state": "Todo", "confirm": True})
    assert r.status_code == 200
    assert r.json()["applied"] is True
    assert client.get(f"/api/projects/{pid}/tasks/2").json()["state"] == "Todo"


def test_reorder_self_reference_noop_api(client, pid):
    """A self-referencing reorder is a clean 200 no-op, not a 500 (#51)."""
    client.post(f"/api/projects/{pid}/tasks", json={"title": "first"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "second"})
    r = client.post(f"/api/projects/{pid}/tasks/1/reorder", json={"after_number": 1})
    assert r.status_code == 200
    assert r.json()["number"] == 1
    r = client.post(f"/api/projects/{pid}/tasks/1/reorder", json={"before_number": 1})
    assert r.status_code == 200
    # column order unchanged
    order = [t["number"] for t in client.get(f"/api/projects/{pid}/tasks").json()]
    assert order == [1, 2]


def test_archive_and_delete_flow(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "e", "type": "Epic"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "s", "parent_number": 1})

    # deleting a non-archived task is refused — only archived tasks may be deleted
    r = client.delete(f"/api/projects/{pid}/tasks/1?confirm=true")
    assert r.status_code == 400
    assert client.get(f"/api/projects/{pid}/tasks/1").status_code == 200

    # archived tasks hidden by default
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={})
    assert r.status_code == 409  # subtree of 2 tasks
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={"confirm": True})
    assert r.status_code == 200
    tasks = client.get(f"/api/projects/{pid}/tasks").json()
    assert tasks == []
    tasks = client.get(f"/api/projects/{pid}/tasks?include_archived=true").json()
    assert len(tasks) == 2

    # restore single
    r = client.post(f"/api/projects/{pid}/tasks/1/restore", json={})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}/tasks/1").json()["state"] == "Backlog"

    # re-archive, then delete with confirmation (409 without, 200 with, subtree gone)
    r = client.post(f"/api/projects/{pid}/tasks/1/archive", json={"confirm": True})
    assert r.status_code == 200
    # deleting a 2-task subtree needs confirmation: 409 without, 200 with
    r = client.delete(f"/api/projects/{pid}/tasks/1")
    assert r.status_code == 409
    r = client.delete(f"/api/projects/{pid}/tasks/1?confirm=true")
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}/tasks/1").status_code == 404
    assert client.get(f"/api/projects/{pid}/tasks/2").status_code == 404  # subtree gone


def test_prereq_cycle_rejected(client, pid):
    for i in (1, 2):
        client.post(f"/api/projects/{pid}/tasks", json={"title": f"t{i}"})
    client.put(f"/api/projects/{pid}/tasks/1/prereqs", json={"prereq_numbers": [2]})
    r = client.put(f"/api/projects/{pid}/tasks/2/prereqs", json={"prereq_numbers": [1]})
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"]


def test_attachment_upload_download(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert r.status_code == 201
    att = r.json()
    got = client.get(f"/api/attachments/{att['id']}")
    assert got.status_code == 200
    assert got.content == b"# hi"
    assert got.headers["content-type"].startswith("text/markdown")

    # wrong content type rejected
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("x.exe", b"x", "application/octet-stream")},
    )
    assert r.status_code == 400

    assert client.delete(f"/api/attachments/{att['id']}").status_code == 200
    assert client.get(f"/api/attachments/{att['id']}").status_code == 404


def test_svg_attachment_served_as_download(client, pid):
    """#86: an image/svg+xml attachment (the only executable type in the
    allowlist) is served with Content-Disposition: attachment so direct
    navigation downloads it instead of rendering it; every other type
    keeps the existing inline behavior. The payload is benign — the
    script-payload regression test belongs to #84."""
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})

    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                        "image/svg+xml")},
    )
    assert r.status_code == 201
    got = client.get(f"/api/attachments/{r.json()['id']}")
    assert got.status_code == 200
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"

    # control: a non-SVG type stays inline (unchanged behavior)
    r2 = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert r2.status_code == 201
    got2 = client.get(f"/api/attachments/{r2.json()['id']}")
    assert got2.status_code == 200
    assert got2.headers["content-disposition"].startswith("inline")


def test_svg_script_payload_attachment_is_inert(client, pid):
    """#84: an image/svg+xml attachment carrying an embedded <script> is
    served inert: the bytes are returned as-is, but the response is a
    forced download with the inert CSP (sandbox; default-src 'none') and
    nosniff, so even direct navigation to the API URL cannot execute the
    script. The rule is uniform: a text/markdown attachment keeps its
    inline disposition but carries the same inert CSP."""
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})

    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<script>fetch("/api/projects",{method:"POST"})</script></svg>'
    )
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("evil.svg", payload, "image/svg+xml")},
    )
    assert r.status_code == 201
    got = client.get(f"/api/attachments/{r.json()['id']}")
    assert got.status_code == 200
    assert got.content == payload  # bytes served as-is
    assert got.headers["content-type"] == "image/svg+xml"
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"
    # the dedicated inert policy — not the UI CSP
    assert got.headers["content-security-policy"] == ATTACHMENT_CSP

    # uniform rule: text/markdown also carries the inert CSP while
    # keeping its inline disposition
    r2 = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert r2.status_code == 201
    got2 = client.get(f"/api/attachments/{r2.json()['id']}")
    assert got2.status_code == 200
    assert got2.headers["content-disposition"].startswith("inline")
    assert got2.headers["x-content-type-options"] == "nosniff"
    assert got2.headers["content-security-policy"] == ATTACHMENT_CSP


def _multipart_body(boundary, filename, data, content_type):
    """Build a raw multipart/form-data body with the exact filename bytes,
    so a CRLF inside the filename reaches the parser unescaped."""
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode()


def test_attachment_malicious_filename_no_header_injection(client, pid):
    """Bug #68: a crafted upload filename must not inject HTTP response
    headers.     The multipart filename carries a raw CRLF; the resulting
    ``Content-Disposition`` header must contain no control characters and no
    injected ``X-Attacked`` header.
    """
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    boundary = "----yaskboundary"
    body = _multipart_body(
        boundary,
        "x\r\nX-Attacked: 1",
        b"data",
        "text/markdown",
    )
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 201
    att = r.json()

    got = client.get(f"/api/attachments/{att['id']}")
    assert got.status_code == 200
    disp = got.headers.get("content-disposition", "")
    # No control characters (CRLF, tab) survive into the header value.
    assert "\r" not in disp and "\n" not in disp and "\t" not in disp
    assert "X-Attacked" not in disp
    # No injected response header leaked onto the response at all.
    header_names = {k.lower() for k in got.headers.keys()}
    assert "x-attacked" not in header_names


def test_attachment_upload_rejects_oversized_payload(client, pid):
    """Bug #72: an oversized attachment is rejected by the size check before
    the full payload is buffered in memory (DoS, CWE-770). The web handler
    caps its read at ``MAX + 1`` bytes and rejects anything larger; the
    store guard is the backend safety net.
    """
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    boundary = "----yaskoversizedboundary"
    cap = 10 * 1024 * 1024

    # control: exactly the cap is accepted.
    body_ok = _multipart_body(boundary, "ok.md", b"x" * cap, "text/markdown")
    r_ok = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_ok,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_ok.status_code == 201
    stored_after_ok = len(
        client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"]
    )

    # the boundary itself: cap + 1 byte is rejected.
    body_over = _multipart_body(boundary, "over.md", b"x" * (cap + 1), "text/markdown")
    r_over = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_over,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_over.status_code == 400
    # nothing new was stored by the rejection.
    assert (
        len(client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"])
        == stored_after_ok
    )

    # a clearly oversized payload is rejected too.
    body_big = _multipart_body(boundary, "big.md", b"x" * (cap * 2), "text/markdown")
    r_big = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_big,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_big.status_code == 400
    assert (
        len(client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"])
        == stored_after_ok
    )


# -- free-text length limits (#126) -------------------------------------------


def test_text_field_length_limits_over_api(client, pid):
    # The store is the enforcement point for spec.FIELD_LIMITS; the REST
    # surface surfaces a limit violation as a 400 validation error.
    r = client.post(f"/api/projects/{pid}/tasks", json={"title": "x" * 300})
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]

    r = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "t", "description": "x" * 65537},
    )
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]

    r = client.post("/api/projects", json={"name": "x" * 300})
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]


def test_attachment_oversized_filename_rejected_over_api(client, pid):
    # A multi-KB upload filename is rejected at ingest: the name is never
    # persisted, so it can never re-appear in a download header.
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    boundary = "----yasklongnameboundary"
    body = _multipart_body(boundary, "a" * 2048, b"# hi", "text/markdown")
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]
    assert client.get(f"/api/projects/{pid}/tasks/1").json()["attachments"] == []

    # positive control: a filename at exactly the 255-char limit is
    # accepted, and its download header stays small.
    name = "b" * 252 + ".md"  # 255 chars
    body_ok = _multipart_body(boundary, name, b"# hi", "text/markdown")
    r_ok = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        content=body_ok,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r_ok.status_code == 201
    got = client.get(f"/api/attachments/{r_ok.json()['id']}")
    assert got.status_code == 200
    disp = got.headers["content-disposition"]
    assert disp == f'inline; filename="{name}"'
    assert len(disp) < 300  # disposition + filename only, nothing inflated


def test_attachment_legacy_oversized_filename_header_bounded(client, pid, tmp_path):
    # A legacy row (stored before #126's ingest-time limit) with a multi-KB
    # filename is capped to its first 255 chars on read: the download
    # header stays bounded and never contains the full stored name.
    from yask import db

    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    task = client.get(f"/api/projects/{pid}/tasks/1").json()
    legacy_name = "b" * 10240

    # Seed the row through a second connection on the same DB file.
    conn = db.connect(tmp_path / "api.db")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO attachments(task_id, filename, content_type, data,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (task["id"], legacy_name, "text/markdown", b"legacy",
                 "2026-01-01T00:00:00Z"),
            )
        att_id = cur.lastrowid
    finally:
        conn.close()

    got = client.get(f"/api/attachments/{att_id}")
    assert got.status_code == 200
    disp = got.headers["content-disposition"]
    assert disp == f'inline; filename="{"b" * 255}"'
    assert len(disp) < 300
    assert legacy_name not in disp


def test_attachment_streamed_in_chunks(client, pid, tmp_path):
    """#96 (decision B): a multi-MB attachment is served through a
    StreamingResponse, so its body reaches the ASGI send in multiple
    ``more_body`` chunks (one per generator yield), not one buffer — while
    the observable behavior (bytes, content-type, disposition, inert CSP)
    is unchanged."""
    payload = b"z" * (3 * 1024 * 1024)
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("big.md", payload, "text/markdown")},
    )
    assert r.status_code == 201
    att_id = r.json()["id"]

    # Plain round-trip: byte-identical, headers intact.
    got = client.get(f"/api/attachments/{att_id}")
    assert got.status_code == 200
    assert got.content == payload
    assert got.headers["content-type"].startswith("text/markdown")
    assert got.headers["content-disposition"].startswith("inline")
    assert got.headers["content-security-policy"] == ATTACHMENT_CSP

    # Chunked delivery: a pass-through ASGI wrapper counts
    # ``http.response.body`` messages with ``more_body=True``. A fresh app
    # is used (a semaphore is bound to the loop that first used it, so the
    # wrapper must not share the fixture app's loop).
    inner = create_app(tmp_path / "chunks.db")
    chunks = {"n": 0}

    async def counting(scope, receive, send):
        async def counting_send(message):
            if (
                message["type"] == "http.response.body"
                and message.get("more_body", False)
            ):
                chunks["n"] += 1
            await send(message)

        await inner(scope, receive, counting_send)

    with TestClient(counting, base_url="http://127.0.0.1:4304") as c2:
        p2 = c2.post("/api/projects", json={"name": "Chunked"}).json()["id"]
        c2.post(f"/api/projects/{p2}/tasks", json={"title": "t"})
        r2 = c2.post(
            f"/api/projects/{p2}/tasks/1/attachments",
            files={"file": ("big.md", payload, "text/markdown")},
        )
        assert r2.status_code == 201
        got2 = c2.get(f"/api/attachments/{r2.json()['id']}")
        assert got2.status_code == 200
        assert got2.content == payload

    # 3 MiB payload with the 1 MiB chunk constant: the body is genuinely
    # streamed in several chunks, not one.
    assert chunks["n"] >= 2


def test_attachment_download_concurrency_capped(client, pid, monkeypatch):
    """#96 (decision B): the per-app semaphore caps concurrent in-flight
    attachment downloads. ``2 * cap`` concurrent GETs of one ~2 MiB
    attachment, with a 50 ms overlap window inside the read (which runs
    while the slot is held), must never exceed ``cap`` concurrent
    ``get_attachment`` calls — while every response stays byte-identical."""
    payload = b"y" * (2 * 1024 * 1024)
    client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    r = client.post(
        f"/api/projects/{pid}/tasks/1/attachments",
        files={"file": ("big.md", payload, "text/markdown")},
    )
    assert r.status_code == 201
    att_id = r.json()["id"]

    lock = threading.Lock()
    in_flight = 0
    peak = 0
    original = Store.get_attachment

    def spy(self, attachment_id):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            # Widen the overlap window; the sleep runs while the slot is
            # held (the slot is held across read + stream).
            time.sleep(0.05)
            return original(self, attachment_id)
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(Store, "get_attachment", spy)

    n = 2 * ATTACHMENT_DOWNLOAD_CONCURRENCY
    # One TestClient (one portal loop, the semaphore's loop) driven from a
    # thread pool: all GETs run as concurrent tasks on the same loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(
            pool.map(lambda _: client.get(f"/api/attachments/{att_id}"), range(n))
        )

    assert all(resp.status_code == 200 for resp in responses)
    assert all(resp.content == payload for resp in responses)
    # The hard guarantee: reads are capped at the concurrency limit.
    assert peak <= ATTACHMENT_DOWNLOAD_CONCURRENCY
    # Sanity: overlap actually happened (without the cap the 50 ms sleeps
    # would overlap well beyond 2).
    assert peak >= 2


def test_task_types_over_api(client):
    r = client.get("/api/task-types").json()
    assert {t["name"] for t in r} >= {"Story", "Task", "Bug", "Epic"}
    r = client.post("/api/task-types", json={"name": "Spike"})
    assert r.status_code == 201
    client.post(f"/api/projects", json={"name": "T2"})
    pid2 = client.get("/api/projects").json()[0]["id"]
    r = client.post(f"/api/projects/{pid2}/tasks", json={"title": "s", "type": "Spike"})
    assert r.status_code == 201
    assert r.json()["type"] == "Spike"


def test_epic_estimate_over_api(client, pid):
    client.post(f"/api/projects/{pid}/tasks", json={"title": "e", "type": "Epic"})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "a", "type": "Story", "estimate": 3, "parent_number": 1})
    client.post(f"/api/projects/{pid}/tasks", json={"title": "b", "type": "Bug", "estimate": 2, "parent_number": 1})
    e = client.get(f"/api/projects/{pid}/tasks/1").json()
    assert e["estimate"] is None
    assert e["estimate_total"] == 5
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["tasks"][0]["children"][0]["title"] == "a"


def test_openapi_route_table_stable(client):
    """Refactor guard (#82): the REST surface is pinned. Every
    (method, path, status code, operationId) entry of the OpenAPI route
    table must stay byte-identical when the internals of api.py change.
    FastAPI derives the operationId as ``{route name}_{path}_{method}``,
    so pinning it also guards the route ``name`` passed to ``_route``."""
    spec = client.get("/openapi.json").json()

    def status_of(op):
        # Declared responses are the success status plus 422 (request
        # validation) when there is a body; the success one is the rest.
        return next(k for k in op["responses"] if k != "422")

    table = {
        (method.upper(), path, status_of(op), op["operationId"])
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
    }
    assert table == {
        ("GET", "/api/projects", "200", "api_list_projects_api_projects_get"),
        ("POST", "/api/projects", "201", "api_create_project_api_projects_post"),
        ("GET", "/api/projects/{project_id}", "200", "api_get_project_api_projects__project_id__get"),
        ("GET", "/api/projects/{project_id}/tasks", "200", "api_list_tasks_api_projects__project_id__tasks_get"),
        ("POST", "/api/projects/{project_id}/tasks", "201", "api_create_task_api_projects__project_id__tasks_post"),
        ("GET", "/api/projects/{project_id}/tasks/{number}", "200", "api_get_task_api_projects__project_id__tasks__number__get"),
        ("PATCH", "/api/projects/{project_id}/tasks/{number}", "200", "api_update_task_api_projects__project_id__tasks__number__patch"),
        ("DELETE", "/api/projects/{project_id}/tasks/{number}", "200", "api_delete_task_api_projects__project_id__tasks__number__delete"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/move", "200", "api_move_task_api_projects__project_id__tasks__number__move_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/archive", "200", "api_archive_task_api_projects__project_id__tasks__number__archive_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/restore", "200", "api_restore_task_api_projects__project_id__tasks__number__restore_post"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/reorder", "200", "api_reorder_task_api_projects__project_id__tasks__number__reorder_post"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/prereqs", "200", "api_get_prereqs_api_projects__project_id__tasks__number__prereqs_get"),
        ("PUT", "/api/projects/{project_id}/tasks/{number}/prereqs", "200", "api_set_prereqs_api_projects__project_id__tasks__number__prereqs_put"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/history", "200", "api_history_api_projects__project_id__tasks__number__history_get"),
        ("GET", "/api/projects/{project_id}/tasks/{number}/attachments", "200", "api_list_attachments_api_projects__project_id__tasks__number__attachments_get"),
        ("POST", "/api/projects/{project_id}/tasks/{number}/attachments", "201", "api_add_attachment_api_projects__project_id__tasks__number__attachments_post"),
        ("PUT", "/api/projects/{project_id}/tasks/{number}/labels", "200", "api_set_task_labels_api_projects__project_id__tasks__number__labels_put"),
        ("GET", "/api/attachments/{attachment_id}", "200", "api_get_attachment_api_attachments__attachment_id__get"),
        ("DELETE", "/api/attachments/{attachment_id}", "200", "api_delete_attachment_api_attachments__attachment_id__delete"),
        ("GET", "/api/projects/{project_id}/labels", "200", "api_list_labels_api_projects__project_id__labels_get"),
        ("POST", "/api/projects/{project_id}/labels", "201", "api_create_label_api_projects__project_id__labels_post"),
        ("PUT", "/api/projects/{project_id}/labels/{label_id}", "200", "api_update_label_api_projects__project_id__labels__label_id__put"),
        ("DELETE", "/api/projects/{project_id}/labels/{label_id}", "200", "api_delete_label_api_projects__project_id__labels__label_id__delete"),
        ("GET", "/api/projects/{project_id}/roles", "200", "api_list_roles_api_projects__project_id__roles_get"),
        ("PUT", "/api/projects/{project_id}/roles", "200", "api_set_roles_api_projects__project_id__roles_put"),
        ("DELETE", "/api/projects/{project_id}/roles/{name}", "200", "api_delete_role_api_projects__project_id__roles__name__delete"),
        ("GET", "/api/telegram-users", "200", "api_list_telegram_users_api_telegram_users_get"),
        ("POST", "/api/telegram-users", "201", "api_add_telegram_user_api_telegram_users_post"),
        ("PUT", "/api/telegram-users/{chat_id}", "200", "api_set_telegram_user_password_api_telegram_users__chat_id__put"),
        ("DELETE", "/api/telegram-users/{chat_id}", "200", "api_remove_telegram_user_api_telegram_users__chat_id__delete"),
        ("GET", "/api/telegram-users/{chat_id}/projects", "200", "api_list_telegram_user_projects_api_telegram_users__chat_id__projects_get"),
        ("PUT", "/api/telegram-users/{chat_id}/projects", "200", "api_set_telegram_user_projects_api_telegram_users__chat_id__projects_put"),
        ("GET", "/api/task-types", "200", "api_list_types_api_task_types_get"),
        ("POST", "/api/task-types", "201", "api_create_type_api_task_types_post"),
        ("PATCH", "/api/task-types/{type_id}", "200", "api_rename_type_api_task_types__type_id__patch"),
        ("DELETE", "/api/task-types/{type_id}", "200", "api_delete_type_api_task_types__type_id__delete"),
    }


def test_web_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "yask" in r.text.lower()
    r = client.get("/static/js/main.js")
    assert r.status_code == 200
    r = client.get("/static/style.css")
    assert r.status_code == 200


def test_security_headers_on_web_responses(client):
    """#86: every web response carries the four security headers, and the
    CSP matches the module-level policy constant. #98: style-src is fully
    strict — 'unsafe-inline' must not creep back in."""
    for path in ("/", "/static/js/main.js", "/static/style.css"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == CSP
        assert "'unsafe-inline'" not in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_security_headers_on_api_and_error_responses(client, pid):
    """#86: the middleware covers JSON API responses and
    exception-handler responses (404) alike."""
    ok = client.get("/api/projects")
    assert ok.status_code == 200
    not_found = client.get("/api/projects/9999")
    assert not_found.status_code == 404
    for r in (ok, not_found):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_api_responses_carry_cache_control_no_store(client, pid):
    """#132: /api/* carries sensitive data (board state, attachment
    metadata, Telegram allowlist) — no-store keeps it out of browser
    and shared-proxy HTTP caches. Static UI assets stay cacheable."""
    ok = client.get(f"/api/projects/{pid}")          # GET, 200
    created = client.post("/api/projects", json={"name": "CC"})  # POST, 201
    tg = client.get("/api/telegram-users")           # allowlist, 200
    assert (ok.status_code, created.status_code, tg.status_code) == (200, 201, 200)
    for r in (ok, created, tg):
        assert r.headers["cache-control"] == "no-store"

    # streaming response: the attachment download must carry it too
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "t"})
    att = client.post(
        f"/api/projects/{pid}/tasks/{task.json()['number']}/attachments",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert att.status_code == 201
    got = client.get(f"/api/attachments/{att.json()['id']}")
    assert got.status_code == 200
    assert got.headers["cache-control"] == "no-store"

    # boundary: the static UI shell is not part of /api/* and stays
    # cacheable (task #132 scopes the fix to /api/*)
    for path in ("/", "/static/js/main.js"):
        assert "cache-control" not in client.get(path).headers


def test_project_isolation_over_api(client):
    a = client.post("/api/projects", json={"name": "A"}).json()["id"]
    b = client.post("/api/projects", json={"name": "B"}).json()["id"]
    client.post(f"/api/projects/{a}/tasks", json={"title": "in a"})
    assert client.get(f"/api/projects/{b}/tasks").json() == []
