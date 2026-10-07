// Unit tests for the pure ordering helpers in yask/web/js/ordering.js.
// Run from the repo root: nix shell nixpkgs#nodejs -c node --test

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  computeNewOrder,
  scopeOrder,
  columnTasks,
} from '../../yask/web/js/ordering.js';

const task = (number, state, sort_order, parent_number = null, labels = []) => ({
  number,
  state,
  sort_order,
  parent_number,
  labels,
});

// -- computeNewOrder -------------------------------------------------------------------

test('computeNewOrder inserts before an existing reference', () => {
  assert.deepEqual(computeNewOrder([1, 2, 3, 4], 5, 2), [1, 5, 2, 3, 4]);
});

test('computeNewOrder inserts after an existing reference', () => {
  assert.deepEqual(computeNewOrder([1, 2, 3, 4], 5, undefined, 2), [1, 2, 5, 3, 4]);
});

test('computeNewOrder appends when there is no reference', () => {
  assert.deepEqual(computeNewOrder([1, 2, 3, 4], 5), [1, 2, 3, 4, 5]);
});

test('computeNewOrder appends when the `after` reference is unknown', () => {
  assert.deepEqual(computeNewOrder([1, 2, 3, 4], 5, undefined, 99), [1, 2, 3, 4, 5]);
});

test('computeNewOrder inserts before the first card when `before` is the first', () => {
  assert.deepEqual(computeNewOrder([2, 3, 4], 5, 2), [5, 2, 3, 4]);
});

test('computeNewOrder drops the moved task from its old position', () => {
  assert.deepEqual(computeNewOrder([1, 5, 2, 3, 4], 5, 1), [5, 1, 2, 3, 4]);
});

test('computeNewOrder moves an existing task within its scope', () => {
  assert.deepEqual(computeNewOrder([29, 77, 71], 77, 29), [77, 29, 71]);
});

// -- scopeOrder ------------------------------------------------------------------------

test('scopeOrder returns one scope by (state, parent), sorted by (sort_order, number)', () => {
  const tasks = [
    task(1, 'Todo', 1),
    task(2, 'Todo', 2),
    task(3, 'In progress', 1),
    task(4, 'Todo', 1, 10),
    task(5, 'Todo', 0),
  ];
  assert.deepEqual(scopeOrder(tasks, 'Todo', null), [5, 1, 2]);
  assert.deepEqual(scopeOrder(tasks, 'Todo', 10), [4]);
  assert.deepEqual(scopeOrder(tasks, 'In progress', null), [3]);
});

test('scopeOrder breaks sort_order ties by number', () => {
  const tasks = [task(10, 'Todo', 1), task(2, 'Todo', 1), task(5, 'Todo', 0)];
  assert.deepEqual(scopeOrder(tasks, 'Todo', null), [5, 2, 10]);
});

test('scopeOrder sees epic children nested under `children`', () => {
  const project = [
    { ...task(4, 'Todo', 1), is_epic: true, children: [task(29, 'Todo', 1, 4), task(77, 'Todo', 2, 4)] },
    task(2, 'Todo', 2),
  ];
  assert.deepEqual(scopeOrder(project, 'Todo', 4), [29, 77]);
  assert.deepEqual(scopeOrder(project, 'Todo', null), [4, 2]);
});

// The task list is the FIRST and REQUIRED argument: a mismatched call must
// fail loudly instead of silently yielding an empty scope (which would make
// every drop look like a move and disable the no-op-drop guard).
test('scopeOrder refuses a missing task list instead of reporting an empty scope', () => {
  assert.throws(() => scopeOrder(undefined, 'Todo', null), TypeError);
  assert.throws(() => scopeOrder(null, 'Todo', null), TypeError);
});

// -- columnTasks -----------------------------------------------------------------------

test('columnTasks returns every task in the state, root and epic children alike', () => {
  const project = {
    tasks: [
      task(1, 'Todo', 1),
      task(2, 'Todo', 2),
      task(3, 'Done', 1),
      {
        ...task(4, 'Todo', 3),
        is_epic: true,
        children: [task(29, 'Todo', 4, 4), task(30, 'In progress', 1, 4)],
      },
    ],
  };
  assert.deepEqual(columnTasks(project, 'Todo', '').map((t) => t.number), [1, 2, 4, 29]);
  assert.deepEqual(columnTasks(project, 'In progress', '').map((t) => t.number), [30]);
});

test('columnTasks keeps only tasks carrying the label directly', () => {
  const project = {
    tasks: [
      task(1, 'Todo', 1, null, [{ name: 'bug' }]),
      task(2, 'Todo', 2, null, [{ name: 'feature' }]),
      task(3, 'Todo', 3, null, []),
      {
        ...task(4, 'Todo', 4),
        is_epic: true,
        children: [task(29, 'Todo', 1, 4, [{ name: 'bug' }])],
      },
    ],
  };
  assert.deepEqual(columnTasks(project, 'Todo', 'bug').map((t) => t.number), [1, 29]);
});

test('a scope order is a subsequence of the column it lives in', () => {
  const project = {
    tasks: [
      task(1, 'Todo', 1),
      task(2, 'Todo', 5),
      {
        ...task(4, 'Todo', 2),
        is_epic: true,
        children: [task(29, 'Todo', 3, 4), task(77, 'Todo', 4, 4)],
      },
    ],
  };
  const column = columnTasks(project, 'Todo', '').map((t) => t.number);
  const scope = scopeOrder(project.tasks, 'Todo', 4);
  const filtered = column.filter((n) => scope.includes(n));
  assert.deepEqual(filtered, scope);
});
