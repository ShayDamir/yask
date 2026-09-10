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

import re
import sqlite3
from typing import Any

from . import db

_UNSET = object()

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


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


class Store:
    def __init__(self, conn: sqlite3.Connection, source: str = "web"):
        if source not in ("web", "mcp"):
            raise ValidationError(f"invalid source '{source}': expected 'web' or 'mcp'")
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
        # New tasks always start in the Backlog; moving them forward is a
        # separate, tracked action (see #1).
        state = "Backlog"
        self._get_project(project_id)
        title = (title or "").strip()
        if not title:
            raise ValidationError("task title must not be empty")
        ttype = self._type_by_name(type)
        if ttype is None:
            raise ValidationError(f"unknown task type '{type}'")
        if estimate is not None and estimate < 0:
            raise ValidationError("estimate must not be negative")
        if ttype["is_epic"] and estimate is not None:
            raise ValidationError(
                "epics are not estimated; their estimate is the sum of contained tasks"
            )

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
        return [self._serialize_task(r) for r in rows]

    def _all_descendants(self, task_id: int) -> list[sqlite3.Row]:
        """All transitive children of an epic (BFS over parent links)."""
        out: list[sqlite3.Row] = []
        frontier = [task_id]
        seen: set[int] = set()
        while frontier:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE parent_id IN (%s)"
                % ",".join("?" * len(frontier)),
                frontier,
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

    def _serialize_task(self, row: sqlite3.Row) -> dict:
        ttype = self._get_type(row["type_id"])
        is_epic = bool(ttype["is_epic"])
        prereqs = [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
            }
            for p in self.conn.execute(
                "SELECT p2.number, p2.title, p2.state "
                "FROM task_prereqs pr JOIN tasks p2 ON p2.id = pr.prereq_id "
                "WHERE pr.task_id = ? ORDER BY p2.number",
                (row["id"],),
            ).fetchall()
        ]
        attachments = [
            {
                "id": a["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": len(a["data"]),
                "created_at": a["created_at"],
            }
            for a in self.conn.execute(
                "SELECT * FROM attachments WHERE task_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
        ]
        labels = [
            {"id": l["id"], "name": l["name"], "color": l["color"] or ""}
            for l in self.conn.execute(
                "SELECT l.id, l.name, l.color FROM labels l "
                "JOIN task_labels tl ON tl.label_id = l.id "
                "WHERE tl.task_id = ? ORDER BY l.name COLLATE NOCASE",
                (row["id"],),
            ).fetchall()
        ]
        parent_number = None
        if row["parent_id"] is not None:
            parent_number = self.conn.execute(
                "SELECT number FROM tasks WHERE id = ?", (row["parent_id"],)
            ).fetchone()["number"]
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
            "attachments": attachments,
            "labels": labels,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created_by": row["created_by"],
        }

    def get_project(self, project_id: int) -> dict:
        proj = self._get_project(project_id)
        tasks = [self._serialize_task(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? "
            "ORDER BY state, parent_id, sort_order, number",
            (project_id,),
        ).fetchall()]
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
        if estimate is not None and estimate < 0:
            raise ValidationError("estimate must not be negative")
        if ttype["is_epic"] and estimate is not None:
            raise ValidationError("epics are not estimated")

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
            rows = self.conn.execute(
                "SELECT prereq_id FROM task_prereqs WHERE task_id IN (%s)"
                % ",".join("?" * len(frontier)),
                frontier,
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
        with self.conn:
            self.conn.execute(
                "DELETE FROM task_prereqs WHERE task_id = ?", (row["id"],)
            )
            self.conn.executemany(
                "INSERT INTO task_prereqs(task_id, prereq_id) VALUES (?, ?)",
                [(row["id"], p) for p in prereq_ids],
            )
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
            return self._describe_states([row["id"]], to_state)
        target_rank = db.STATE_RANK[to_state]

        affected_ids: list[int] = [row["id"]]
        seen = {row["id"]}
        frontier = [row["id"]]
        while frontier:
            rows = self.conn.execute(
                "SELECT p2.* FROM task_prereqs pr JOIN tasks p2 ON p2.id = pr.prereq_id "
                "WHERE pr.task_id IN (%s)" % ",".join("?" * len(frontier)),
                frontier,
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
        return self._describe_states(affected_ids, to_state)

    def _describe_states(self, ids: list[int], to_state: str) -> list[dict]:
        out = []
        for i in ids:
            r = self.conn.execute(
                "SELECT t.number, t.title, t.state, tt.name AS type_name "
                "FROM tasks t JOIN task_types tt ON tt.id = t.type_id WHERE t.id = ?",
                (i,),
            ).fetchone()
            out.append(
                {
                    "number": r["number"],
                    "title": r["title"],
                    "type": r["type_name"],
                    "from": r["state"],
                    "to": to_state,
                }
            )
        return out

    def move_task(
        self,
        project_id: int,
        number: int,
        to_state: str,
        confirm: bool = False,
        before_number: int | None = None,
        after_number: int | None = None,
    ) -> dict:
        row = self._get_task(project_id, number)
        affected = self.plan_move(project_id, number, to_state)
        if not affected:
            return {"applied": True, "affected": []}
        if len(affected) > 1 and not confirm:
            raise ConfirmationRequired(affected)

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
            # place the main task (and its pulled prerequisites right after it)
            idx = self._position_index(
                project_id, to_state, main_row["parent_id"], before_number, after_number,
                exclude_ids={main_row["id"], *(t["id"] for t in pulled)},
            )
            ids = self._scope_task_ids(project_id, to_state, main_row["parent_id"])
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
        return self._describe_states(ids, db.ARCHIVED_STATE)

    def archive_task(self, project_id: int, number: int, confirm: bool = False) -> dict:
        row = self._get_task(project_id, number)
        affected = self.plan_archive(project_id, number)
        if not affected:
            return {"applied": True, "affected": []}
        if len(affected) > 1 and not confirm:
            raise ConfirmationRequired(affected)
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
        to_state: str = "Backlog",
        confirm: bool = False,
    ) -> dict:
        """Un-archive a single task. Children of an epic stay where they are."""
        row = self._get_task(project_id, number)
        if row["state"] != db.ARCHIVED_STATE:
            raise ValidationError("task is not archived")
        if to_state not in db.WORKFLOW_STATES:
            raise ValidationError(f"cannot restore to '{to_state}'")
        affected = self.plan_move(project_id, number, to_state)
        if len(affected) > 1 and not confirm:
            raise ConfirmationRequired(affected)
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
        out = []
        for i in ids:
            r = self.conn.execute(
                "SELECT t.number, t.title, tt.name AS type_name, t.state "
                "FROM tasks t JOIN task_types tt ON tt.id = t.type_id WHERE t.id = ?",
                (i,),
            ).fetchone()
            out.append(
                {
                    "number": r["number"],
                    "title": r["title"],
                    "type": r["type_name"],
                    "state": r["state"],
                }
            )
        return out

    def delete_task(self, project_id: int, number: int, confirm: bool = False) -> dict:
        row = self._get_task(project_id, number)
        if row["state"] != db.ARCHIVED_STATE:
            raise ValidationError("only archived tasks can be deleted")
        affected = self.plan_delete(project_id, number)
        if len(affected) > 1 and not confirm:
            raise ConfirmationRequired(affected)
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

    def add_attachment(
        self, project_id: int, number: int, filename: str, content_type: str, data: bytes
    ) -> dict:
        row = self._get_task(project_id, number)
        filename = (filename or "attachment").strip() or "attachment"
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
        a = self.conn.execute(
            "SELECT * FROM attachments WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return {
            "id": a["id"],
            "filename": a["filename"],
            "content_type": a["content_type"],
            "size": len(a["data"]),
            "created_at": a["created_at"],
        }

    def list_attachments(self, project_id: int, number: int) -> list[dict]:
        row = self._get_task(project_id, number)
        return [
            {
                "id": a["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": len(a["data"]),
                "created_at": a["created_at"],
            }
            for a in self.conn.execute(
                "SELECT * FROM attachments WHERE task_id = ? ORDER BY id", (row["id"],)
            ).fetchall()
        ]

    def get_attachment(self, attachment_id: int) -> tuple[dict, bytes]:
        a = self.conn.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if a is None:
            raise NotFound(f"attachment {attachment_id} not found")
        return (
            {
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": len(a["data"]),
                "created_at": a["created_at"],
            },
            bytes(a["data"]),
        )

    def last_attachment(self, project_id: int, number: int) -> tuple[dict, bytes]:
        self._get_task(project_id, number)
        a = self.conn.execute(
            "SELECT * FROM attachments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (number,),
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
        with self.conn:
            self.conn.execute(
                "DELETE FROM task_labels WHERE task_id = ?", (row["id"],)
            )
            self.conn.executemany(
                "INSERT INTO task_labels(task_id, label_id) VALUES (?, ?)",
                [(row["id"], lid) for lid in deduped],
            )
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
        with self.conn:
            self.conn.execute(
                "DELETE FROM project_roles WHERE project_id = ?", (project_id,)
            )
            self.conn.executemany(
                "INSERT INTO project_roles(project_id, name) VALUES (?, ?)",
                [(project_id, name) for name in order],
            )
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
