"""Shared fixtures: a Store bound to a temporary SQLite database."""

import pytest

from yask import db
from yask.store import Store


@pytest.fixture()
def store(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield Store(conn)
    conn.close()


@pytest.fixture()
def project(store):
    return store.create_project("Demo")


def task(store, pid, title, type="Task", **kw):
    return store.create_task(pid, title, type, **kw)
