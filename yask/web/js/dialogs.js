// Modals: task editor, new task, confirmation, attachment viewer.

import api from "./api.js";
import { h, clear, fmtEstimate, fmtBytes, fmtTime, typeClass } from "./util.js";
import { renderMarkdown } from "./markdown.js";
import { toast, toastError } from "./toast.js";

// -- per-project preferences (localStorage) ----------------------------------------------

const LAST_TYPE_KEY = (pid) => `yask.last-type.${pid}`;

function getLastType(pid) {
  try {
    return localStorage.getItem(LAST_TYPE_KEY(pid));
  } catch {
    return null;
  }
}

function setLastType(pid, type) {
  try {
    localStorage.setItem(LAST_TYPE_KEY(pid), type);
  } catch {
    /* ignore quota / private-mode failures */
  }
}

// -- modal plumbing ----------------------------------------------------------

let modalCount = 0;

function closeModal(modal) {
  modal.remove();
  modalCount--;
  if (modalCount === 0) document.body.style.overflow = "";
}

export function openModal(content, { small = false } = {}) {
  const backdrop = h("div", { class: "modal-backdrop" });
  const modal = h("div", { class: `modal${small ? " small" : ""}` }, content);
  backdrop.append(modal);
  const onBackdropClick = (e) => {
    if (e.target === backdrop) closeModal(backdrop);
  };
  backdrop.addEventListener("click", onBackdropClick);
  const onKey = (e) => {
    if (e.key === "Escape") {
      document.removeEventListener("keydown", onKey, true);
      closeModal(backdrop);
    }
  };
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("click", () =>
    document.removeEventListener("keydown", onKey, true)
  );
  modalCount++;
  document.body.style.overflow = "hidden";
  document.getElementById("modal-root").append(backdrop);
  return {
    el: backdrop,
    close: () => {
      document.removeEventListener("keydown", onKey, true);
      closeModal(backdrop);
    },
  };
}

// -- confirmation --------------------------------------------------------------

export function confirmDialog({
  title,
  message = "",
  affected = [],
  confirmLabel = "Confirm",
  danger = false,
}) {
  return new Promise((resolve) => {
    const actions = h(
      "div",
      { class: "modal-actions" },
      h("span", { class: "spacer" }),
      h("button", { class: "btn ghost", id: "confirm-cancel" }, "Cancel"),
      h("button", { class: `btn${danger ? " danger" : ""}`, id: "confirm-ok" }, confirmLabel),
    );
    const body = [
      h("h2", {}, title),
      message ? h("p", { style: "margin:0;color:var(--text-dim)" }, message) : null,
    ];
    if (affected.length) {
      body.push(h("p", { style: "margin:12px 0 0;font-size:max(13px,var(--min-font))" }, "This also affects:"));
      body.push(
        h(
          "ul",
          { class: "affected-list" },
          affected.map((a) =>
            h(
              "li",
              {},
              h("span", { class: "num", style: "color:var(--text-dim)" }, `#${a.number}`),
              h("span", {}, a.title || ""),
              h(
                "span",
                { class: "arrow", style: "margin-left:auto" },
                `${a.from ?? "—"} → ${a.to}`
              )
            )
          )
        )
      );
    }
    body.push(actions);
    const modal = openModal(h("div", {}, ...body), { small: true });
    const done = (value) => {
      modal.close();
      resolve(value);
    };
    modal.el.querySelector("#confirm-cancel").onclick = () => done(false);
    modal.el.querySelector("#confirm-ok").onclick = () => done(true);
    modal.el.addEventListener("click", (e) => {
      if (e.target === modal.el) done(false);
    });
  });
}

// -- helpers over the project tree ---------------------------------------------

export function flattenTasks(tasks, out = []) {
  for (const t of tasks) {
    out.push(t);
    if (t.children && t.children.length) flattenTasks(t.children, out);
  }
  return out;
}

function epicsOf(project, excludeIds) {
  return flattenTasks(project.tasks).filter(
    (t) => t.is_epic && !excludeIds.has(t.id)
  );
}

// descendants of a task (to exclude from parent selection)
function descendantIds(task) {
  const ids = new Set();
  const walk = (t) => {
    for (const c of t.children || []) {
      ids.add(c.id);
      walk(c);
    }
  };
  walk(task);
  return ids;
}

// -- attachment viewer -----------------------------------------------------------

export async function openAttachmentViewer(task, att) {
  const modal = openModal(
    h("div", {}, h("h2", {}, att.filename), h("div", { id: "viewer-body" }, "Loading…"))
  );
  try {
    const res = await fetch(api.attachmentUrl(att.id));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    clear(modal.el.querySelector("#viewer-body"));
    if (att.content_type.startsWith("image/")) {
      const url = URL.createObjectURL(blob);
      modal.el.querySelector("#viewer-body").append(h("img", { class: "viewer-img", src: url }));
    } else {
      const text = await blob.text();
      modal.el
        .querySelector("#viewer-body")
        .append(h("div", { class: "viewer-md", html: renderMarkdown(text) }));
    }
  } catch (err) {
    clear(modal.el.querySelector("#viewer-body"));
    modal.el.querySelector("#viewer-body").textContent = `Could not load attachment: ${err.message}`;
  }
}

// -- new task -------------------------------------------------------------------

export function openNewTaskModal(project, state, onCreated) {
  const pid = project.id;
  const titleInput = h("input", { type: "text", id: "nt-title", placeholder: "Task title" });
  const types = project.task_types || [];
  // Default to the last type created in this project (see #6), else Task, else the first type.
  const lastType = getLastType(pid);
  const defaultType =
    types.some((t) => t.name === lastType) ? lastType :
    types.some((t) => t.name === "Task") ? "Task" :
    types[0]?.name ?? "";
  const typeSelect = h(
    "select",
    { id: "nt-type" },
    types.map((t) => h("option", { value: t.name }, t.name))
  );
  typeSelect.value = defaultType;
  const estInput = h("input", { type: "number", id: "nt-est", min: "0", step: "0.5", placeholder: "—" });
  const syncEpic = () => {
    const isEpic = types.find((t) => t.name === typeSelect.value)?.is_epic;
    estInput.disabled = !!isEpic;
  };
  typeSelect.addEventListener("change", syncEpic);
  syncEpic();

  // User story pattern (see #5): three-field form composing an
  // "As a …, I want …, so that …." description. Auto-shown only when the
  // selected type is "Story" (#20); hidden for every other type.
  // (#7) When the project has preset roles, "As a" is a dropdown of them;
  // otherwise it stays a free-text field so the pattern still works.
  const projectRoles =
    project && Array.isArray(project.roles) ? project.roles : [];
  const useRoleSelect = projectRoles.length > 0;
  const usAs = useRoleSelect
    ? h(
        "select",
        { id: "nt-us-as" },
        projectRoles.map((r) => h("option", { value: r.name }, r.name))
      )
    : h("input", { type: "text", id: "nt-us-as", placeholder: "a role or user" });
  const usWant = h("input", { type: "text", id: "nt-us-want", placeholder: "an action or capability" });
  const usSo = h("input", { type: "text", id: "nt-us-so", placeholder: "a benefit or reason" });
  const usFields = h(
    "div",
    { class: "us-fields", hidden: "" },
    h("div", { class: "field" }, h("label", {}, "As a", usAs)),
    h("div", { class: "field" }, h("label", {}, "I want", usWant)),
    h("div", { class: "field" }, h("label", {}, "So that", usSo))
  );
  // Show the pattern only when the selected type is "Story" (#20).
  const syncStoryPattern = () => {
    usFields.hidden = typeSelect.value !== "Story";
  };
  typeSelect.addEventListener("change", syncStoryPattern);
  syncStoryPattern(); // initial state

  const form = h(
    "form",
    {
      id: "nt-form",
      onsubmit: async (e) => {
        e.preventDefault();
        let title = titleInput.value.trim();
        const isStory = typeSelect.value === "Story";
        let description = "";
        if (isStory) {
          const as = usAs.value.trim();
          const want = usWant.value.trim();
          const so = usSo.value.trim();
          const clauses = [];
          if (as) clauses.push(`As a ${as}`);
          if (want) clauses.push(`I want ${want}`);
          if (so) clauses.push(`So that ${so}`);
          if (clauses.length) description = clauses.join(", ") + ".";
          if (!title) title = want || "User story";
        }
        if (!title) return;
        const isEpic = types.find((t) => t.name === typeSelect.value)?.is_epic;
        const est = isEpic ? null : estInput.value === "" ? null : Number(estInput.value);
        try {
          // new tasks always land in the Backlog (#1); no state field
          await api.createTask(pid, {
            title,
            type: typeSelect.value,
            estimate: est,
            description: isStory ? description : "",
          });
          setLastType(pid, typeSelect.value);
          modal.close();
          if (onCreated) onCreated();
        } catch (err) {
          toastError(err);
        }
      },
    },
    h("h2", {}, `New task in ${state}`),
    h("div", { class: "field" }, titleInput),
    h(
      "div",
      { class: "field-row" },
      h("div", { class: "field" }, h("label", {}, "Type"), typeSelect),
      h("div", { class: "field" }, h("label", {}, "Story points"), estInput)
    ),
    usFields,
    h(
      "div",
      { class: "modal-actions" },
      h("span", { class: "spacer" }),
      h("button", { type: "submit", class: "btn" }, "Create")
    )
  );
  const modal = openModal(form, { small: true });
  setTimeout(() => titleInput.focus(), 0);
  return modal;
}

// -- task editor -------------------------------------------------------------------

export function openEditorModal(project, task, actions) {
  const pid = project.id;

  const titleInput = h("input", { type: "text", id: "ed-title", value: task.title });
  const descInput = h("textarea", { id: "ed-desc" }, task.description || "");

  const allTypes = project.task_types || [];
  const typeSelect = h(
    "select",
    { id: "ed-type" },
    allTypes.map((t) =>
      h("option", { value: t.name, selected: t.name === task.type ? "selected" : null }, t.name)
    )
  );
  const estInput = h("input", {
    type: "number",
    id: "ed-est",
    min: "0",
    step: "0.5",
    value: task.estimate === null || task.estimate === undefined ? "" : task.estimate,
  });
  const syncEstimate = () => {
    const isEpic = allTypes.find((t) => t.name === typeSelect.value)?.is_epic;
    estInput.disabled = !!isEpic;
  };
  typeSelect.addEventListener("change", syncEstimate);
  syncEstimate();

  // parent epic (only epics may contain tasks)
  const excluded = new Set([task.id, ...descendantIds(task)]);
  const epics = epicsOf(project, excluded);
  const parentSelect = h(
    "select",
    { id: "ed-parent" },
    h("option", { value: "" }, "(none — top level)"),
    epics.map((e) =>
      h("option", { value: String(e.number), selected: task.parent_number === e.number ? "selected" : null },
        `#${e.number} ${e.title}`)
    )
  );

  // prerequisites
  const others = flattenTasks(project.tasks).filter(
    (t) => t.id !== task.id && t.state !== "Done" && t.state !== "Archived"
  );
  const chosen = new Set(task.prerequisites.map((p) => p.number));
  const checkList = h(
    "div",
    { class: "check-list" },
    others.map((t) => {
      const label = h(
        "label",
        {},
        h("input", { type: "checkbox", value: String(t.number), checked: chosen.has(t.number) ? "checked" : null }),
        h("span", { class: "num", style: "color:var(--text-dim)" }, `#${t.number}`),
        h("span", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, t.title),
        h("span", { class: `state-chip ${t.state}` }, t.state)
      );
      return label;
    })
  );
  if (!others.length) checkList.append(h("span", { style: "color:var(--text-dim);font-size:max(12px,var(--min-font))" }, "No other tasks in this project."));

  // attachments
  const attList = h("div", { class: "attachment-list" });
  const fileInput = h("input", { type: "file", id: "ed-file", accept: ".md,.markdown,.txt,image/*", style: "max-width:100%" });
  const renderAttachments = (list) => {
    clear(attList);
    if (!list.length) {
      attList.append(h("span", { style: "color:var(--text-dim);font-size:max(12px,var(--min-font))" }, "No attachments."));
      return;
    }
    for (const a of list) {
      attList.append(
        h(
          "div",
          { class: "attachment-item" },
          a.content_type.startsWith("image/") ? h("span", {}, "▣") : h("span", {}, "✎"),
          h("span", { class: "name", title: a.filename, onclick: () => openAttachmentViewer(task, a) }, a.filename),
          h("span", { class: "size" }, fmtBytes(a.size)),
          h("button", {
            class: "del",
            title: "Delete attachment",
            onclick: async () => {
              try {
                await api.deleteAttachment(a.id);
                renderAttachments(list.filter((x) => x.id !== a.id));
              } catch (err) {
                toastError(err);
              }
            },
          }, "✕")
        )
      );
    }
  };
  renderAttachments(task.attachments);
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    try {
      await api.uploadAttachment(pid, task.number, file);
      const fresh = await api.getTask(pid, task.number);
      renderAttachments(fresh.attachments);
      toast(`Attached ${file.name}`, "success");
    } catch (err) {
      toastError(err);
    }
    fileInput.value = "";
  });

  // labels
  const labelList = h("div", { class: "check-list" });
  const newLabelInput = h("input", { type: "text", id: "ed-new-label", placeholder: "New label…" });
  // Seed from the task's own labels (already serialized on the task) so the
  // save below never clears labels even if the project label fetch fails.
  let projectLabels = (task.labels || []).map((l) => ({ id: l.id, name: l.name, color: l.color || "" }));
  const chosenLabelIds = new Set(projectLabels.map((l) => l.id));
  // Snapshot the task's label ids as loaded, before any checkbox toggles. The
  // checkboxes mutate `chosenLabelIds` on every change (see below), so comparing
  // the checked boxes against `chosenLabelIds` at save time is always equal and
  // never persists — this snapshot is the correct baseline for change detection.
  const initialLabelIds = new Set(projectLabels.map((l) => l.id));
  const renderLabels = () => {
    clear(labelList);
    if (!projectLabels.length) {
      labelList.append(h("span", { style: "color:var(--text-dim);font-size:max(12px,var(--min-font))" }, "No labels in this project yet."));
      return;
    }
    for (const l of projectLabels) {
      const cb = h("input", { type: "checkbox", value: String(l.id), checked: chosenLabelIds.has(l.id) ? "checked" : null });
      cb.addEventListener("change", () => {
        if (cb.checked) chosenLabelIds.add(l.id);
        else chosenLabelIds.delete(l.id);
      });
      // color swatch — independent of the checkbox; persists the color only
      // when the user actually picks one.
      const colorInput = h("input", {
        type: "color",
        class: "label-color",
        value: (l.color && l.color.length >= 7) ? l.color : "#888888",
        title: "Label color (empty means none)",
        "aria-label": `Color for label “${l.name}”`,
      });
      colorInput.addEventListener("change", async () => {
        const color = colorInput.value;
        try {
          await api.updateLabel(pid, l.id, color);
          l.color = color;
          if (actions && typeof actions.onLabelUpdated === "function") {
            await actions.onLabelUpdated();
          }
          renderLabels();
        } catch (err) {
          toastError(err);
        }
      });
      labelList.append(
        h(
          "label",
          {},
          cb,
          h("span", { class: "label-chip" }, l.name),
          colorInput,
          h(
            "button",
            {
              type: "button",
              class: "label-del",
              title: `Delete label “${l.name}”`,
              "aria-label": `Delete label “${l.name}”`,
              onclick: (e) => {
                e.stopPropagation();
                deleteLabel(l);
              },
            },
            "✕"
          )
        )
      );
    }
  };
  renderLabels();
  (async () => {
    try {
      const all = await api.listLabels(pid);
      for (const l of all) {
        if (!projectLabels.some((x) => x.id === l.id)) {
          projectLabels.push(l);
          renderLabels();
        }
      }
    } catch {
      /* non-fatal: the task's own labels are already shown */
    }
  })();
  const addLabelBtn = h("button", { type: "button", class: "btn" }, "Add");
  const newLabelColorInput = h("input", {
    type: "color",
    class: "label-color",
    value: "#888888",
    title: "Color for the new label (empty means none)",
    "aria-label": "Color for the new label",
  });
  const createLabel = async () => {
    const name = newLabelInput.value.trim();
    if (!name) return;
    const color = newLabelColorInput.value;
    try {
      const l = await api.createLabel(pid, name, color);
      chosenLabelIds.delete(l.id); // not auto-applied until save
      projectLabels.push(l);
      renderLabels();
      newLabelInput.value = "";
      newLabelColorInput.value = "#888888";
      toast(`Label “${name}” created`, "success");
    } catch (err) {
      toastError(err);
    }
  };
  async function deleteLabel(l) {
    const ok = await confirmDialog({
      title: `Delete label “${l.name}”?`,
      message: "This removes the label from all tasks in this project.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.deleteLabel(pid, l.id);
      projectLabels = projectLabels.filter((x) => x.id !== l.id);
      chosenLabelIds.delete(l.id);
      renderLabels();
      const n = res.detached_tasks ?? 0;
      const where = n === 1 ? "1 task" : `${n} tasks`;
      toast(`Deleted label “${l.name}” (removed from ${where})`, "success");
      if (actions && typeof actions.onLabelDeleted === "function") {
        await actions.onLabelDeleted(l.name);
      }
    } catch (err) {
      toastError(err);
    }
  }
  // Enter in the new-label field must create the label, not save the editor form.
  newLabelInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      createLabel();
    }
  });
  addLabelBtn.addEventListener("click", createLabel);

  // history
  const historyList = h("ul", { class: "history-list" });
  (async () => {
    try {
      const hist = await api.getHistory(pid, task.number);
      for (const e of hist.slice(-12).reverse()) {
        historyList.append(
          h(
            "li",
            {},
            h("span", {}, `${e.from_state ?? "created"} → `),
            h("span", { class: "to" }, e.to_state),
            h("span", { class: "src" }, "· " + (e.source ?? "")),
            h("span", { class: "ts" }, fmtTime(e.changed_at))
          )
        );
      }
    } catch {
      /* non-fatal */
    }
  })();

  const isArchived = task.state === "Archived";

  const form = h(
    "form",
    {
      id: "ed-form",
      onsubmit: async (e) => {
        e.preventDefault();
        const saveBtn = form.querySelector("#ed-save");
        saveBtn.disabled = true;
        try {
          const updates = {
            title: titleInput.value.trim(),
            description: descInput.value,
            type: typeSelect.value,
          };
          if (!estInput.disabled) {
            updates.estimate = estInput.value === "" ? null : Number(estInput.value);
          }
          const parentVal = parentSelect.value;
          updates.parent_number = parentVal === "" ? null : Number(parentVal);
          if (updates.title === "") throw new Error("Title must not be empty");
          await api.updateTask(pid, task.number, updates);
          // Prereqs that are Done/Archived are filtered out of `others` and thus
          // have no checkbox; they were already chosen at load, so preserve them
          // (plan: existing assignments must remain intact — don't silently drop
          // them on Save).
          const otherNumbers = new Set(others.map((t) => t.number));
          const hiddenChosen = [...chosen].filter((n) => !otherNumbers.has(n));
          const visibleChecked = [...checkList.querySelectorAll("input:checked")].map(
            (c) => Number(c.value)
          );
          const checked = [...new Set([...visibleChecked, ...hiddenChosen])].sort(
            (a, b) => a - b
          );
          if (checked.join() !== [...chosen].sort((a, b) => a - b).join()) {
            await api.setPrerequisites(pid, task.number, checked);
          }
          const checkedLabelIds = [...labelList.querySelectorAll("input:checked")].map((c) => Number(c.value)).sort((a, b) => a - b);
          const initialSorted = [...initialLabelIds].sort((a, b) => a - b);
          if (checkedLabelIds.join() !== initialSorted.join()) {
            await api.setTaskLabels(pid, task.number, checkedLabelIds);
          }
          toast(`Task #${task.number} saved`, "success");
          modal.close();
          await actions.refresh();
        } catch (err) {
          toastError(err);
          saveBtn.disabled = false;
        }
      },
    },
    h("h2", {}, `Task #${task.number}`),
    h("div", { class: "field" }, h("label", {}, "Title"), titleInput),
    h(
      "div",
      { class: "field-row" },
      h("div", { class: "field" }, h("label", {}, "Type"), typeSelect),
      h(
        "div",
        { class: "field" },
        h("label", {}, "Story points"),
        estInput,
        h("div", { class: "hint" }, "Epics have no estimate — they sum their contained tasks.")
      )
    ),
    h("div", { class: "field" }, h("label", {}, "Description"), descInput),
    h("div", { class: "field" }, h("label", {}, "Parent epic"), parentSelect),
    h("div", { class: "section-title" }, "Prerequisites"),
    checkList,
    h("div", { class: "hint", style: "margin-top:5px" }, "Moving a task forward also moves prerequisites still behind the target stage."),
    h("div", { class: "section-title" }, "Attachments"),
    fileInput,
    attList,
    h("div", { class: "section-title" }, "Labels"),
    labelList,
    h(
      "div",
      { class: "field-row" },
      newLabelInput,
      newLabelColorInput,
      addLabelBtn
    ),
    h("div", { class: "section-title" }, "History"),
    historyList,
    h(
      "div",
      { class: "modal-actions" },
      isArchived
        ? h("button", {
            class: "btn",
            type: "button",
            onclick: async () => {
              modal.close();
              actions.onRestore(task);
            },
          }, "Restore to Backlog")
        : h("button", {
            class: "btn ghost",
            type: "button",
            onclick: async () => {
              modal.close();
              actions.onArchive(task);
            },
          }, "Archive"),
      isArchived
        ? h("button", {
            class: "btn danger",
            type: "button",
            onclick: async () => {
              modal.close();
              actions.onDelete(task);
            },
          }, "Delete")
        : null,
      h("span", { class: "spacer" }),
      h("button", { class: "btn ghost", type: "button", onclick: () => modal.close() }, "Cancel"),
      h("button", { class: "btn", type: "submit", id: "ed-save" }, "Save")
    )
  );

  const modal = openModal(form);
  setTimeout(() => titleInput.focus(), 0);
  return modal;
}
