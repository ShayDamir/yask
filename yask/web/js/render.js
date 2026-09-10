// Board rendering: columns of cards, epics with nested children.

import { h, clear, fmtEstimate, typeClass } from "./util.js";

export const STATES = ["Backlog", "Todo", "Planning", "In progress", "Review", "Done", "Blocked"];
export const ARCHIVED = "Archived";
export const ALL_STATES = [...STATES, ARCHIVED];

const PREREQ_ICON = `<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M6 2h2v6h6v2H8v6H6V2z" transform="rotate(-90 8 8)"/></svg>`;
const ATTACH_ICON = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M11.5 7 7 11.5a2.4 2.4 0 0 1-3.4-3.4l5-5a4 4 0 0 1 5.6 5.6l-5.5 5.5a5.7 5.7 0 0 1-8-8L7 1.6"/></svg>`;
const DELETE_ICON = `<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M6.5 1.5a1 1 0 0 1 2 0h3a.75.75 0 0 1 .19 1.48L12.5 3h1.25a.75.75 0 1 1 0 1.5h-1l-.92 9.22a1.75 1.75 0 0 1-1.74 1.58H6.92a1.75 1.75 0 0 1-1.74-1.58L4.26 4.5h-1a.75.75 0 0 1 0-1.5h1.25l.81-2.02A1 1 0 0 1 6.5 1.5Zm1.25 2.25a.75.75 0 0 0-1.5 0v6a.75.75 0 0 0 1.5 0v-6Zm2.5 0a.75.75 0 0 0-1.5 0v6a.75.75 0 0 0 1.5 0v-6Zm-5 0a.75.75 0 0 0-1.5 0v6a.75.75 0 0 0 1.5 0v-6Z"/></svg>`;

function estimateBadge(task) {
  if (task.is_epic) {
    const v = fmtEstimate(task.estimate_total);
    return h(
      "span",
      { class: "estimate epic-sum", title: "Sum of estimates of all contained tasks" },
      v === null ? "Σ –" : `Σ ${v}`
    );
  }
  const v = fmtEstimate(task.estimate);
  if (v === null) return null;
  return h("span", { class: "estimate", title: "Story points" }, `${v} pts`);
}

function prereqFlag(task) {
  if (!task.prerequisites.length) return null;
  const names = task.prerequisites
    .map((p) => `#${p.number} ${p.title} (${p.state})`)
    .join("\n");
  return h(
    "span",
    {
      class: `flag${task.has_unmet_prerequisites ? " unmet" : ""}`,
      title: `Prerequisites:\n${names}`,
    },
    h("span", { html: PREREQ_ICON }),
    String(task.prerequisites.length)
  );
}

function attachFlag(task) {
  if (!task.attachments.length) return null;
  return h(
    "span",
    { class: "flag", title: task.attachments.map((a) => a.filename).join("\n") },
    h("span", { html: ATTACH_ICON }),
    String(task.attachments.length)
  );
}

function labelChips(task) {
  if (!task.labels || !task.labels.length) return null;
  return h(
    "span",
    { class: "label-chips" },
    task.labels.map((l) => h("span", { class: "label-chip", title: `Label: ${l.name}` }, l.name))
  );
}

function cardBadges(task) {
  return [
    h("span", { class: `badge ${typeClass(task.type)}` }, task.type),
    estimateBadge(task),
    prereqFlag(task),
    attachFlag(task),
  ].filter(Boolean);
}

function hasLabel(task, name) {
  return (task.labels || []).some((l) => l.name === name);
}

function subtreeHasLabel(task, name) {
  if (hasLabel(task, name)) return true;
  return (task.children || []).some((c) => subtreeHasLabel(c, name));
}

// -- nested children of an epic ------------------------------------------------

function renderChildRow(task, actions, filterLabel) {
  const container = h("div", { class: "child-container" });
  const row = h("div", { class: "child-row", dataset: { number: task.number } });

  if (task.is_epic) {
    let open = false;
    const subWrap = h("div", { class: "sub-children" });
    const renderSub = () => {
      clear(subWrap);
      const kids = filterLabel
        ? (task.children || []).filter((c) => subtreeHasLabel(c, filterLabel))
        : task.children || [];
      for (const c of kids) subWrap.append(renderChildRow(c, actions, filterLabel));
    };
    row.append(
      h("button", {
        class: "sub-toggle",
        title: open ? "Collapse" : "Expand",
        onclick: () => {
          open = !open;
          subWrap.hidden = !open;
          if (open && !subWrap.firstChild) renderSub();
        },
      }, open ? "▾" : "▸")
    );
    container.append(subWrap);
    subWrap.hidden = true;
  }

  row.append(
    ...[
      h("span", { class: "num" }, `#${task.number}`),
      h("span", { class: "title", title: task.title, onclick: () => actions.onEdit(task) }, task.title),
      labelChips(task),
      h("span", { class: `badge ${typeClass(task.type)}` }, task.type),
      estimateBadge(task),
      prereqFlag(task),
      attachFlag(task),
      h(
        "select",
        {
          title: "Move to state",
          onchange: (e) => {
            e.target.value = task.state; // reset until the server confirms
            actions.onMove(task, e.target.value);
          },
        },
        ALL_STATES.map((s) =>
          h("option", { value: s, selected: s === task.state ? "selected" : null }, s)
        )
      ),
    ].filter(Boolean)
  );

  container.append(row);
  return container;
}

export function renderEpicChildren(task, actions, filterLabel) {
  const wrap = h("div", { class: "epic-children" });
  let children = task.children || [];
  if (filterLabel) children = children.filter((c) => subtreeHasLabel(c, filterLabel));
  if (!children.length) {
    wrap.append(
      h("span", { style: "color:var(--text-dim);font-size:max(12px,var(--min-font))" }, "No tasks in this epic yet.")
    );
    return wrap;
  }
  const groups = new Map();
  for (const c of children) {
    if (!groups.has(c.state)) groups.set(c.state, []);
    groups.get(c.state).push(c);
  }
  for (const s of ALL_STATES) {
    if (!groups.has(s)) continue;
    wrap.append(
      h(
        "div",
        { class: "child-group-head" },
        h("span", {}, s),
        h("span", { class: "count" }, String(groups.get(s).length))
      ),
      ...groups.get(s).map((c) => renderChildRow(c, actions, filterLabel))
    );
  }
  return wrap;
}

// -- root cards ----------------------------------------------------------------

export function renderCard(task, actions, { expanded, filterLabel, isArchived } = {}) {
  const card = h(
    "div",
    {
      class: `card${task.state === "Done" ? " done" : ""}${task.state === "Blocked" ? " blocked" : ""}${task.is_epic ? " epic" : ""}`,
      draggable: "true",
      dataset: { number: task.number },
      title: task.description || undefined,
      onclick: (e) => {
        if (e.target.closest("select,button,input")) return;
        actions.onEdit(task);
      },
    }
  );
  card.append(
    ...[
      h(
        "div",
        { class: "card-top" },
        h("span", { class: "num" }, `#${task.number}`),
        h("span", { class: "title" }, task.title),
        isArchived
          ? h("button", {
              type: "button",
              class: "icon-btn danger delete-btn",
              title: `Delete task #${task.number}`,
              "aria-label": `Delete task #${task.number}`,
              onclick: (e) => {
                e.stopPropagation();
                actions.onDelete(task);
              },
            }, h("span", { html: DELETE_ICON }))
          : null,
        task.state === "Done" ? h("span", { class: "done-mark", title: "Done" }, "✓") : null
      ),
      labelChips(task),
      h("div", { class: "card-meta" }, cardBadges(task)),
    ].filter(Boolean)
  );
  if (task.description) {
    card.append(h("div", { class: "card-desc" }, task.description));
  }
  if (task.is_epic) {
    const kids = renderEpicChildren(task, actions, filterLabel);
    if (expanded !== false) card.append(kids);
  }
  return card;
}

// -- columns / board ---------------------------------------------------------------

function renderColumn(colState, roots, actions, filterLabel, opts = {}) {
  const body = h("div", { class: "column-body", dataset: { state: colState } });
  if (!roots.length) {
    body.append(
      h("div", { class: "empty-column" }, colState === ARCHIVED ? "Nothing archived." : "Drop tasks here.")
    );
  }
  for (const t of roots) body.append(renderCard(t, actions, { filterLabel, isArchived: colState === ARCHIVED }));
  const addBtn =
    colState === "Backlog" && opts.showAdd !== false
      ? h("button", {
          class: "add-btn",
          title: `Add task to ${colState}`,
          dataset: { state: colState },
          onclick: (e) => {
            e.stopPropagation();
            actions.onAdd(colState);
          },
        }, "+")
      : null;
  const head = h("div", { class: "column-head" }, h("span", {}, colState), h("span", { class: "count" }, String(roots.length)), addBtn);
  return h("div", { class: `column${colState === ARCHIVED ? " archived" : ""}` }, head, body);
}

// Locate an Epic (by its task number) anywhere in the nested tree; epics may
// be nested, so recurse (#30).
export function findEpic(tasks, number) {
  const n = Number(number); // option values are strings; task numbers are ints (#31)
  for (const t of tasks) {
    if (t.number === n) return t;
    if (t.children) {
      const hit = findEpic(t.children, number); // keep recursing with the raw value
      if (hit) return hit;
    }
  }
  return null;
}

// Board view of a single Epic's direct subtasks as full kanban cards, instead
// of the cramped inline rows inside the epic card (#30). When an epic is
// selected the selector in main.js routes render() here; when not, renderBoard
// renders the normal project board with nested epics.
export function renderEpicBoard(project, epicNumber, actions, { showArchived, filterLabel } = {}) {
  const board = document.getElementById("board");
  clear(board);
  const epic = findEpic(project.tasks, epicNumber);
  if (!epic) {
    board.append(h("div", { class: "empty-column", style: "flex:1;text-align:center" }, "Epic not found."));
    return;
  }
  let children = epic.children || [];
  if (filterLabel) children = children.filter((c) => subtreeHasLabel(c, filterLabel));
  if (!children.length) {
    board.append(h("div", { class: "empty-column", style: "flex:1;text-align:center" }, "This epic has no tasks yet."));
    return;
  }
  const columns = [...STATES];
  if (showArchived) columns.push(ARCHIVED);
  const byState = new Map();
  for (const c of children) {
    if (!byState.has(c.state)) byState.set(c.state, []);
    byState.get(c.state).push(c);
  }
  for (const colState of columns) {
    // Passing showAdd: false suppresses the "+" button — task creation stays at
    // project scope (adding to an epic is a follow-up). renderCard already
    // handles regular tasks and nested epics, so nothing else is needed there.
    board.append(renderColumn(colState, byState.get(colState) || [], actions, filterLabel, { showAdd: false }));
  }
}

export function renderBoard(project, actions, opts) {
  const board = document.getElementById("board");
  clear(board);
  const filterLabel = opts.filterLabel;
  const columns = [...STATES];
  if (opts.showArchived) columns.push(ARCHIVED);
  for (const colState of columns) {
    let roots = project.tasks.filter((t) => t.state === colState);
    if (filterLabel) roots = roots.filter((r) => subtreeHasLabel(r, filterLabel));
    board.append(renderColumn(colState, roots, actions, filterLabel));
  }
}

// -- search results ------------------------------------------------------------------

export function renderSearchResults(project, query, actions, filterLabel) {
  const el = document.getElementById("search-results");
  clear(el);
  const q = query.trim().toLowerCase();
  const flat = [];
  const walk = (tasks) => {
    for (const t of tasks) {
      flat.push(t);
      if (t.children) walk(t.children);
    }
  };
  walk(project.tasks);
  let matches = flat.filter(
    (t) =>
      t.title.toLowerCase().includes(q) ||
      (t.description || "").toLowerCase().includes(q)
  );
  if (filterLabel) matches = matches.filter((t) => hasLabel(t, filterLabel));
  el.append(h("h2", {}, `${matches.length} task${matches.length === 1 ? "" : "s"} matching “${query.trim()}”`));
  if (!matches.length) {
    el.append(h("p", { style: "color:var(--text-dim)" }, "No tasks match. Try a different search."));
    return;
  }
  for (const t of matches) {
    el.append(
      h(
        "div",
        { class: "result-card", onclick: () => actions.onEdit(t) },
        h("span", { class: "num", style: "color:var(--text-dim)" }, `#${t.number}`),
        h("span", { class: "title" }, t.title),
        h("span", { class: `badge ${typeClass(t.type)}` }, t.type),
        estimateBadge(t),
        h("span", { class: `state-chip ${t.state}` }, t.state)
      )
    );
  }
}

// -- sidebar ---------------------------------------------------------------------------

export function renderSidebar(projects, currentId, onSelect) {
  const list = document.getElementById("project-list");
  clear(list);
  for (const p of projects) {
    list.append(
      h(
        "div",
        {
          class: `project-item${p.id === currentId ? " active" : ""}`,
          onclick: () => onSelect(p.id),
        },
        h("span", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, p.name),
        h("span", { class: "count" }, String(p.task_count))
      )
    );
  }
}
