// Pure ordering helpers for tasks in columns and scopes.
// These functions have no DOM dependencies and are easily testable.

import { walkTasks } from "./util.js";

// Stored order of one ordering scope: tasks in `stateName` under one parent
// (parentNumber null = root scope), by (sort_order, number) (#140). The
// backend scopes before/after position references per (state, parent), so
// drops resolve within the dragged task's own scope only.
// The task list comes first and is required: a silently defaulted task list
// would make the scope look empty and disable the no-op-drop guard.
export function scopeOrder(projectTasks, stateName, parentNumber) {
  if (!Array.isArray(projectTasks)) {
    throw new TypeError("scopeOrder(projectTasks, stateName, parentNumber): projectTasks must be the project's task list");
  }
  return walkTasks(projectTasks)
    .filter((t) => t.state === stateName && (t.parent_number ?? null) === parentNumber)
    .sort((a, b) => a.sort_order - b.sort_order || a.number - b.number)
    .map((t) => t.number);
}

export function computeNewOrder(order, moved, before, after) {
  const rest = order.filter((n) => n !== moved);
  if (before !== undefined) {
    const idx = Math.max(0, rest.indexOf(before));
    rest.splice(idx, 0, moved);
  } else if (after !== undefined) {
    let idx = rest.indexOf(after);
    idx = idx === -1 ? rest.length : idx + 1;
    rest.splice(idx, 0, moved);
  } else {
    rest.push(moved);
  }
  return rest;
}

function hasLabel(task, name) {
  return (task.labels || []).some((l) => l.name === name);
}

// Tasks that fill a board column: every task in the state — root tasks and
// epic children alike (#140) — in (sort_order, number) order. Within one
// epic scope this is exactly the stored order; across scopes, tasks of
// different epics interleave by rank. The label filter keeps only tasks that
// carry the label directly (same semantics as search results).
export function columnTasks(project, stateName, filterLabel) {
  let tasks = walkTasks(project.tasks).filter((t) => t.state === stateName);
  if (filterLabel) tasks = tasks.filter((t) => hasLabel(t, filterLabel));
  return [...tasks].sort((a, b) => a.sort_order - b.sort_order || a.number - b.number);
}

// Extracted pure position reference resolution for handleDrop's outOfScope case.
// Returns { before, after, skip } where skip means no API call should be made.
//
// A position intent (before/after/end within a column). The backend scopes
// ordering per (state, parent), so a position reference from another scope
// (root vs. epic) would be rejected (#140) (#142).
//
// An in-scope reference is already exact. An out-of-scope one needs mapping:
// dnd names the visual gap by exactly ONE reference card (just above it for
// `before`, just below it for `after`), and since a scope's stored order is a
// subsequence of the column's visible order (both are (sort_order, number)
// sorted), every gap lands in exactly one slot of the dragged task's own
// scope: the slot right before the first in-scope card below the gap. Send
// that single `before` reference — "after the last in-scope card above the
// gap" names the same slot, and with no in-scope card below the gap the slot
// is the end of the scope, which is what sending no reference at all does.
// Never send both (the backend rejects that, #25).
export function resolvePositionReference({ task, intent, project, filterLabel }) {
  const myParent = task.parent_number ?? null;
  const inMyScope = (n, parent) => n != null && (parent ?? null) === myParent;
  let before = intent.before;
  let after = intent.after;
  const outOfScope =
    (before != null && !inMyScope(before, intent.beforeParent)) ||
    (after != null && !inMyScope(after, intent.afterParent));

  if (!outOfScope) {
    return { before, after, skip: false };
  }

  const ref = before ?? after;
  const visible = columnTasks(project, intent.state, filterLabel);
  const idx = visible.findIndex((t) => t.number === ref);
  // The gap is above `before` (the search starts at that card) or below
  // `after` (it starts at the next one). idx === -1 cannot happen (the
  // reference is a card rendered from this very column); clamp to the top
  // of the scope rather than to a wrong neighbour.
  let start = before != null ? idx : idx + 1;
  if (start < 0) start = 0;
  const below = visible
    .slice(start)
    .filter((t) => (t.parent_number ?? null) === myParent);
  before = below.length ? below[0].number : undefined;
  after = undefined;
  // The first in-scope card below the gap is the task itself, so the gap is
  // its own slot and its scope order cannot change. Same reasoning as the
  // sameOrder guard in planDropPosition, and unreachable when the state
  // changes (the task is then not among the target column's cards).
  if (before === task.number && task.state === intent.state) {
    return { before, after, skip: true };
  }
  return { before, after, skip: false };
}

// The whole position-drop decision of handleDrop, minus the API calls:
// resolve the reference into the target (state, parent) scope, compute the
// new stored order of that scope and report whether the drop changes
// anything. Returns { skip, before, after, order, newOrder }; `skip` means
// handleDrop must return without sending a no-op PATCH.
export function planDropPosition({ task, intent, project, filterLabel }) {
  const myParent = task.parent_number ?? null;
  const resolved = resolvePositionReference({ task, intent, project, filterLabel });
  const order = scopeOrder(project.tasks, intent.state, myParent);
  if (resolved.skip) return { skip: true, before: resolved.before, after: resolved.after, order, newOrder: order };
  const newOrder = computeNewOrder(order, task.number, resolved.before, resolved.after);
  const sameOrder = order.length === newOrder.length && order.every((n, i) => n === newOrder[i]);
  return {
    skip: sameOrder && task.state === intent.state,
    before: resolved.before,
    after: resolved.after,
    order,
    newOrder,
  };
}
