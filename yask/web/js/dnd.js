// Drag & drop: move cards between columns, reorder within a column,
// and drop a card onto an epic card to move it into that epic.

import { h } from "./util.js";

const INTO_EPIC_TOP = 0.3;
const INTO_EPIC_BOTTOM = 0.7;

export function initDnd(actions) {
  const board = document.getElementById("board");
  let dragged = null; // number of the dragged root card
  let slot = null; // placeholder element inside a column body
  let intoEpic = null; // number of the epic being targeted for "drop into"

  const clearVisuals = () => {
    if (slot) {
      slot.remove();
      slot = null;
    }
    if (intoEpic !== null) {
      intoEpic = null;
    }
    for (const el of document.querySelectorAll(".card.drop-target-into")) {
      el.classList.remove("drop-target-into");
    }
    for (const el of document.querySelectorAll(".column-body.drag-over")) {
      el.classList.remove("drag-over");
    }
  };

  const cleanup = () => {
    clearVisuals();
    for (const el of document.querySelectorAll(".card.dragging")) {
      el.classList.remove("dragging");
    }
    dragged = null;
  };

  const rootCardsIn = (colBody) =>
    [...colBody.querySelectorAll(":scope > .card:not(.dragging)")];

  board.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".card");
    if (!card || !card.parentElement.classList.contains("column-body")) return;
    dragged = Number(card.dataset.number);
    e.dataTransfer.effectAllowed = "move";
    try {
      e.dataTransfer.setData("text/plain", String(dragged));
    } catch {
      /* older browsers */
    }
    setTimeout(() => card.classList.add("dragging"), 0);
  });

  board.addEventListener("dragover", (e) => {
    if (dragged === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearVisuals();

    const colBody = e.target.closest(".column-body");
    if (!colBody) return;
    colBody.classList.add("drag-over");

    // "drop into epic" when hovering the middle band of an epic card
    const card = e.target.closest(".card");
    if (
      card &&
      card.classList.contains("epic") &&
      Number(card.dataset.number) !== dragged
    ) {
      const rect = card.getBoundingClientRect();
      const frac = (e.clientY - rect.top) / rect.height;
      if (frac >= INTO_EPIC_TOP && frac <= INTO_EPIC_BOTTOM) {
        intoEpic = Number(card.dataset.number);
        card.classList.add("drop-target-into");
        return;
      }
    }

    // position slot between root cards
    const cards = rootCardsIn(colBody);
    let ref = null;
    for (const c of cards) {
      const r = c.getBoundingClientRect();
      if (e.clientY < r.top + r.height / 2) {
        ref = c;
        break;
      }
    }
    if (!slot) slot = h("div", { class: "drop-slot" });
    if (ref) colBody.insertBefore(slot, ref);
    else colBody.appendChild(slot);
  });

  board.addEventListener("drop", (e) => {
    if (dragged === null) return;
    const number = dragged;
    const colBody = e.target.closest(".column-body");
    let intent = null;
    if (colBody && intoEpic !== null) {
      intent = { type: "into-epic", epicNumber: intoEpic };
    } else if (colBody) {
      intent = {
        type: "position",
        state: colBody.dataset.state,
        before: undefined,
        after: undefined,
      };
      const children = [...colBody.children].filter(
        (el) =>
          (el.classList.contains("card") && !el.classList.contains("dragging")) ||
          el.classList.contains("drop-slot")
      );
      const slotIdx = children.indexOf(slot);
      if (slotIdx >= 0) {
        const prev = children[slotIdx - 1];
        const next = children[slotIdx + 1];
        // A drop in the gap between two cards is "insert after prev" (or,
        // equivalently, "before next"). Send only ONE of the two so we never
        // violate the backend's before/after contract (see #25).
        if (prev && prev.classList.contains("card")) {
          intent.after = Number(prev.dataset.number);
        } else if (next && next.classList.contains("card")) {
          intent.before = Number(next.dataset.number);
        }
      }
    }
    cleanup();
    if (intent) actions.onDrop(number, intent);
  });

  board.addEventListener("dragend", cleanup);
  window.addEventListener("dragleave", (e) => {
    if (e.clientX <= 0 || e.clientY <= 0) cleanup();
  });
}
