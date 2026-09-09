"""SQLite schema and connection management for yask.

The whole world lives in a single SQLite file. All domain state — projects,
tasks, their types, prerequisites, state history and attachments — is stored
here. Attachments are kept as BLOBs so the database is fully self-contained.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    next_task_number INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_types (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    is_epic  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    type_id     INTEGER NOT NULL REFERENCES task_types(id),
    state       TEXT NOT NULL,
    estimate    REAL,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    sort_order  REAL NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (project_id, number)
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_state
    ON tasks(project_id, state, sort_order);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_prereqs (
    task_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    prereq_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, prereq_id)
);
CREATE INDEX IF NOT EXISTS idx_prereqs_prereq ON task_prereqs(prereq_id);

CREATE TABLE IF NOT EXISTS state_history (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_task ON state_history(task_id, changed_at);

CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data         BLOB NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);

CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_labels_project ON labels(project_id);

CREATE TABLE IF NOT EXISTS task_labels (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, label_id)
);
CREATE INDEX IF NOT EXISTS idx_task_labels_label ON task_labels(label_id);

CREATE TABLE IF NOT EXISTS project_roles (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_project_roles_project ON project_roles(project_id);
"""

# Seed task types. Epic is the single compound type.
SEED_TASK_TYPES = [
    ("Story", 0),
    ("Task", 0),
    ("Bug", 0),
    ("Epic", 1),
]

# Workflow order. A lower rank is an earlier stage. Archived is a holding
# state and deliberately has no rank: it is not part of the forward workflow
# used by prerequisite cascading.
WORKFLOW_STATES = [
    "Backlog",
    "Todo",
    "Planning",
    "In progress",
    "Review",
    "Done",
]
ARCHIVED_STATE = "Archived"
ALL_STATES = WORKFLOW_STATES + [ARCHIVED_STATE]
STATE_RANK = {s: i for i, s in enumerate(WORKFLOW_STATES)}


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection, enabling WAL, foreign keys and row access by name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    _seed_task_types(conn)
    return conn


def _seed_task_types(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("SELECT name FROM task_types")}
    with conn:
        for name, is_epic in SEED_TASK_TYPES:
            if name not in existing:
                conn.execute(
                    "INSERT INTO task_types(name, is_epic) VALUES (?, ?)",
                    (name, is_epic),
                )


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
