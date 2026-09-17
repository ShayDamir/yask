"""yask domain logic.

Everything the product does — projects, numbered tasks, epics, prerequisite
cascades, archiving, ordering, history, attachments — is implemented here,
independent of the web or MCP interface. Both frontends call into this module.

Notable domain rules (see README.md):

* Task numbers start at 1 per project and only ever increase.
* Epics form a cycle-free tree; only an Epic can contain other tasks.
* Epics have no estimate of their own; their estimate is the sum of the
  estimates of all (transitively) contained regular tasks.
* Moving a task forward in the workflow pulls along every (transitive)
  prerequisite that has not reached the target stage yet.
* Any action that would change the state of more than one task requires an
  explicit confirmation.
* Every state change is recorded in state_history with a timestamp.
* Archiving an Epic archives its whole subtree; restoring only ever affects
  the single named task.
* Deleting an Epic permanently removes its whole subtree.
* Only archived tasks can be permanently deleted.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
from typing import Any

from . import db, spec

_UNSET = object()

# Single-sourced in spec.COLOR_HEX_RE (shared with the web UI via codegen).
_HEX_RE = re.compile(spec.COLOR_HEX_RE)


# -- password hashing (the Telegram bot's user allowlist) ----------------------
#
# Only the stdlib is used: scrypt with a per-user 16-byte salt. The stored
# form carries its own parameters so verification never depends on ambient
# defaults: ``scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>``.


SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
# OpenSSL's implicit scrypt ceiling is 32 MB, which n=2**15, r=8 sits right
# at — pass an explicit headroom so hashing works on any build.
SCRYPT_MAXMEM = 64 * 1024 * 1024

# Telegram `/login` throttle (task #69). A chat that fails to authenticate is
# temporarily locked out after `MAX_ATTEMPTS` consecutive failures, so the
# `/login` replay loop the bot runs on every message can no longer brute-force
# a weak password an unlimited number of times. The lockout is enforced
# *before* scrypt is computed, so a locked-out attempt costs the server no hash.
# Keyed by chat id — Telegram messages arrive via Meta's servers, so IP-based
# throttling is unavailable and a single chat is the only practical key.
TELEGRAM_LOGIN_MAX_ATTEMPTS = 5
TELEGRAM_LOGIN_LOCK_SECONDS = 15 * 60


def _hash_password(password: str) -> str:
    """Salted scrypt hash of ``password`` (``scrypt$n$r$p$salt$hash``)."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _is_locked(row) -> bool:
    """Whether a ``telegram_users`` row is within a lockout window.

    ``locked_until`` is an ISO-8601 UTC string; because those compare
    chronologically lexicographically, a value greater than "now" means the
    lock is still in force. A NULL / absent ``locked_until`` is not locked.
    """
    locked_until = row["locked_until"]
    return locked_until is not None and locked_until > db.utcnow()


def _lock_expiry() -> str:
    """ISO-8601 UTC timestamp ``TELEGRAM_LOGIN_LOCK_SECONDS`` from now."""
    from datetime import datetime, timezone, timedelta

    expiry = datetime.now(timezone.utc) + timedelta(
        seconds=TELEGRAM_LOGIN_LOCK_SECONDS
    )
    return expiry.strftime("%Y-%m-%dT%H:%M:%SZ")


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash string.

    A malformed ``stored`` value (wrong shape, bad hex) is simply a mismatch.
    """
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


class YaskError(Exception):
    """Base class for domain errors."""

    status = 400


class NotFound(YaskError):
    status = 404


class Conflict(YaskError):
    """Request cannot be applied as-is (e.g. name already taken)."""

    status = 409


class ValidationError(YaskError):
    status = 400


class ConfirmationRequired(Conflict):
    """The action would change state for multiple tasks; re-send with confirm."""

    def __init__(self, affected: list[dict]):
        self.affected = affected
        super().__init__("confirmation required")


class CycleError(ValidationError):
    def __init__(self, message: str):
        self.cycle = message
        super().__init__(message)


def _normalize_color(raw: str | None) -> str:
    """Normalize a label color to a canonical ``#RRGGBB`` uppercase hex.

    ``""``, ``None`` and blank input mean "no color" and normalize to ``""``.
    ``#RGB`` is expanded to ``#RRGGBB``. Anything else is rejected.
    """
    if raw is None:
        return ""
    value = raw.strip()
    if not value:
        return ""
    m = _HEX_RE.match(value)
    if m is None:
        raise ValidationError(f"invalid color '{raw}': expected #RRGGBB or #RGB")
    hexval = m.group(1)
    if len(hexval) == 3:
        hexval = "".join(c * 2 for c in hexval)
    return f"#{hexval}".upper()


def _in_clause(seq) -> tuple[str, list]:
    """Placeholder text and params for a variable-length ``IN`` clause.

    ``seq`` may be any iterable of query parameters. Empty input yields
    ``"NULL"`` so the clause reads ``col IN (NULL)``, which matches nothing,
    instead of the malformed ``col IN ()``.
    """
    seq = list(seq)
    return (",".join("?" * len(seq)) if seq else "NULL"), seq


def _validate_estimate(estimate: float | None, is_epic: bool) -> None:
    """Shared estimate guards for create/update.

    ``None`` means "not provided" and always passes. Otherwise the estimate
    must be a finite, non-negative number — and an epic may not have one at
    all (its estimate is the sum of contained tasks).
    """
    if estimate is None:
        return
    if not math.isfinite(estimate):
        raise ValidationError("estimate must be a finite number")
    if estimate < 0:
        raise ValidationError("estimate must not be negative")
    if is_epic:
        raise ValidationError(
            "epics are not estimated; their estimate is the sum of contained tasks"
        )


class Store:
    def __init__(self, conn: sqlite3.Connection, source: str = "web"):
        if source not in ("web", "mcp", "telegram"):
            raise ValidationError(
                f"invalid source '{source}': expected 'web', 'mcp' or 'telegram'"
            )
        self.conn = conn
        self.source = source

    # -- low-level helpers ------------------------------------------------------

    def _now(self) -> str:
        return db.utcnow()

    def _get_project(self, project_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"project {project_id} not found")
        return row

    def _get_task(self, project_id: int, number: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND number = ?",
            (project_id, number),
        ).fetchone()
        if row is None:
            raise NotFound(f"task #{number} not found")
        return row

    def _get_type(self, type_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM task_types WHERE id = ?", (type_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"unknown task type id {type_id}")
        return row

    def _type_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM task_types WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()

    def _is_epic_row(self, row: sqlite3.Row) -> bool:
        return bool(self._get_type(row["type_id"])["is_epic"])

    def _replace_links(self, table: str, key_col: str, key, item_col: str, values: list) -> None:
        """Replace every link row owned by `key` in one transaction.

        The DELETE and INSERT run in a single `with self.conn:` block, so
        the link set is swapped atomically: either all old rows are gone
        and all new rows present, or (on error) nothing changed. All
        validation must happen in the caller before this runs, so a
        rejected call leaves the previous links untouched.
        """
        with self.conn:
            self.conn.execute(
                f"DELETE FROM {table} WHERE {key_col} = ?", (key,)
            )
            self.conn.executemany(
                f"INSERT INTO {table}({key_col}, {item_col}) VALUES (?, ?)",
                [(key, v) for v in values],
            )

    def _log_state(
        self, task_id: int, from_state: str | None, to_state: str, now: str,
        source: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO state_history(task_id, from_state, to_state, changed_at, source)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, from_state, to_state, now, self.source if source is None else source),
        )

    # -- projects ------------------------------------------------------------

    def list_projects(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count "
            "FROM projects p ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_project(self, name_or_id) -> dict:
        """Resolve a project by name (case-insensitive) or numeric id.

        Accepts a project name (case-insensitive), a numeric id, or a numeric
        string; a numeric value is tried as an id first and falls back to a
        case-insensitive name match, so a project named "123" still resolves
        when no id 123 exists. Raises NotFound when the reference does not
        resolve, ValidationError for a boolean.
        """
        if isinstance(name_or_id, bool):
            raise ValidationError(
                f"invalid project '{name_or_id}': expected a name or id"
            )
        if isinstance(name_or_id, int):
            return dict(self._get_project(name_or_id))
        text = str(name_or_id).strip()
        if text.isdigit():
            try:
                return dict(self._get_project(int(text)))
            except NotFound:
                pass  # not a numeric id; fall through to case-insensitive name match
        key = text.lower()
        for p in self.list_projects():
            if p["name"].lower() == key:
                return p
        raise NotFound(f"project '{name_or_id}' not found")

    def list_project_overviews(self) -> list[dict]:
        """Per-project task-count overview (the bot's ``/projects`` view).

        One entry per project, in the same name order as ``list_projects``:
        ``{"id", "name", "states"}`` where ``states`` maps state → count and
        contains only non-zero counts, in canonical order (workflow order,
        then ``Blocked``). Archived tasks are excluded — the overview
        mirrors the visible board.
        """
        states = [s for s in db.ALL_STATES if s != db.ARCHIVED_STATE]
        placeholders, state_params = _in_clause(states)
        counts = {
            (r["project_id"], r["state"]): r["n"]
            for r in self.conn.execute(
                "SELECT project_id, state, COUNT(*) AS n FROM tasks "
                f"WHERE state IN ({placeholders}) GROUP BY project_id, state",
                state_params,
            )
        }
        out = []
        for p in self.conn.execute(
            "SELECT id, name FROM projects ORDER BY name COLLATE NOCASE"
        ):
            entry_states: dict[str, int] = {}
            for s in states:
                n = counts.get((p["id"], s), 0)
                if n:
                    entry_states[s] = n
            out.append({"id": p["id"], "name": p["name"], "states": entry_states})
        return out

    def list_in_progress(self, project_id: int) -> list[dict]:
        """The project's tasks in the active pipeline states (the bot's ``/tasks`` view).

        Returns lean ``{"number", "title", "state"}`` dicts for every task whose
        state is in :data:`db.IN_PROGRESS_STATES` (``Todo``, ``Planning``,
        ``In progress``, ``Review``) — Backlog, Done, Blocked and Archived
        tasks are excluded. Ordered by workflow state, and within a state by
        column order (``sort_order``). Raises ``NotFound`` for an unknown
        project id.
        """
        self._get_project(project_id)
        placeholders, state_params = _in_clause(db.IN_PROGRESS_STATES)
        rows = self.conn.execute(
            "SELECT number, title, state, sort_order, id FROM tasks "
            f"WHERE project_id = ? AND state IN ({placeholders})",
            [project_id, *state_params],
        ).fetchall()
        rows = sorted(
            rows, key=lambda r: (db.STATE_RANK[r["state"]], r["sort_order"], r["id"])
        )
        return [
            {"number": r["number"], "title": r["title"], "state": r["state"]}
            for r in rows
        ]

    def list_backlog(self, project_id: int) -> list[dict]:
        """The project's tasks in the Backlog state (the bot's ``/backlog`` view).

        Returns lean ``{"number", "title", "state"}`` dicts for every task
        whose state is ``Backlog``, in column order (``sort_order``). The
        ``/backlog`` counterpart of :meth:`list_in_progress` (the active
        states); Done, Blocked and Archived tasks are excluded. Raises
        ``NotFound`` for an unknown project id.
        """
        self._get_project(project_id)
        rows = self.conn.execute(
            "SELECT number, title, state, sort_order, id FROM tasks "
            "WHERE project_id = ? AND state = ?",
            (project_id, db.DEFAULT_STATE),
        ).fetchall()
        rows = sorted(rows, key=lambda r: (r["sort_order"], r["id"]))
        return [
            {"number": r["number"], "title": r["title"], "state": r["state"]}
            for r in rows
        ]

    def list_blocked(self, project_id: int) -> list[dict]:
        """The project's tasks in the Blocked state (the bot's ``/blocked`` view).

        Returns lean ``{"number", "title", "state"}`` dicts for every task
        whose state is ``Blocked``, in column order (``sort_order``). The
        ``/blocked`` counterpart of :meth:`list_backlog`; every other state
        (Backlog, Todo, Planning, In progress, Review, Done, Archived) is
        excluded. Raises ``NotFound`` for an unknown project id.
        """
        self._get_project(project_id)
        rows = self.conn.execute(
            "SELECT number, title, state, sort_order, id FROM tasks "
            "WHERE project_id = ? AND state = ?",
            (project_id, db.BLOCKED_STATE),
        ).fetchall()
        rows = sorted(rows, key=lambda r: (r["sort_order"], r["id"]))
        return [
            {"number": r["number"], "title": r["title"], "state": r["state"]}
            for r in rows
        ]

    def find_tasks_by_title(self, project_id: int, title: str) -> list[dict]:
        """Exact, case-insensitive task-title lookup (the bot's ``/task`` view).

        Returns lean ``{"number", "title", "state"}`` dicts for every visible
        task of ``project_id`` whose title matches ``title`` (case-insensitive),
        in number order. Archived tasks are excluded — a title search only
        reaches the visible board; an explicit number lookup still finds
        archived tasks. Raises ``NotFound`` for an unknown project id.
        """
        self._get_project(project_id)
        rows = self.conn.execute(
            "SELECT number, title, state FROM tasks "
            "WHERE project_id = ? AND state != ? AND title = ? COLLATE NOCASE "
            "ORDER BY number",
            (project_id, db.ARCHIVED_STATE, title),
        ).fetchall()
        return [
            {"number": r["number"], "title": r["title"], "state": r["state"]}
            for r in rows
        ]

    def create_project(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("project name must not be empty")
        if self.conn.execute(
            "SELECT 1 FROM projects WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone():
            raise Conflict(f"project '{name}' already exists")
        now = self._now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO projects(name, next_task_number, created_at) VALUES (?, 1, ?)",
                (name, now),
            )
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)

    # -- task types ------------------------------------------------------------

    def list_task_types(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM tasks x WHERE x.type_id = t.id) AS usage "
            "FROM task_types t ORDER BY t.is_epic DESC, t.name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_task_type(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("type name must not be empty")
        if self._type_by_name(name):
            raise Conflict(f"task type '{name}' already exists")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO task_types(name, is_epic) VALUES (?, 0)", (name,)
            )
        row = self.conn.execute(
            "SELECT * FROM task_types WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)

    def rename_task_type(self, type_id: int, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("type name must not be empty")
        t = self._get_type(type_id)
        other = self._type_by_name(name)
        if other and other["id"] != type_id:
            raise Conflict(f"task type '{name}' already exists")
        with self.conn:
            self.conn.execute(
                "UPDATE task_types SET name = ? WHERE id = ?", (name, type_id)
            )
        row = self.conn.execute(
            "SELECT * FROM task_types WHERE id = ?", (type_id,)
        ).fetchone()
        return dict(row)

    def delete_task_type(self, type_id: int) -> None:
        t = self._get_type(type_id)
        if t["is_epic"]:
            raise ValidationError("the built-in Epic type cannot be deleted")
        usage = self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE type_id = ?", (type_id,)
        ).fetchone()["c"]
        if usage:
            raise Conflict(f"task type '{t['name']}' is used by {usage} task(s)")
        with self.conn:
            self.conn.execute("DELETE FROM task_types WHERE id = ?", (type_id,))

    # -- task creation / reading ----------------------------------------------

    def next_sort_order(self, project_id: int, state: str, parent_id: int | None) -> float:
        return float(len(self._scope_task_ids(project_id, state, parent_id))) + 1.0

    def _scope_task_ids(
        self, project_id: int, state: str, parent_id: int | None
    ) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND state = ? AND parent_id IS ? "
            "ORDER BY sort_order, id",
            (project_id, state, parent_id),
        ).fetchall()
        return [r["id"] for r in rows]

    def _position_index(
        self,
        project_id: int,
        state: str,
        parent_id: int | None,
        before_number: int | None,
        after_number: int | None,
        exclude_ids: set[int] | None = None,
    ) -> int:
        """Insertion index within the scope for a before/after/end placement."""
        if (before_number is None) == (after_number is None) and before_number and after_number:
            raise ValidationError("use either before or after, not both")
        ids = [
            i
            for i in self._scope_task_ids(project_id, state, parent_id)
            if not exclude_ids or i not in exclude_ids
        ]
        if before_number is None and after_number is None:
            return len(ids)
        ref = self._get_task(project_id, before_number or after_number)
        if ref["state"] != state or ref["parent_id"] != parent_id:
            raise ValidationError(
                f"task #{ref['number']} is not in the target position scope"
            )
        if ref["id"] not in ids:
            # The ref is in scope but excluded: it is one of the tasks moved
            # in this action and cannot be its own position reference.
            raise ValidationError(
                f"task #{ref['number']} is moved in this action and cannot be "
                "used as a position reference"
            )
        idx = ids.index(ref["id"])
        return idx if before_number is not None else idx + 1

    def _apply_scope_order(
        self, project_id: int, state: str, parent_id: int | None, ordered_ids: list[int]
    ) -> None:
        for i, tid in enumerate(ordered_ids, start=1):
            self.conn.execute(
                "UPDATE tasks SET sort_order = ? WHERE id = ?", (float(i), tid)
            )

    def _renumber_scope(self, project_id: int, state: str, parent_id: int | None) -> None:
        self._apply_scope_order(
            project_id,
            state,
            parent_id,
            self._scope_task_ids(project_id, state, parent_id),
        )

    def create_task(
        self,
        project_id: int,
        title: str,
        type: str = "Task",
        estimate: float | None = None,
        parent_number: int | None = None,
        description: str = "",
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        # New tasks always start in the default state (Backlog); moving
        # them forward is a separate, tracked action (see #1).
        state = db.DEFAULT_STATE
        self._get_project(project_id)
        title = (title or "").strip()
        if not title:
            raise ValidationError("task title must not be empty")
        ttype = self._type_by_name(type)
        if ttype is None:
            raise ValidationError(f"unknown task type '{type}'")
        _validate_estimate(estimate, ttype["is_epic"])

        parent_id = None
        if parent_number is not None:
            parent = self._get_task(project_id, parent_number)
            if not self._is_epic_row(parent):
                raise ValidationError("only an epic can contain other tasks")
            parent_id = parent["id"]

        now = self._now()
        with self.conn:
            proj = self.conn.execute(
                "SELECT next_task_number FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            number = proj["next_task_number"]
            self.conn.execute(
                "UPDATE projects SET next_task_number = next_task_number + 1 "
                "WHERE id = ?",
                (project_id,),
            )
            cur = self.conn.execute(
                "INSERT INTO tasks(project_id, number, title, description, type_id,"
                " state, estimate, parent_id, sort_order, created_at, updated_at,"
                " created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    project_id, number, title, description or "", ttype["id"],
                    state, None if ttype["is_epic"] else estimate, parent_id,
                    now, now, self.source,
                ),
            )
            self._log_state(cur.lastrowid, None, state, now, self.source)
            # place the new task within its column scope
            idx = self._position_index(
                project_id, state, parent_id, before_number, after_number
            )
            ids = self._scope_task_ids(project_id, state, parent_id)
            ids.insert(idx, cur.lastrowid)
            self._apply_scope_order(project_id, state, parent_id, ids)
        return self.get_task(project_id, number)

    def get_task(self, project_id: int, number: int) -> dict:
        row = self._get_task(project_id, number)
        return self._serialize_task(row)

    def list_tasks(
        self,
        project_id: int,
        state: str | None = None,
        include_archived: bool = False,
        label: str | None = None,
    ) -> list[dict]:
        self._get_project(project_id)
        sql = "SELECT * FROM tasks WHERE project_id = ?"
        args: list[Any] = [project_id]
        if state is not None:
            if state not in db.ALL_STATES:
                raise ValidationError(f"unknown state '{state}'")
            sql += " AND state = ?"
            args.append(state)
        if not include_archived and state is None:
            sql += f" AND state != '{db.ARCHIVED_STATE}'"
        if label is not None:
            label = (label or "").strip()
            if not label:
                raise ValidationError("label must not be empty")
            row = self.conn.execute(
                "SELECT l.id FROM labels l WHERE l.project_id = ? AND l.name = ? COLLATE NOCASE",
                (project_id, label),
            ).fetchone()
            if row is None:
                raise ValidationError(f"label '{label}' not found")
            sql += (
                " AND id IN (SELECT task_id FROM task_labels WHERE label_id = ?)"
            )
            args.append(row["id"])
        rows = self.conn.execute(
            sql + " ORDER BY state, parent_id, sort_order", args
        ).fetchall()
        return self._serialize_tasks(rows)

    def _all_descendants(self, task_id: int) -> list[sqlite3.Row]:
        """All transitive children of an epic (BFS over parent links)."""
        out: list[sqlite3.Row] = []
        frontier = [task_id]
        seen: set[int] = set()
        while frontier:
            placeholders, params = _in_clause(frontier)
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE parent_id IN ({placeholders})",
                params,
            ).fetchall()
            frontier = []
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                out.append(r)
                frontier.append(r["id"])
        return out

    def _estimate_total(self, task_id: int) -> float | None:
        """Epic estimate = sum of contained regular tasks' estimates (transitive)."""
        total = 0.0
        any_estimate = False
        for child in self._all_descendants(task_id):
            if child["estimate"] is not None:
                total += child["estimate"]
                any_estimate = True
        return total if any_estimate else None

    def _batch_task_maps(self, rows: list[sqlite3.Row]) -> dict:
        """Bulk-load everything task serialization needs, in five queries.

        Returns a dict of maps for a batch of task rows: ``types`` (type id
        -> task_types row), ``parents`` (parent task id -> number),
        ``prereqs`` (task id -> dicts ordered by number), ``attachments``
        (task id -> dicts ordered by id) and ``labels`` (task id -> dicts
        ordered by name COLLATE NOCASE). ``_in_clause`` degrades an empty id
        list to ``IN (NULL)``, so an empty batch yields empty maps with no
        special-casing.
        """
        ids = [r["id"] for r in rows]
        ph, params = _in_clause(ids)
        type_ph, type_params = _in_clause({r["type_id"] for r in rows})
        type_map = {
            t["id"]: t
            for t in self.conn.execute(
                f"SELECT * FROM task_types WHERE id IN ({type_ph})", type_params
            ).fetchall()
        }
        parent_ids = {r["parent_id"] for r in rows if r["parent_id"] is not None}
        parent_ph, parent_params = _in_clause(parent_ids)
        parent_map = {
            p["id"]: p["number"]
            for p in self.conn.execute(
                f"SELECT id, number FROM tasks WHERE id IN ({parent_ph})",
                parent_params,
            ).fetchall()
        }
        prereq_map: dict[int, list[dict]] = {}
        for p in self.conn.execute(
            "SELECT pr.task_id, p2.number, p2.title, p2.state "
            "FROM task_prereqs pr JOIN tasks p2 ON p2.id = pr.prereq_id "
            f"WHERE pr.task_id IN ({ph}) ORDER BY pr.task_id, p2.number",
            params,
        ).fetchall():
            prereq_map.setdefault(p["task_id"], []).append(
                {"number": p["number"], "title": p["title"], "state": p["state"]}
            )
        attachment_map: dict[int, list[dict]] = {}
        # length(data) is metadata-only: SQLite gets the byte count from the
        # record header and never copies the BLOB bytes into the row, so a
        # listing stays O(metadata) no matter how many attachments exist.
        for a in self.conn.execute(
            f"SELECT id, task_id, filename, content_type, length(data) AS size, "
            f"created_at FROM attachments WHERE task_id IN ({ph}) "
            "ORDER BY task_id, id",
            params,
        ).fetchall():
            attachment_map.setdefault(a["task_id"], []).append(
                {
                    "id": a["id"],
                    "filename": a["filename"],
                    "content_type": a["content_type"],
                    "size": a["size"],
                    "created_at": a["created_at"],
                }
            )
        label_map: dict[int, list[dict]] = {}
        for l in self.conn.execute(
            "SELECT tl.task_id, l.id, l.name, l.color FROM labels l "
            "JOIN task_labels tl ON tl.label_id = l.id "
            f"WHERE tl.task_id IN ({ph}) "
            "ORDER BY tl.task_id, l.name COLLATE NOCASE",
            params,
        ).fetchall():
            label_map.setdefault(l["task_id"], []).append(
                {"id": l["id"], "name": l["name"], "color": l["color"] or ""}
            )
        return {
            "types": type_map,
            "parents": parent_map,
            "prereqs": prereq_map,
            "attachments": attachment_map,
            "labels": label_map,
        }

    def _compose_task(self, row: sqlite3.Row, maps: dict) -> dict:
        """Build the serialized task dict for `row` from pre-fetched `maps`.

        Produces exactly the dict the old per-query `_serialize_task` did:
        same fields, value types and orderings. ``estimate_total`` for epics
        still runs the per-epic BFS against the DB — that fan-out is
        deliberate and left as a follow-up (see #77 plan).
        """
        ttype = maps["types"].get(row["type_id"])
        if ttype is None:
            raise ValidationError(f"unknown task type id {row['type_id']}")
        is_epic = bool(ttype["is_epic"])
        prereqs = maps["prereqs"].get(row["id"], [])
        parent_number = None
        if row["parent_id"] is not None:
            parent_number = maps["parents"].get(row["parent_id"])
        return {
            "id": row["id"],
            "number": row["number"],
            "title": row["title"],
            "description": row["description"],
            "type": ttype["name"],
            "is_epic": is_epic,
            "state": row["state"],
            "estimate": row["estimate"],
            "estimate_total": (
                self._estimate_total(row["id"]) if is_epic else row["estimate"]
            ),
            "parent_number": parent_number,
            "sort_order": row["sort_order"],
            "prerequisites": prereqs,
            "has_unmet_prerequisites": any(p["state"] != "Done" for p in prereqs),
            "attachments": maps["attachments"].get(row["id"], []),
            "labels": maps["labels"].get(row["id"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created_by": row["created_by"],
        }

    def _serialize_tasks(self, rows: list[sqlite3.Row]) -> list[dict]:
        """Serialize a batch of task rows with a single shared bulk load."""
        maps = self._batch_task_maps(rows)
        return [self._compose_task(r, maps) for r in rows]

    def _serialize_task(self, row: sqlite3.Row) -> dict:
        return self._serialize_tasks([row])[0]

    def get_project(self, project_id: int) -> dict:
        proj = self._get_project(project_id)
        tasks = self._serialize_tasks(self.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? "
            "ORDER BY state, parent_id, sort_order, number",
            (project_id,),
        ).fetchall())
        by_number = {t["number"]: t for t in tasks}
        children_map: dict[int | None, list[dict]] = {}
        for t in tasks:
            pid = by_number[t["parent_number"]]["id"] if t["parent_number"] else None
            children_map.setdefault(pid, []).append(t)

        def build(t: dict) -> dict:
            out = dict(t)
            out["children"] = [build(c) for c in children_map.get(t["id"], [])]
            return out

        roots = [t for t in tasks if t["parent_number"] is None]
        roots.sort(key=lambda x: (x["state"], x["sort_order"], x["number"]))
        return {
            **dict(proj),
            "task_types": self.list_task_types(),
            "roles": self.list_project_roles(project_id),
            "tasks": [build(t) for t in roots],
        }

    def get_next_task(self, project_id: int) -> dict | None:
        """Return the next actionable task, or None if nothing is actionable.

        Priority order (highest first): Review, In progress, Planning, Todo.
        Within each state, tasks follow their sort_order. If the top candidate
        has a prerequisite not yet Done, the first unmet prerequisite is
        followed recursively, since it must be worked on first. Archived,
        Blocked, Done and Backlog tasks are never returned; holding states are
        skipped implicitly by only scanning the listed forward states.
        """
        self._get_project(project_id)
        priority_states = ["Review", "In progress", "Planning", "Todo"]
        for state in priority_states:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND state = ? "
                "ORDER BY sort_order, id",
                (project_id, state),
            ).fetchall()
            for row in rows:
                result = self._follow_prereqs(row)
                if result is not None:
                    return result
        return None

    def _follow_prereqs(self, row: sqlite3.Row, _seen: set[int] | None = None) -> dict | None:
        """Resolve the actionable task reached from `row`, following prerequisites.

        If `row` has prerequisites whose state is not Done, follow the first
        unmet one (by task number) recursively. Returns the serialized task
        once a candidate has no unmet prerequisites, or None if the chain
        dead-ends. ``_seen`` guards against prerequisite cycles (structurally
        impossible once set, kept as a cheap safety net).
        """
        if _seen is None:
            _seen = set()
        if row["id"] in _seen:
            return None
        _seen.add(row["id"])
        prereqs = self.conn.execute(
            "SELECT p2.* "
            "FROM task_prereqs pr JOIN tasks p2 ON p2.id = pr.prereq_id "
            "WHERE pr.task_id = ? ORDER BY p2.number",
            (row["id"],),
        ).fetchall()
        unmet = [p for p in prereqs if p["state"] != "Done"]
        if unmet:
            return self._follow_prereqs(unmet[0], _seen)
        return self._serialize_task(row)

    # -- updating ------------------------------------------------------------

    def update_task(
        self,
        project_id: int,
        number: int,
        title: str | None = None,
        description: str | None = None,
        type: str | None = None,
        estimate: float | None = None,
        parent_number: int | None = _UNSET,
    ) -> dict:
        row = self._get_task(project_id, number)
        ttype = self._get_type(row["type_id"])
        now = self._now()

        if title is not None:
            title = title.strip()
            if not title:
                raise ValidationError("task title must not be empty")
        if type is not None:
            new_type = self._type_by_name(type)
            if new_type is None:
                raise ValidationError(f"unknown task type '{type}'")
            ttype = new_type
        _validate_estimate(estimate, ttype["is_epic"])

        new_parent_id = row["parent_id"]
        if parent_number is not _UNSET:
            if parent_number is None:
                new_parent_id = None
            else:
                parent = self._get_task(project_id, parent_number)
                if not self._is_epic_row(parent):
                    raise ValidationError("only an epic can contain other tasks")
                if parent["id"] == row["id"]:
                    raise CycleError("a task cannot be its own parent")
                for d in self._all_descendants(row["id"]):
                    if d["id"] == parent["id"]:
                        raise CycleError(
                            f"task #{number} already contains task #{parent_number}"
                        )
                new_parent_id = parent["id"]

        has_children = self.conn.execute(
            "SELECT 1 FROM tasks WHERE parent_id = ? LIMIT 1", (row["id"],)
        ).fetchone()
        if has_children and not ttype["is_epic"]:
            raise ValidationError(
                "task has children and cannot be changed away from an epic"
            )

        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET title = ?, description = ?, type_id = ?, estimate = ?,"
                " parent_id = ?, updated_at = ? WHERE id = ?",
                (
                    title if title is not None else row["title"],
                    description if description is not None else row["description"],
                    ttype["id"],
                    None if ttype["is_epic"] else (
                        estimate if estimate is not None else row["estimate"]
                    ),
                    new_parent_id,
                    now,
                    row["id"],
                ),
            )
            if new_parent_id != row["parent_id"]:
                state = row["state"]
                so = self.next_sort_order(project_id, state, new_parent_id)
                self.conn.execute(
                    "UPDATE tasks SET sort_order = ? WHERE id = ?", (so, row["id"])
                )
                self._renumber_scope(project_id, state, new_parent_id)
                if row["parent_id"] is not None:
                    self._renumber_scope(project_id, state, row["parent_id"])
        return self.get_task(project_id, number)

    # -- prerequisites ---------------------------------------------------------

    def _depends_on(self, from_id: int, to_id: int) -> bool:
        """True if `from_id` transitively depends on `to_id` via prereq edges."""
        frontier = [from_id]
        seen: set[int] = set()
        while frontier:
            placeholders, params = _in_clause(frontier)
            rows = self.conn.execute(
                f"SELECT prereq_id FROM task_prereqs "
                f"WHERE task_id IN ({placeholders})",
                params,
            ).fetchall()
            frontier = []
            for r in rows:
                if r["prereq_id"] == to_id:
                    return True
                if r["prereq_id"] in seen:
                    continue
                seen.add(r["prereq_id"])
                frontier.append(r["prereq_id"])
        return False

    def set_prerequisites(
        self, project_id: int, number: int, prereq_numbers: list[int]
    ) -> dict:
        row = self._get_task(project_id, number)
        prereq_ids: list[int] = []
        for pn in prereq_numbers:
            target = self._get_task(project_id, pn)
            if target["id"] == row["id"]:
                raise ValidationError("a task cannot be a prerequisite of itself")
            if self._depends_on(target["id"], row["id"]):
                raise CycleError(
                    f"task #{number} already (transitively) depends on task #{pn}; "
                    "adding it would create a cycle"
                )
            if target["id"] not in prereq_ids:
                prereq_ids.append(target["id"])
        self._replace_links("task_prereqs", "task_id", row["id"], "prereq_id", prereq_ids)
        return self.get_task(project_id, number)

    # -- moving in the workflow --------------------------------------------------

    def plan_move(self, project_id: int, number: int, to_state: str) -> list[dict]:
        """Compute the set of tasks whose state would change.

        The moved task plus every transitive prerequisite that has not yet
        reached `to_state` (i.e. sits at an earlier workflow stage). Archived
        and Blocked prerequisites (holding states) are left untouched.
        Moving to a holding state (Blocked) is terminal for this action and
        pulls no prerequisites along.
        """
        if to_state not in db.ALL_STATES:
            raise ValidationError(f"cannot move to '{to_state}'")
        row = self._get_task(project_id, number)
        if row["state"] == to_state:
            return []
        # Moving to a holding state is a single-task action: it does not pull
        # prerequisites forward (mirrors Archived; Blocked is the only move
        # target among the holding states).
        if to_state in db.HOLDING_STATES:
            return self._describe_tasks([row["id"]], to_state)
        target_rank = db.STATE_RANK[to_state]

        affected_ids: list[int] = [row["id"]]
        seen = {row["id"]}
        frontier = [row["id"]]
        while frontier:
            placeholders, params = _in_clause(frontier)
            rows = self.conn.execute(
                "SELECT p2.* FROM task_prereqs pr JOIN tasks p2 ON p2.id = pr.prereq_id "
                f"WHERE pr.task_id IN ({placeholders})",
                params,
            ).fetchall()
            frontier = []
            for p in rows:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                if p["state"] in db.HOLDING_STATES:
                    continue
                if db.STATE_RANK[p["state"]] < target_rank:
                    affected_ids.append(p["id"])
                    frontier.append(p["id"])
        return self._describe_tasks(affected_ids, to_state)

    def _describe_tasks(self, ids: list[int], to_state: str | None = None) -> list[dict]:
        """Describe tasks by id for plan/confirmation payloads.

        With ``to_state`` given, each entry describes a state transition:
        ``number``/``title``/``type``/``from``/``to``. Without it, each entry
        describes the task's current state: ``number``/``title``/``type``/
        ``state``.
        """
        out = []
        for i in ids:
            r = self.conn.execute(
                "SELECT t.number, t.title, t.state, tt.name AS type_name "
                "FROM tasks t JOIN task_types tt ON tt.id = t.type_id WHERE t.id = ?",
                (i,),
            ).fetchone()
            entry = {
                "number": r["number"],
                "title": r["title"],
                "type": r["type_name"],
            }
            if to_state is None:
                entry["state"] = r["state"]
            else:
                entry["from"] = r["state"]
                entry["to"] = to_state
            out.append(entry)
        return out

    def _guard_confirmation(self, affected: list[dict], confirm: bool):
        """Shared confirmation contract for mutating actions.

        Returns the no-op result when the plan is empty, raises
        ``ConfirmationRequired`` when the plan touches more than one task and
        ``confirm`` is false, and returns ``None`` when the caller may proceed.
        """
        if not affected:
            return {"applied": True, "affected": []}
        if len(affected) > 1 and not confirm:
            raise ConfirmationRequired(affected)
        return None

    def move_task(
        self,
        project_id: int,
        number: int,
        to_state: str,
        confirm: bool = False,
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        affected = self.plan_move(project_id, number, to_state)
        noop = self._guard_confirmation(affected, confirm)
        if noop is not None:
            return noop

        now = self._now()
        main_row = self._get_task(project_id, number)
        pulled: list[sqlite3.Row] = []
        for a in affected[1:]:
            pulled.append(
                self.conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? AND number = ?",
                    (project_id, a["number"]),
                ).fetchone()
            )
        with self.conn:
            for t in [main_row, *pulled]:
                self._apply_state(t["id"], to_state, t["sort_order"], now)
            # place the main task (and its pulled prerequisites right after it);
            # the scope was refetched after _apply_state, so drop the moved ids
            # before inserting them exactly once (see #83)
            moved_ids = {main_row["id"], *(t["id"] for t in pulled)}
            idx = self._position_index(
                project_id, to_state, main_row["parent_id"], before_number, after_number,
                exclude_ids=moved_ids,
            )
            ids = [
                i
                for i in self._scope_task_ids(project_id, to_state, main_row["parent_id"])
                if i not in moved_ids
            ]
            ids.insert(idx, main_row["id"])
            for t in pulled:
                ids.append(t["id"])
            self._apply_scope_order(project_id, to_state, main_row["parent_id"], ids)
            for a in affected:
                if a["from"] != to_state:
                    p = self.conn.execute(
                        "SELECT parent_id FROM tasks WHERE project_id = ? AND number = ?",
                        (project_id, a["number"]),
                    ).fetchone()
                    self._renumber_scope(project_id, a["from"], p["parent_id"])
        return {"applied": True, "affected": affected}

    def _apply_state(self, task_id: int, to_state: str, sort_order: float, now: str) -> None:
        cur = self.conn.execute(
            "SELECT state FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        self.conn.execute(
            "UPDATE tasks SET state = ?, sort_order = ?, updated_at = ? WHERE id = ?",
            (to_state, sort_order, now, task_id),
        )
        self._log_state(task_id, cur["state"], to_state, now, self.source)

    # -- archiving ---------------------------------------------------------------

    def plan_archive(self, project_id: int, number: int) -> list[dict]:
        row = self._get_task(project_id, number)
        if row["state"] == db.ARCHIVED_STATE:
            return []
        ids = [row["id"]]
        if self._is_epic_row(row):
            ids += [
                d["id"]
                for d in self._all_descendants(row["id"])
                if d["state"] != db.ARCHIVED_STATE
            ]
        return self._describe_tasks(ids, db.ARCHIVED_STATE)

    def archive_task(self, project_id: int, number: int, confirm: bool = False) -> dict:
        affected = self.plan_archive(project_id, number)
        noop = self._guard_confirmation(affected, confirm)
        if noop is not None:
            return noop
        now = self._now()
        with self.conn:
            for a in affected:
                t = self.conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? AND number = ?",
                    (project_id, a["number"]),
                ).fetchone()
                self._apply_state(t["id"], db.ARCHIVED_STATE,
                                  self.next_sort_order(
                                      project_id, db.ARCHIVED_STATE, t["parent_id"]
                                  ),
                                  now)
                self._renumber_scope(project_id, db.ARCHIVED_STATE, t["parent_id"])
                self._renumber_scope(project_id, a["from"], t["parent_id"])
        return {"applied": True, "affected": affected}

    def restore_task(
        self,
        project_id: int,
        number: int,
        to_state: str = db.DEFAULT_STATE,
        confirm: bool = False,
    ) -> dict:
        """Un-archive a single task. Children of an epic stay where they are."""
        row = self._get_task(project_id, number)
        if row["state"] != db.ARCHIVED_STATE:
            raise ValidationError("task is not archived")
        if to_state not in db.WORKFLOW_STATES:
            raise ValidationError(f"cannot restore to '{to_state}'")
        affected = self.plan_move(project_id, number, to_state)
        noop = self._guard_confirmation(affected, confirm)
        if noop is not None:
            return noop
        now = self._now()
        with self.conn:
            so = self.next_sort_order(project_id, to_state, row["parent_id"])
            self._apply_state(row["id"], to_state, so, now)
            so += 1.0
            for a in affected[1:]:
                p = self.conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? AND number = ?",
                    (project_id, a["number"]),
                ).fetchone()
                self._apply_state(p["id"], to_state, so, now)
                so += 1.0
            self._renumber_scope(project_id, to_state, row["parent_id"])
            self._renumber_scope(project_id, db.ARCHIVED_STATE, row["parent_id"])
        return {"applied": True, "affected": affected}

    # -- deletion ------------------------------------------------------------------

    def plan_delete(self, project_id: int, number: int) -> list[dict]:
        row = self._get_task(project_id, number)
        ids = [row["id"]] + [d["id"] for d in self._all_descendants(row["id"])]
        return self._describe_tasks(ids)

    def delete_task(self, project_id: int, number: int, confirm: bool = False) -> dict:
        row = self._get_task(project_id, number)
        if row["state"] != db.ARCHIVED_STATE:
            raise ValidationError("only archived tasks can be deleted")
        affected = self.plan_delete(project_id, number)
        noop = self._guard_confirmation(affected, confirm)
        if noop is not None:
            return noop
        now = self._now()
        with self.conn:
            for d in self._all_descendants(row["id"]):
                self.conn.execute("DELETE FROM tasks WHERE id = ?", (d["id"],))
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
            states = self.conn.execute(
                "SELECT DISTINCT state FROM tasks WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for s in states:
                parents = self.conn.execute(
                    "SELECT DISTINCT parent_id FROM tasks "
                    "WHERE project_id = ? AND state = ?",
                    (project_id, s["state"]),
                ).fetchall()
                for p in parents:
                    self._renumber_scope(project_id, s["state"], p["parent_id"])
        return {"applied": True, "affected": affected}

    # -- ordering -------------------------------------------------------------------

    def reorder_task(
        self,
        project_id: int,
        number: int,
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        row = self._get_task(project_id, number)
        state, parent_id = row["state"], row["parent_id"]
        # Reordering relative to itself is a no-op ("dropped it where it
        # already was"). Only meaningful when exactly one of before/after is
        # given; the both-set case still falls through to validation below.
        if (before_number is None) != (after_number is None) and (
            before_number or after_number
        ) == number:
            return self.get_task(project_id, number)
        idx = self._position_index(
            project_id, state, parent_id, before_number, after_number,
            exclude_ids={row["id"]},
        )
        ids = [i for i in self._scope_task_ids(project_id, state, parent_id)
               if i != row["id"]]
        ids.insert(idx, row["id"])
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (self._now(), row["id"])
            )
            self._apply_scope_order(project_id, state, parent_id, ids)
        return self.get_task(project_id, number)

    # -- history ----------------------------------------------------------------------

    def get_history(self, project_id: int, number: int) -> list[dict]:
        row = self._get_task(project_id, number)
        rows = self.conn.execute(
            "SELECT * FROM state_history WHERE task_id = ? ORDER BY changed_at, id",
            (row["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def max_state_history_id(self) -> int:
        """The largest ``state_history.id`` (0 when there is no history)."""
        row = self.conn.execute("SELECT MAX(id) AS m FROM state_history").fetchone()
        return row["m"] or 0

    def new_state_changes(self, since_id: int) -> list[dict]:
        """State changes recorded after ``since_id``, in history (id) order.

        The Telegram bot's notification-cursor query. Returns one lean dict
        per transition row — ``from_state IS NOT NULL`` excludes task
        creations, which are not state changes — joined to the task and
        project so a notification composes from a single row. Deleting a
        task cascade-deletes its history rows, so the join never dangles.
        """
        rows = self.conn.execute(
            "SELECT h.id, p.id AS project_id, p.name AS project_name, "
            "t.number, t.title, h.from_state, h.to_state, h.changed_at "
            "FROM state_history h "
            "JOIN tasks t ON t.id = h.task_id "
            "JOIN projects p ON p.id = t.project_id "
            "WHERE h.id > ? AND h.from_state IS NOT NULL "
            "ORDER BY h.id",
            (since_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "project_name": r["project_name"],
                "number": r["number"],
                "title": r["title"],
                "from_state": r["from_state"],
                "to_state": r["to_state"],
                "changed_at": r["changed_at"],
            }
            for r in rows
        ]

    # -- telegram subscriptions ---------------------------------------------------

    def subscribe_project(self, chat_id: int, project_id: int) -> dict:
        """Subscribe ``chat_id`` to the project's state-change notifications.

        Idempotent: a repeat keeps the existing row (and its
        ``created_at``). Raises ``NotFound`` for an unknown project.
        """
        proj = self._get_project(project_id)
        now = self._now()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO telegram_subscriptions"
                "(chat_id, project_id, created_at) VALUES (?, ?, ?)",
                (chat_id, project_id, now),
            )
        row = self.conn.execute(
            "SELECT chat_id, project_id, created_at "
            "FROM telegram_subscriptions WHERE chat_id = ? AND project_id = ?",
            (chat_id, project_id),
        ).fetchone()
        return {
            "chat_id": row["chat_id"],
            "project_id": row["project_id"],
            "project_name": proj["name"],
            "created_at": row["created_at"],
        }

    def unsubscribe_project(self, chat_id: int, project_id: int) -> dict:
        """Remove the chat's subscription; report whether one was removed."""
        self._get_project(project_id)
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM telegram_subscriptions "
                "WHERE chat_id = ? AND project_id = ?",
                (chat_id, project_id),
            )
        return {"applied": True, "removed": cur.rowcount > 0}

    def list_subscriptions(self, chat_id: int) -> list[dict]:
        """The chat's subscriptions, joined to the project name, in name order."""
        rows = self.conn.execute(
            "SELECT p.id, p.name, s.created_at "
            "FROM telegram_subscriptions s "
            "JOIN projects p ON p.id = s.project_id "
            "WHERE s.chat_id = ? ORDER BY p.name COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
        return [
            {"project_id": r["id"], "project_name": r["name"], "created_at": r["created_at"]}
            for r in rows
        ]

    def subscribed_chats(self, project_id: int) -> list[int]:
        """Chat ids subscribed to the project (the notifier's fan-out list)."""
        self._get_project(project_id)
        rows = self.conn.execute(
            "SELECT chat_id FROM telegram_subscriptions "
            "WHERE project_id = ? ORDER BY chat_id",
            (project_id,),
        ).fetchall()
        return [r["chat_id"] for r in rows]

    # -- telegram user allowlist (bot authentication) ----------------------------------

    def _get_telegram_user_row(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM telegram_users WHERE chat_id = ?", (chat_id,)
        ).fetchone()

    def add_telegram_user(self, chat_id: int, password: str) -> dict:
        """Permit a Telegram chat to authenticate to the bot.

        ``chat_id`` must be a positive integer and ``password`` non-empty;
        the password is stored only as a salted scrypt hash, never in plain
        form. A chat that is already permitted is a conflict — use
        :meth:`set_telegram_user_password` to rotate its password. The
        returned dict never carries the hash. A newly permitted chat holds
        no login session yet — it must ``/login`` once before the board is
        open to it.
        """
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
            raise ValidationError("chat id must be a positive integer")
        if password is None or not password.strip():
            raise ValidationError("password must not be empty")
        if self._get_telegram_user_row(chat_id) is not None:
            raise Conflict(f"telegram user {chat_id} already exists")
        now = self._now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO telegram_users(chat_id, password_hash, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, _hash_password(password), now, now),
            )
        return {"chat_id": chat_id, "created_at": now, "updated_at": now}

    def list_telegram_users(self) -> list[dict]:
        """All permitted Telegram chats, in chat-id order.

        The password hash is an internal secret: the serialization exposes
        only ``chat_id``, ``created_at`` and ``updated_at``.
        """
        rows = self.conn.execute(
            "SELECT chat_id, created_at, updated_at "
            "FROM telegram_users ORDER BY chat_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_telegram_user_password(self, chat_id: int, password: str) -> dict:
        """Replace a permitted chat's password (re-hashed, ``updated_at`` bumped).

        The rotation also invalidates the chat's login session
        (``authenticated_at`` reset to NULL): a fresh ``/login`` with the new
        password is required, and a running bot picks up the revocation on
        its next gate check.
        """
        if password is None or not password.strip():
            raise ValidationError("password must not be empty")
        row = self._get_telegram_user_row(chat_id)
        if row is None:
            raise NotFound(f"telegram user {chat_id} not found")
        now = self._now()
        with self.conn:
            self.conn.execute(
                "UPDATE telegram_users SET password_hash = ?, updated_at = ?, "
                "authenticated_at = NULL WHERE chat_id = ?",
                (_hash_password(password), now, chat_id),
            )
        return {
            "chat_id": chat_id,
            "created_at": row["created_at"],
            "updated_at": now,
        }

    def remove_telegram_user(self, chat_id: int) -> dict:
        """Revoke a chat's permission to authenticate."""
        if self._get_telegram_user_row(chat_id) is None:
            raise NotFound(f"telegram user {chat_id} not found")
        with self.conn:
            self.conn.execute("DELETE FROM telegram_users WHERE chat_id = ?", (chat_id,))
        return {"applied": True, "removed": True}

    def verify_telegram_user(self, chat_id: int, password: str) -> bool:
        """Whether ``chat_id`` is permitted and ``password`` is its password.

        Returns ``False`` both for an unknown chat and for a wrong password —
        callers (the bot) must not be able to tell the two apart.
        """
        row = self._get_telegram_user_row(chat_id)
        if row is None:
            return False
        return _verify_password(password or "", row["password_hash"])

    def login_telegram_user(self, chat_id: int, password: str) -> bool:
        """Authenticate a permitted chat and persist its login session.

        Verifies the password with the same contract as
        :meth:`verify_telegram_user` (unknown chat, wrong or empty password
        all return ``False`` indistinguishably — no allowlist enumeration).
        A chat is temporarily **locked out** after
        :data:`TELEGRAM_LOGIN_MAX_ATTEMPTS` consecutive failed attempts; the
        lockout is enforced *before* scrypt is computed, so a locked-out or
        unknown attempt costs the server no hash. This is the throttle that
        closes CWE-307 on the bot's ``/login`` (task #69): an attacker who
        replays ``/login`` can no longer brute-force a weak password an
        unlimited number of times. On success the chat's
        ``authenticated_at`` is stamped with the current time and the failure
        counter is reset — the session lives in the database, so it survives
        bot restarts. A re-login simply re-stamps the timestamp (idempotent).
        """
        # Unknown chat short-circuits with no state written; a locked-out chat
        # short-circuits *before* scrypt — the throttle gates verification, not
        # merely the result of it.
        row = self._get_telegram_user_row(chat_id)
        if row is None:
            return False
        if _is_locked(row):
            return False
        if not self.verify_telegram_user(chat_id, password):
            self._record_login_failure(chat_id)
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE telegram_users SET authenticated_at = ?, "
                "login_failures = 0, locked_until = NULL "
                "WHERE chat_id = ?",
                (self._now(), chat_id),
            )
        return True

    def _record_login_failure(self, chat_id: int) -> None:
        """Record one failed ``/login`` and lock the chat out at the threshold.

        Called only after a failed verification of a *known* chat. If a
        previously-set lockout has already expired the count starts fresh
        (each attempt begins a new window); once failures reach
        :data:`TELEGRAM_LOGIN_MAX_ATTEMPTS` a ``locked_until`` stamp is set so
        subsequent attempts short-circuit before scrypt.
        """
        row = self._get_telegram_user_row(chat_id)
        if row is None:
            return
        failures = row["login_failures"]
        # A lockout that has already expired means the window elapsed: start
        # a fresh count rather than persisting a stale, exceeded total.
        if row["locked_until"] is not None and not _is_locked(row):
            failures = 0
        failures = failures + 1
        locked_until = None
        if failures >= TELEGRAM_LOGIN_MAX_ATTEMPTS:
            locked_until = _lock_expiry()
        with self.conn:
            self.conn.execute(
                "UPDATE telegram_users SET login_failures = ?, "
                "locked_until = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (failures, locked_until, self._now(), chat_id),
            )

    def is_telegram_user_authenticated(self, chat_id: int) -> bool:
        """Whether the chat is permitted *and* holds a persisted session.

        A newly permitted chat has no session yet (``authenticated_at`` is
        NULL) — it must ``/login`` once before the board is open to it.
        ``False`` for a chat that is not in the allowlist at all.
        """
        row = self.conn.execute(
            "SELECT authenticated_at FROM telegram_users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row is not None and row["authenticated_at"] is not None

    # -- attachments --------------------------------------------------------------------

    ALLOWED_ATTACHMENT_TYPES = {
        "text/markdown",
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }
    MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Strip control characters (incl. CR/LF) and double quotes from a
        filename.

        A stored attachment filename is re-emitted verbatim into the
        ``Content-Disposition`` response header of
        ``GET /api/attachments/{id}`` (see :func:`yask.api.api_get_attachment`),
        so a filename containing ``\\r``/``\\n`` (CRLF injection, CWE-113) or a
        ``"`` (break-out of the quoted param) would be a header-injection hole.
        Sanitizing centrally in the store means every upload path — web, MCP,
        and bot — inherits the same safe value; there is one source of truth.

        Rule: drop C0 controls (0x00–0x1F, incl. ``\\r``, ``\\n``, ``\\t``,
        NUL), DEL (0x7F), C1 controls (0x80–0x9F), and the double-quote
        character (0x22); keep every other character. Fall back to
        ``"attachment"`` when nothing printable survives.
        """
        name = (filename or "attachment").strip() or "attachment"
        cleaned = "".join(
            ch
            for ch in name
            if 0x20 <= ord(ch) < 0x7F and ch != '"'
        )
        return cleaned or "attachment"

    def add_attachment(
        self, project_id: int, number: int, filename: str, content_type: str, data: bytes
    ) -> dict:
        row = self._get_task(project_id, number)
        filename = self._sanitize_filename(filename)
        if content_type not in self.ALLOWED_ATTACHMENT_TYPES:
            raise ValidationError(
                f"attachment type '{content_type}' not allowed (markdown or images only)"
            )
        if len(data) > self.MAX_ATTACHMENT_SIZE:
            raise ValidationError("attachment exceeds 10 MB limit")
        now = self._now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO attachments(task_id, filename, content_type, data, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (row["id"], filename, content_type, data, now),
            )
        # Build the response from the in-memory values just inserted — the
        # INSERT stores exactly these (no DB-side transforms), so no
        # post-INSERT SELECT * is needed and the BLOB is never re-read.
        return {
            "id": cur.lastrowid,
            "filename": filename,
            "content_type": content_type,
            "size": len(data),
            "created_at": now,
        }

    def list_attachments(self, project_id: int, number: int) -> list[dict]:
        row = self._get_task(project_id, number)
        return [
            {
                "id": a["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": a["size"],
                "created_at": a["created_at"],
            }
            for a in self.conn.execute(
                "SELECT id, filename, content_type, length(data) AS size, "
                "created_at FROM attachments WHERE task_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]

    def get_attachment(self, attachment_id: int) -> tuple[dict, bytes]:
        a = self.conn.execute(
            "SELECT filename, content_type, length(data) AS size, created_at, data "
            "FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        if a is None:
            raise NotFound(f"attachment {attachment_id} not found")
        return (
            {
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": a["size"],
                "created_at": a["created_at"],
            },
            bytes(a["data"]),
        )

    def get_task_attachment(
        self, project_id: int, number: int, attachment_id: int
    ) -> tuple[dict, bytes]:
        """One attachment of one task, scoped to that task (the bot's
        ``/attachment`` command).

        Returns the same meta shape as :meth:`get_attachment` plus the
        bytes. Raises ``NotFound`` when the task or the attachment does not
        exist, or when the attachment belongs to a different task — a
        copied or typo'd id can never leak a foreign attachment.
        """
        row = self._get_task(project_id, number)
        a = self.conn.execute(
            "SELECT filename, content_type, length(data) AS size, created_at, data "
            "FROM attachments WHERE id = ? AND task_id = ?",
            (attachment_id, row["id"]),
        ).fetchone()
        if a is None:
            raise NotFound(f"attachment {attachment_id} not found on task #{number}")
        return (
            {
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": a["size"],
                "created_at": a["created_at"],
            },
            bytes(a["data"]),
        )

    def last_attachment(self, project_id: int, number: int) -> tuple[dict, bytes]:
        row = self._get_task(project_id, number)
        a = self.conn.execute(
            "SELECT * FROM attachments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if a is None:
            raise NotFound(f"no attachments on task #{number}")
        meta, data = self.get_attachment(a["id"])
        return {**meta, "id": a["id"]}, data

    def delete_attachment(self, attachment_id: int) -> None:
        a = self.conn.execute(
            "SELECT 1 FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if a is None:
            raise NotFound(f"attachment {attachment_id} not found")
        with self.conn:
            self.conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))

    # -- labels --------------------------------------------------------------------

    def create_label(self, project_id: int, name: str, color: str = "") -> dict:
        self._get_project(project_id)
        name = (name or "").strip()
        if not name:
            raise ValidationError("label name must not be empty")
        norm_color = _normalize_color(color)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO labels(project_id, name, color) VALUES (?, ?, ?)",
                    (project_id, name, norm_color),
                )
        except sqlite3.IntegrityError:
            raise Conflict(f"label '{name}' already exists") from None
        return {"id": cur.lastrowid, "name": name, "color": norm_color}

    def list_labels(self, project_id: int) -> list[dict]:
        self._get_project(project_id)
        return [
            {"id": l["id"], "name": l["name"], "color": l["color"] or ""}
            for l in self.conn.execute(
                "SELECT id, name, color FROM labels WHERE project_id = ? "
                "ORDER BY name COLLATE NOCASE",
                (project_id,),
            ).fetchall()
        ]

    def update_label(self, project_id: int, label_id: int, color: str = "") -> dict:
        self._get_project(project_id)
        row = self.conn.execute(
            "SELECT id, name FROM labels WHERE project_id = ? AND id = ?",
            (project_id, label_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"label {label_id} not found in this project")
        norm_color = _normalize_color(color)
        with self.conn:
            self.conn.execute(
                "UPDATE labels SET color = ? WHERE id = ?", (norm_color, label_id)
            )
        return {"id": label_id, "name": row["name"], "color": norm_color}

    def set_task_labels(self, project_id: int, number: int, label_ids: list[int]) -> dict:
        row = self._get_task(project_id, number)
        valid = {
            l["id"]
            for l in self.conn.execute(
                "SELECT id FROM labels WHERE project_id = ?", (project_id,)
            ).fetchall()
        }
        deduped: list[int] = []
        for lid in label_ids:
            if lid not in valid:
                raise ValidationError(f"label {lid} does not belong to this project")
            if lid not in deduped:
                deduped.append(lid)
        self._replace_links("task_labels", "task_id", row["id"], "label_id", deduped)
        return self.get_task(project_id, number)

    def delete_label(self, project_id: int, label_id: int) -> dict:
        self._get_project(project_id)
        row = self.conn.execute(
            "SELECT id, name FROM labels WHERE project_id = ? AND id = ?",
            (project_id, label_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"label {label_id} not found in this project")
        detached = self.conn.execute(
            "SELECT COUNT(*) FROM task_labels WHERE label_id = ?", (label_id,)
        ).fetchone()[0]
        with self.conn:
            # ON DELETE CASCADE removes the task_labels links, detaching the
            # label from every task it was applied to.
            self.conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
        return {
            "applied": True,
            "label_id": label_id,
            "name": row["name"],
            "detached_tasks": detached,
        }

    # -- project roles (user-story role presets) ----------------------------

    def list_project_roles(self, project_id: int) -> list[dict]:
        self._get_project(project_id)
        return [
            {"id": r["id"], "name": r["name"]}
            for r in self.conn.execute(
                "SELECT id, name FROM project_roles WHERE project_id = ? "
                "ORDER BY id",
                (project_id,),
            ).fetchall()
        ]

    def set_project_roles(self, project_id: int, names: list[str]) -> list[dict]:
        self._get_project(project_id)
        order: list[str] = []
        seen: set[str] = set()
        for raw_name in names:
            name = (raw_name or "").strip()
            if not name:
                raise ValidationError("role name must not be empty")
            if "/" in name:
                # '/' cannot be addressed by DELETE .../roles/{name}
                raise ValidationError("role name must not contain '/'")
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            order.append(name)
        self._replace_links("project_roles", "project_id", project_id, "name", order)
        return self.list_project_roles(project_id)

    def remove_project_role(self, project_id: int, name: str) -> dict:
        self._get_project(project_id)
        name = (name or "").strip()
        row = self.conn.execute(
            "SELECT id, name FROM project_roles WHERE project_id = ? AND name = ? "
            "COLLATE NOCASE",
            (project_id, name),
        ).fetchone()
        if row is None:
            raise NotFound(f"role '{name}' not found in this project")
        with self.conn:
            self.conn.execute(
                "DELETE FROM project_roles WHERE id = ?", (row["id"],)
            )
        return {
            "applied": True,
            "name": row["name"],
            "remaining": len(self.list_project_roles(project_id)),
        }
