// Unit tests for the pure drop-intent helper in yask/web/js/dnd-intent.js.
// Run from the repo root: nix shell nixpkgs#nodejs -c node --test

import test from 'node:test';
import assert from 'node:assert/strict';

import { computePositionIntentFromChildren } from '../../yask/web/js/dnd-intent.js';

// `children` are the column body's children reduced the way dnd.js reduces
// them: the dragged card filtered out, the drop slot marked `isSlot`.
const card = (number, parentNumber) => ({
  isCard: true,
  isSlot: false,
  number: String(number),
  parentNumber,
});
const slot = { isCard: false, isSlot: true };

test('a gap at the top of the column names the first card as `before`', () => {
  const intent = computePositionIntentFromChildren([slot, card(5, ''), card(6, '')]);
  assert.equal(intent.before, 5);
  assert.equal(intent.beforeParent, null);
});

test('a gap between two cards names the previous card as `after`', () => {
  const intent = computePositionIntentFromChildren([card(5, ''), slot, card(6, '')]);
  assert.equal(intent.after, 5);
  assert.equal(intent.afterParent, null);
});

test('a gap at the bottom of the column names the last card as `after`', () => {
  const intent = computePositionIntentFromChildren([card(5, ''), card(6, ''), slot]);
  assert.equal(intent.after, 6);
});

test('the reference card travels with its own ordering scope', () => {
  const intent = computePositionIntentFromChildren([card(10, '20'), slot, card(11, '20')]);
  assert.equal(intent.after, 10);
  assert.equal(intent.afterParent, 20);
});

// The backend rejects a position that carries both before and after (#25).
test('exactly one of `before`/`after` is ever named', () => {
  const shapes = [
    [slot, card(5, ''), card(6, '')],
    [card(5, ''), slot, card(6, '')],
    [card(5, ''), card(6, ''), slot],
    [card(10, '20'), slot, card(11, '20')],
    [slot],
    [card(5, ''), slot],
  ];
  for (const children of shapes) {
    const intent = computePositionIntentFromChildren(children);
    const named = [intent.before, intent.after].filter((v) => v !== undefined);
    assert.ok(
      named.length <= 1,
      `before and after must never both be named: ${JSON.stringify(intent)}`,
    );
  }
});

test('an empty column names no reference at all', () => {
  const intent = computePositionIntentFromChildren([slot]);
  assert.equal(intent.before, undefined);
  assert.equal(intent.after, undefined);
  assert.equal(intent.beforeParent, undefined);
  assert.equal(intent.afterParent, undefined);
});

test('a column without a drop slot yields no intent', () => {
  assert.equal(computePositionIntentFromChildren([card(5, ''), card(6, '')]), null);
});

test('an empty parent number means the root scope', () => {
  const intent = computePositionIntentFromChildren([slot, card(3, '')]);
  assert.equal(intent.before, 3);
  assert.equal(intent.beforeParent, null);
});

test('a missing parent number means the root scope too', () => {
  const intent = computePositionIntentFromChildren([slot, card(3, null)]);
  assert.equal(intent.before, 3);
  assert.equal(intent.beforeParent, null);
});
