// Pure helper to compute a drop's position intent from a column body's
// children, so the gap-naming contract can be tested without a DOM.
//
// `children` are the column body's children in DOM order, reduced to
// `{ isCard, isSlot, number, parentNumber }`; `parentNumber` is "" for root
// cards, else the epic's number (both arrive as strings from datasets).
// Dragged cards must already be filtered out by the caller.

export function computePositionIntentFromChildren(children) {
  const slotIdx = children.findIndex((el) => el.isSlot);
  if (slotIdx < 0) {
    return null;
  }
  const prev = children[slotIdx - 1];
  const next = children[slotIdx + 1];
  // A drop in the gap between two cards is "insert after prev" (or,
  // equivalently, "before next"). Send only ONE of the two so we never
  // violate the backend's before/after contract (see #25). The
  // reference card's ordering scope (dataset.parentNumber: "" = root,
  // else the epic's number) travels with it so main.js can resolve
  // cross-scope drops (#140).
  const intent = { before: undefined, after: undefined, beforeParent: undefined, afterParent: undefined };
  if (prev && prev.isCard) {
    intent.after = Number(prev.number);
    intent.afterParent = prev.parentNumber === "" || prev.parentNumber == null ? null : Number(prev.parentNumber);
  } else if (next && next.isCard) {
    intent.before = Number(next.number);
    intent.beforeParent = next.parentNumber === "" || next.parentNumber == null ? null : Number(next.parentNumber);
  }
  return intent;
}
