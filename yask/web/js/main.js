// yask web UI — bootstrap, app state, and user actions.

import api from "./api.js";
import { renderBoard, renderSearchResults, renderSidebar } from "./render.js";
import { initDnd } from "./dnd.js";
import { openEditorModal, openNewTaskModal, confirmDialog } from "./dialogs.js";
import { initTheme } from "./theme.js";
import { toast, toastError } from "./toast.js";
import { debounce } from "./util.js";

const state = {
  projects: [],
  currentProjectId: null,
  project: null,
  search: "",
  showArchived: false,
};

const $ = (id) => document.getElementById(id);

// -- data loading -------------------------------------------------------------

async function loadProjects() {
  state.projects = await api.listProjects();
  renderSidebar(state.projects, state.currentProjectId, selectProject);
}

async function selectProject(pid) {
  state.currentProjectId = pid;
  state.project = await api.getProject(pid);
  await loadProjects();
  render();
}

async function refresh() {
  if (state.currentProjectId === null) return;
  const [project, projects] = await Promise.all([
    api.getProject(state.currentProjectId),
    api.listProjects(),
  ]);
  state.project = project;
  state.projects = projects;
  render();
}

// -- rendering -------------------------------------------------------------------

function render() {
  $("project-name").textContent =
    state.project?.name ?? "yask — yet another simple kanban";
  const hasProjects = state.projects.length > 0;
  const hasProject = state.project !== null;
  $("board").hidden = !hasProject || state.search.trim() !== "";
  $("search-results").hidden = !(hasProject && state.search.trim() !== "");
  $("empty-state").hidden = hasProjects;
  if (!hasProjects) {
    $("empty-state-text").textContent =
      "No projects yet. Create one in the sidebar to get started.";
  }
  if (!hasProject) return;
  if (state.search.trim() !== "") {
    renderSearchResults(state.project, state.search, actions);
    return;
  }
  renderBoard(state.project, actions, { showArchived: state.showArchived });
}

// -- task lookup --------------------------------------------------------------------

function findTask(tasks, number) {
  for (const t of tasks) {
    if (t.number === number) return t;
    if (t.children) {
      const hit = findTask(t.children, number);
      if (hit) return hit;
    }
  }
  return null;
}

function columnOrder(stateName) {
  return state.project.tasks
    .filter((t) => t.state === stateName)
    .map((t) => t.number);
}

function computeNewOrder(order, moved, before, after) {
  const rest = order.filter((n) => n !== moved);
  let idx = rest.length;
  if (before !== undefined) {
    idx = Math.max(0, rest.indexOf(before));
    rest.splice(idx, 0, moved);
  } else if (after !== undefined) {
    idx = rest.indexOf(after);
    idx = idx === -1 ? rest.length : idx + 1;
    rest.splice(idx, 0, moved);
  } else {
    rest.push(moved);
  }
  return rest;
}

// -- actions -------------------------------------------------------------------------

const actions = {
  refresh,
  onEdit: (task) =>
    openEditorModal(state.project, task, {
      refresh,
      onArchive: (t) => doArchive(t),
      onRestore: (t) => doRestore(t),
      onDelete: (t) => doDelete(t),
    }),
  onAdd: (colState) =>
    openNewTaskModal(state.project, colState, () => {
      toast(`Created task in ${colState}`, "success");
      refresh();
    }),
  onMove: (task, toState) => doMove(task, toState),
  onArchive: (task) => doArchive(task),
  onRestore: (task) => doRestore(task),
  onDelete: (task) => doDelete(task),
  onDrop: (number, intent) => handleDrop(number, intent),
};

async function doMove(task, toState, position = {}) {
  if (task.state === toState) return;
  try {
    const res = await api.moveTask(state.project.id, task.number, toState, position);
    toast(`Moved #${task.number} to ${toState}`, "success");
  } catch (err) {
    if (err.needsConfirmation) {
      const ok = await confirmDialog({
        title: `Move #${task.number} to ${toState}?`,
        message:
          "This also moves its prerequisites that have not reached this stage yet.",
        affected: err.affected,
        confirmLabel: "Move all",
      });
      if (!ok) return;
      await api.moveTask(state.project.id, task.number, toState, {
        ...position,
        confirm: true,
      });
      toast(`Moved ${err.affected.length} tasks to ${toState}`, "success");
    } else {
      toastError(err);
      return;
    }
  }
  await refresh();
}

async function doArchive(task) {
  try {
    await api.archiveTask(state.project.id, task.number);
    toast(`Archived #${task.number}`, "success");
  } catch (err) {
    if (err.needsConfirmation) {
      const ok = await confirmDialog({
        title: `Archive epic #${task.number}?`,
        message: "Archiving an epic also archives everything inside it.",
        affected: err.affected,
        confirmLabel: "Archive all",
      });
      if (!ok) return;
      await api.archiveTask(state.project.id, task.number, true);
      toast(`Archived ${err.affected.length} tasks`, "success");
    } else {
      toastError(err);
      return;
    }
  }
  await refresh();
}

async function doRestore(task) {
  try {
    const res = await api.restoreTask(state.project.id, task.number);
    toast(
      `Restored #${task.number} to ${res.affected[0]?.to ?? "Backlog"}`,
      "success"
    );
  } catch (err) {
    if (err.needsConfirmation) {
      const ok = await confirmDialog({
        title: `Restore #${task.number}?`,
        message: "This also moves its prerequisites that are behind the target stage.",
        affected: err.affected,
        confirmLabel: "Restore all",
      });
      if (!ok) return;
      await api.restoreTask(state.project.id, task.number, "Backlog", true);
      toast(`Restored ${err.affected.length} tasks`, "success");
    } else {
      toastError(err);
      return;
    }
  }
  await refresh();
}

async function doDelete(task) {
  const ok = await confirmDialog({
    title: `Delete task #${task.number}?`,
    message: task.is_epic
      ? "This permanently deletes the epic and every task inside it."
      : "This permanently deletes the task. This cannot be undone.",
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.deleteTask(state.project.id, task.number, true);
    toast(`Deleted #${task.number}`, "success");
  } catch (err) {
    toastError(err);
    return;
  }
  await refresh();
}

async function handleDrop(number, intent) {
  const task = findTask(state.project.tasks, number);
  if (!task) return;
  const pid = state.project.id;

  if (intent.type === "into-epic") {
    if (task.parent_number === intent.epicNumber) return;
    try {
      await api.updateTask(pid, number, { parent_number: intent.epicNumber });
      toast(`Moved #${number} into epic #${intent.epicNumber}`, "success");
    } catch (err) {
      toastError(err);
      await refresh();
      return;
    }
    await refresh();
    return;
  }

  // position intent (before/after/end within a column)
  const order = columnOrder(intent.state);
  const newOrder = computeNewOrder(order, number, intent.before, intent.after);
  const sameOrder =
    order.length === newOrder.length &&
    order.every((n, i) => n === newOrder[i]);
  if (sameOrder && task.state === intent.state) return; // no-op drop

  if (task.state === intent.state) {
    try {
      await api.reorderTask(pid, number, {
        before: intent.before,
        after: intent.after,
      });
    } catch (err) {
      toastError(err);
      await refresh();
      return;
    }
    await refresh();
    return;
  }

  await doMove(task, intent.state, {
    before: intent.before,
    after: intent.after,
  });
}

// -- wiring ---------------------------------------------------------------------------

function init() {
  initTheme();

  $("search").addEventListener(
    "input",
    debounce((e) => {
      state.search = e.target.value;
      render();
    }, 120)
  );

  $("show-archived").addEventListener("change", (e) => {
    state.showArchived = e.target.checked;
    render();
  });

  $("new-project-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("new-project-name");
    const name = input.value.trim();
    if (!name) return;
    try {
      const p = await api.createProject(name);
      input.value = "";
      toast(`Created project “${name}”`, "success");
      await selectProject(p.id);
    } catch (err) {
      toastError(err);
    }
  });

  initDnd(actions);

  loadProjects().then(() => {
    render();
    const first = state.projects[0];
    if (first) selectProject(first.id);
  });
}

init();
