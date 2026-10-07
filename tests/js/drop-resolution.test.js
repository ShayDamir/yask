// Unit tests for handleDrop's position resolution, extracted into
// yask/web/js/ordering.js as planDropPosition/resolvePositionReference.
// Run from the repo root: nix shell nixpkgs#nodejs -c node --test

import test from 'node:test';
import assert from 'node:assert/strict';

import { planDropPosition, resolvePositionReference } from '../../yask/web/js/ordering.js';
import { computePositionIntentFromChildren } from '../../yask/web/js/dnd-intent.js';

const task = (number, state, sort_order, parent_number = null) => ({
  number,
  state,
  sort_order,
  parent_number,
});

// The reported column of #142: epic #4 (its own card) in the Todo column with
// children 29, 77, 71 interleaved between the root cards 2 and 5. The column
// as rendered, in (sort_order, number) order:
//   4 (epic card, root scope), 2, 29, 77, 71, 5
const reported = () => ({
  id: 1,
  tasks: [
    { ...task(4, 'Todo', 0), is_epic: true, children: [
      task(29, 'Todo', 2, 4),
      task(77, 'Todo', 3, 4),
      task(71, 'Todo', 4, 4),
    ] },
    task(2, 'Todo', 1),
    task(5, 'Todo', 5),
  ],
});

// -- helpers that mirror the production wiring, independently of ordering.js ------------

// Independent walk (ordering.js has its own) used to derive the expected
// outcome of a drop without reusing the code under test.
function flatten(nodes, out = []) {
  for (const t of nodes) {
    out.push(t);
    flatten(t.children || [], out);
  }
  return out;
}

const parentOf = (t) => t.parent_number ?? null;

function visibleColumn(project, stateName) {
  return flatten(project.tasks)
    .filter((t) => t.state === stateName)
    .sort((a, b) => a.sort_order - b.sort_order || a.number - b.number);
}

// The children of a column body as dnd.js builds them for a gap at `gapIdx`
// (0 = top of the column), with the dragged card filtered out.
function childrenAtGap(project, stateName, dragged, gapIdx) {
  const cards = visibleColumn(project, stateName)
    .filter((t) => t.number !== dragged)
    .map((t) => ({
      isCard: true,
      isSlot: false,
      number: String(t.number),
      parentNumber: t.parent_number === null ? '' : String(t.parent_number),
    }));
  cards.splice(gapIdx, 0, { isCard: false, isSlot: true });
  return cards;
}

// The intent dnd.js would hand to handleDrop for that gap.
function intentAtGap(project, stateName, dragged, gapIdx) {
  const computed = computePositionIntentFromChildren(childrenAtGap(project, stateName, dragged, gapIdx)) || {};
  return {
    type: 'position',
    state: stateName,
    before: computed.before,
    after: computed.after,
    beforeParent: computed.beforeParent,
    afterParent: computed.afterParent,
  };
}

// Where the dragged card must end up: right after the last in-scope card
// above the gap, or at the top of its scope when nothing of its scope is
// above the gap.
function expectedOrder(project, stateName, dragged, gapIdx) {
  const parent = parentOf(flatten(project.tasks).find((t) => t.number === dragged));
  const scope = visibleColumn(project, stateName)
    .filter((t) => parentOf(t) === parent)
    .map((t) => t.number);
  const above = visibleColumn(project, stateName)
    .filter((t) => t.number !== dragged)
    .slice(0, gapIdx)
    .filter((t) => parentOf(t) === parent);
  const rest = scope.filter((n) => n !== dragged);
  const anchor = above.length ? above[above.length - 1].number : undefined;
  const at = anchor === undefined ? 0 : rest.indexOf(anchor) + 1;
  rest.splice(at, 0, dragged);
  return rest;
}

function currentOrder(project, stateName, dragged) {
  const parent = parentOf(flatten(project.tasks).find((t) => t.number === dragged));
  return visibleColumn(project, stateName)
    .filter((t) => parentOf(t) === parent)
    .map((t) => t.number);
}

// -- invariants over every gap of every card -------------------------------------------

// #142: dropping an epic child next to root cards had no effect at all,
// because the gap was named by an out-of-scope reference that the backend
// rejects. Every gap of every card must still resolve to exactly one slot of
// the dragged task's own (state, parent) scope, and must never be swallowed.
// `toState` is the column the card is dropped into; it defaults to its own.
function sweep(project, fromState, toState, label) {
  const target = toState ?? fromState;
  for (const dragged of visibleColumn(project, fromState).map((t) => t.number)) {
    // n cards in the column leave n-1 once the dragged one is hidden, and so
    // n gap positions: 0 (above the first) ... n-1 (below the last).
    const gapCount = visibleColumn(project, target).length;
    for (let gapIdx = 0; gapIdx < gapCount; gapIdx++) {
      const where = `${label}: #${dragged} dropped at gap ${gapIdx} of ${target}`;
      const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
      const intent = intentAtGap(project, target, dragged, gapIdx);
      const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
      const parent = parentOf(draggedTask);
      const scope = currentOrder(project, target, dragged);
      const expected = expectedOrder(project, target, dragged, gapIdx);

      // 1. never both before and after (the backend rejects that, #25)
      assert.ok(
        plan.before === undefined || plan.after === undefined,
        `${where}: before and after must never both be sent (${JSON.stringify(plan)})`,
      );

      if (plan.skip) {
        // 2. a no-op drop sends nothing at all, and really is a no-op --
        //    and a drop into another column is never one
        assert.deepEqual(plan.order, plan.newOrder, `${where}: skip with a changed order (${JSON.stringify(plan)})`);
        assert.equal(target, fromState, `${where}: a drop into another column must never be a no-op`);
      } else {
        // 3. every reference sent lives inside the target (state, parent)
        //    scope and is never the dragged task itself
        for (const ref of [plan.before, plan.after]) {
          if (ref === undefined) continue;
          const referenced = flatten(project.tasks).find(
            (t) => t.number === ref && t.state === target && parentOf(t) === parent,
          );
          assert.ok(referenced, `${where}: reference #${ref} is not in the target scope (${JSON.stringify(plan)})`);
          assert.notEqual(ref, dragged, `${where}: a card must never be sent as its own reference`);
        }
      }

      // 4. the stored order that results is the semantically correct one:
      //    right after the last in-scope card above the gap
      assert.deepEqual(plan.order, scope, `${where}: wrong scope order`);
      assert.deepEqual(plan.newOrder, expected, `${where}: wrong resulting order (${JSON.stringify(plan)})`);

      // 5. no real move is ever swallowed by the no-op guard
      assert.equal(
        plan.skip,
        expected.join(',') === scope.join(','),
        `${where}: skip does not match whether the order actually changes (${JSON.stringify(plan)})`,
      );
    }
  }
}

test('every gap of every card in the reported #142 column resolves in scope', () => {
  sweep(reported(), 'Todo', null, 'reported column');
});

test('every gap of every card in a two-epic column resolves in scope', () => {
  sweep(
    {
      id: 2,
      tasks: [
        { ...task(1, 'Todo', 1), is_epic: true, children: [task(11, 'Todo', 1, 1), task(12, 'Todo', 4, 1)] },
        task(2, 'Todo', 2),
        { ...task(3, 'Todo', 3), is_epic: true, children: [task(31, 'Todo', 3, 3), task(32, 'Todo', 6, 3)] },
        task(4, 'Todo', 5),
      ],
    },
    'Todo',
    null,
    'two epics',
  );
});

test('every gap of every card in a single-scope column resolves in scope', () => {
  sweep(
    { id: 3, tasks: [task(1, 'Todo', 1), task(2, 'Todo', 2), task(3, 'Todo', 3)] },
    'Todo',
    null,
    'root scope only',
  );
});

test('every gap of every card with nested epics resolves in scope', () => {
  sweep(
    {
      id: 4,
      tasks: [
        {
          ...task(1, 'Todo', 1),
          is_epic: true,
          children: [
            task(11, 'Todo', 2, 1),
            { ...task(12, 'Todo', 3, 1), is_epic: true, children: [task(121, 'Todo', 1, 12)] },
          ],
        },
        task(2, 'Todo', 4),
      ],
    },
    'Todo',
    null,
    'nested epics',
  );
});

// Dragging between columns: the card is not in the target scope yet, so every
// drop is a real move and every reference must already sit in the target
// (state, parent) scope.
test('every card dragged into another column resolves in the target scope', () => {
  sweep(
    {
      id: 5,
      tasks: [
        task(1, 'Todo', 1),
        { ...task(4, 'Todo', 2), is_epic: true, children: [task(41, 'Todo', 1, 4), task(42, 'Todo', 2, 4)] },
        task(2, 'Todo', 3),
        task(7, 'In progress', 1),
        { ...task(8, 'In progress', 2), is_epic: true, children: [task(81, 'In progress', 1, 8)] },
        task(9, 'In progress', 3),
      ],
    },
    'Todo',
    'In progress',
    'Todo into In progress',
  );
});

// -- the reported case, spelled out -----------------------------------------------------

test('#142: a gap above an in-scope card, named by an out-of-scope card, moves the card', () => {
  const project = reported();
  const dragged = 77;
  const intent = intentAtGap(project, 'Todo', dragged, 2); // between root #2 and #29

  // dnd names the gap by the card above it — a root card, out of scope.
  assert.equal(intent.after, 2);
  assert.equal(intent.afterParent, null);

  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  const resolved = resolvePositionReference({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(resolved.skip, false);
  assert.equal(resolved.before, 29); // first in-scope card below the gap
  assert.equal(resolved.after, undefined);

  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(plan.skip, false);
  assert.deepEqual(plan.order, [29, 77, 71]);
  assert.deepEqual(plan.newOrder, [77, 29, 71]);
});

test('an out-of-scope reference with no in-scope card below it appends to the scope', () => {
  const project = reported();
  const dragged = 77;
  const intent = intentAtGap(project, 'Todo', dragged, 5); // below the last card, root #5
  assert.equal(intent.after, 5);
  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  const resolved = resolvePositionReference({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(resolved.skip, false);
  assert.equal(resolved.before, undefined);
  assert.equal(resolved.after, undefined);
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.deepEqual(plan.newOrder, [29, 71, 77]);
});

test('an in-scope reference passes through untouched', () => {
  const project = reported();
  const dragged = 77;
  const intent = intentAtGap(project, 'Todo', dragged, 4); // between #71 and root #5
  assert.equal(intent.after, 71);
  assert.equal(intent.afterParent, 4);
  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  const resolved = resolvePositionReference({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(resolved.before, undefined);
  assert.equal(resolved.after, 71);
  assert.equal(resolved.skip, false);
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(plan.after, 71);
  assert.deepEqual(plan.newOrder, [29, 71, 77]);
});

// -- the no-op drop guard ---------------------------------------------------------------

// Regression: handleDrop used to call scopeOrder in a shape that silently
// yielded an empty scope, so this guard never fired and every drop PATCHed.
test('a card dropped back into its own slot is a no-op (no PATCH)', () => {
  const project = { id: 5, tasks: [task(1, 'Todo', 1), task(2, 'Todo', 2), task(3, 'Todo', 3)] };
  const dragged = 2;
  const intent = intentAtGap(project, 'Todo', dragged, 1); // back between #1 and #3
  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.deepEqual(plan.order, [1, 2, 3]);
  assert.deepEqual(plan.newOrder, [1, 2, 3]);
  assert.equal(plan.skip, true);
});

test('an epic child dropped back into its own slot is a no-op', () => {
  const project = reported();
  const dragged = 77;
  const intent = intentAtGap(project, 'Todo', dragged, 3); // between #29 and #71
  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.deepEqual(plan.order, [29, 77, 71]);
  assert.deepEqual(plan.newOrder, [29, 77, 71]);
  assert.equal(plan.skip, true);
});

test('a gap whose only in-scope card below is the dragged card itself sends nothing', () => {
  // Column as rendered: 2 (root), 77 (only child of epic 4), 4 (epic card).
  const project = {
    id: 7,
    tasks: [
      task(2, 'Todo', 1),
      { ...task(4, 'Todo', 3), is_epic: true, children: [task(77, 'Todo', 2, 4)] },
    ],
  };
  const dragged = 77;
  const draggedTask = flatten(project.tasks).find((t) => t.number === dragged);
  // Gap at the top, named by root #2 — the only in-scope card below the gap
  // is the dragged card, so this is its own slot.
  const intent = intentAtGap(project, 'Todo', dragged, 0);
  assert.equal(intent.before, 2);
  assert.equal(intent.beforeParent, null);
  const resolved = resolvePositionReference({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(resolved.skip, true);
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.equal(plan.skip, true);
  assert.deepEqual(plan.order, [77]);
  assert.deepEqual(plan.newOrder, [77]);
});

test('a drop that changes the state is never treated as a no-op', () => {
  const project = { id: 6, tasks: [task(5, 'Todo', 1)] };
  const draggedTask = project.tasks[0];
  const intent = { type: 'position', state: 'In progress', before: undefined, after: undefined };
  const plan = planDropPosition({ task: draggedTask, intent, project, filterLabel: '' });
  assert.deepEqual(plan.order, []);
  assert.deepEqual(plan.newOrder, [5]);
  assert.equal(plan.skip, false);
});
