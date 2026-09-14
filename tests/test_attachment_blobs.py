"""#89 regression tests: listing/metadata paths must not materialize
attachment BLOB bytes.

`Store._batch_task_maps` (behind get_project/list_tasks) and
`Store.list_attachments` used to project `SELECT * FROM attachments` and
compute `size` as `len(row["data"])`, forcing SQLite to copy every
attachment BLOB into memory on each board load. The fix projects
`length(data) AS size` (metadata-only: SQLite gets the byte count from the
record header without touching the BLOB payload). These tests pin that:
a connection guard makes any `row["data"]` access raise, so a regression
that re-introduces a BLOB read on a listing path fails here.
"""


PAYLOAD = b"# big\n" + b"x" * (1024 * 1024)  # >= 1 MB of markdown


class _GuardRow:
    """Wraps a sqlite3.Row; raises when its BLOB ``data`` column is read.

    Forwards ``keys()`` and key- and index-based ``__getitem__`` to the
    underlying row; only a key-based read of ``data`` is guarded.
    """

    def __init__(self, row):
        self._row = row

    def keys(self):
        return self._row.keys()

    def __getitem__(self, key):
        if key == "data" and "data" in self._row.keys():
            raise AssertionError("listing path materialized the attachment BLOB")
        return self._row[key]


class _GuardCursor:
    """Forwards lastrowid/rowcount; wraps every fetched row in _GuardRow."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchall(self):
        return [_GuardRow(r) for r in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else _GuardRow(row)

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _GuardConn:
    """Connection proxy for the BLOB-read guard.

    The Store touches exactly three connection facilities — `execute`,
    `executemany`, `with conn:` — so the proxy forwards those and wraps
    every row leaving the database in _GuardRow.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return _GuardCursor(self._conn.execute(*args, **kwargs))

    def executemany(self, *args, **kwargs):
        return self._conn.executemany(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)


def test_listing_paths_never_read_blob_bytes(store, project, monkeypatch):
    """The regression pin: an upload, get_project and list_attachments
    must complete with `size == len(payload)` while any `row["data"]`
    access raises.

    If a listing path ever re-introduces `SELECT *` + `len(row["data"])`,
    the guard trips here. If this test passes while a `data` read remains
    in a listing path, it is broken — it is the direct encoding of
    "listings are O(metadata)".
    """
    pid = project["id"]
    t = store.create_task(pid, "t")

    # From here on, every row the store reads is guarded.
    monkeypatch.setattr(store, "conn", _GuardConn(store.conn), raising=False)

    meta = store.add_attachment(pid, t["number"], "big.md", "text/markdown", PAYLOAD)
    assert meta["size"] == len(PAYLOAD)

    proj = store.get_project(pid)
    task = next(x for x in proj["tasks"] if x["number"] == t["number"])
    assert [a["size"] for a in task["attachments"]] == [len(PAYLOAD)]
    assert task["attachments"][0]["filename"] == "big.md"
    assert task["attachments"][0]["content_type"] == "text/markdown"

    listed = store.list_attachments(pid, t["number"])
    assert [a["size"] for a in listed] == [len(PAYLOAD)]
    assert listed[0]["id"] == meta["id"]

    # list_tasks shares _batch_task_maps with get_project — same guarantee.
    rows = store.list_tasks(pid)
    row = next(x for x in rows if x["number"] == t["number"])
    assert [a["size"] for a in row["attachments"]] == [len(PAYLOAD)]


def test_listing_reports_exact_sizes(store, project):
    """Listing metadata reports exact byte sizes for attachments of
    different sizes, keeps id ordering, and yields [] for tasks without
    attachments (including the empty-batch IN (NULL) path)."""
    pid = project["id"]
    small = b"tiny"
    mid = b"m" * (1024 * 1024)  # ~1 MB
    large = b"L" * (2 * 1024 * 1024)  # ~2 MB

    a = store.create_task(pid, "with-attachments")
    b = store.create_task(pid, "one-attachment")
    c = store.create_task(pid, "bare")

    m1 = store.add_attachment(pid, a["number"], "small.md", "text/markdown", small)
    m2 = store.add_attachment(pid, a["number"], "mid.md", "text/markdown", mid)
    m3 = store.add_attachment(pid, a["number"], "large.md", "text/markdown", large)
    m4 = store.add_attachment(pid, b["number"], "img.png", "image/png", b"\x89PNG")

    by_number = {t["number"]: t for t in store.get_project(pid)["tasks"]}

    att_a = by_number[a["number"]]["attachments"]
    assert [(x["id"], x["size"]) for x in att_a] == [
        (m1["id"], len(small)),
        (m2["id"], len(mid)),
        (m3["id"], len(large)),
    ]
    assert [x["filename"] for x in att_a] == ["small.md", "mid.md", "large.md"]
    assert [x["content_type"] for x in att_a] == [
        "text/markdown",
        "text/markdown",
        "text/markdown",
    ]
    for x in att_a:
        assert isinstance(x["size"], int)
        assert x["created_at"]

    att_b = by_number[b["number"]]["attachments"]
    assert [(x["id"], x["size"], x["filename"]) for x in att_b] == [
        (m4["id"], 4, "img.png"),
    ]
    # A task without attachments serializes to an empty list.
    assert by_number[c["number"]]["attachments"] == []

    # An empty project exercises the degenerate IN (NULL) batch.
    empty_pid = store.create_project("empty")["id"]
    assert store.get_project(empty_pid)["tasks"] == []
    assert store.list_tasks(empty_pid) == []
