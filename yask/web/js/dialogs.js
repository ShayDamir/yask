// Modals: task editor, new task, confirmation, attachment viewer.

import api from "./api.js";
import { DEFAULT_STATE, FIELD_LIMITS } from "./constants.js";
import { h, clear, fmtEstimate, fmtBytes, fmtTime, typeClass, walkTasks } from "./util.js";
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
      message ? h("p", { class: "confirm-message" }, message) : null,
    ];
    if (affected.length) {
      body.push(h("p", { class: "affects-head" }, "This also affects:"));
      body.push(
        h(
          "ul",
          { class: "affected-list" },
          affected.map((a) =>
            h(
              "li",
              {},
              h("span", { class: "num dim" }, `#${a.number}`),
              h("span", {}, a.title || ""),
              h(
                "span",
                { class: "arrow" },
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

export function openNewTaskModal(project, state, onCreated, epicNumber = null) {
  const pid = project.id;
  // Epic context (#35): resolve the epic so the modal heading + parent field
  // can reference it. Falls back to null for the normal (project-scope) flow.
  const epic = epicNumber
    ? walkTasks(project.tasks).find((t) => t.number === Number(epicNumber))
    : null;
  const heading = epic
    ? `New task in epic #${epic.number}`
    : `New task in ${state}`;
  const titleInput = h("input", {
    type: "text", id: "nt-title", placeholder: "Task title",
    maxlength: FIELD_LIMITS.title,
  });
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
    : h("input", { type: "text", id: "nt-us-as", placeholder: "a role or user",
                   maxlength: FIELD_LIMITS.title });
  const usWant = h("input", { type: "text", id: "nt-us-want", placeholder: "an action or capability",
                              maxlength: FIELD_LIMITS.title });
  const usSo = h("input", { type: "text", id: "nt-us-so", placeholder: "a benefit or reason",
                            maxlength: FIELD_LIMITS.title });
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
          // new tasks always land in the Backlog (#1); no state field.
          // When creating inside an epic (#35), nest it under that epic.
          const payload = {
            title,
            type: typeSelect.value,
            estimate: est,
            description: isStory ? description : "",
          };
          if (epic) payload.parent_number = Number(epicNumber);
          await api.createTask(pid, payload);
          setLastType(pid, typeSelect.value);
          modal.close();
          if (onCreated) onCreated();
        } catch (err) {
          toastError(err);
        }
      },
    },
    h("h2", {}, heading),
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
//
// openEditorModal is a thin composer: each section is built by a dedicated
// builder that owns its own DOM nodes, state, and event handlers.

function buildTypeEstimateRow(task, allTypes) {
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
  return { typeSelect, estInput };
}

function buildParentRow(project, task) {
  // Parent epic (only epics may contain tasks). Excludes the task itself and
  // its descendants so a task can never be parented under itself.
  const excluded = new Set([task.id, ...walkTasks(task.children).map((t) => t.id)]);
  const epics = walkTasks(project.tasks).filter((t) => t.is_epic && !excluded.has(t.id));
  const parentSelect = h(
    "select",
    { id: "ed-parent" },
    h("option", { value: "" }, "(none — top level)"),
    epics.map((e) =>
      h("option", { value: String(e.number), selected: task.parent_number === e.number ? "selected" : null },
        `#${e.number} ${e.title}`)
    )
  );
  return parentSelect;
}

function buildPrereqSection(project, task) {
  const others = walkTasks(project.tasks).filter(
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
        h("span", { class: "num dim" }, `#${t.number}`),
        h("span", { class: "ellipsis" }, t.title),
        h("span", { class: `state-chip ${t.state}` }, t.state)
      );
      return label;
    })
  );
  if (!others.length) checkList.append(h("span", { class: "dim-sm" }, "No other tasks in this project."));
  return { checkList, others, chosen };
}

function buildAttachmentsSection(pid, task) {
  const attList = h("div", { class: "attachment-list" });
  const fileInput = h("input", { type: "file", id: "ed-file", accept: ".md,.markdown,.txt,image/*" });
  const renderAttachments = (list) => {
    clear(attList);
    if (!list.length) {
      attList.append(h("span", { class: "dim-sm" }, "No attachments."));
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
  return { fileInput, attList };
}

function buildLabelsSection(pid, task, actions) {
  const labelList = h("div", { class: "check-list" });
  const newLabelInput = h("input", {
    type: "text", id: "ed-new-label", placeholder: "New label…",
    maxlength: FIELD_LIMITS.labelName,
  });
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
      labelList.append(h("span", { class: "dim-sm" }, "No labels in this project yet."));
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
  return { labelList, newLabelInput, newLabelColorInput, addLabelBtn, initialLabelIds };
}

function buildHistorySection(pid, task) {
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
  return { historyList };
}

export function openEditorModal(project, task, actions) {
  const pid = project.id;

  const titleInput = h("input", {
    type: "text", id: "ed-title", value: task.title, maxlength: FIELD_LIMITS.title,
  });
  const descInput = h("textarea", { id: "ed-desc", maxlength: FIELD_LIMITS.description }, task.description || "");

  const allTypes = project.task_types || [];
  const { typeSelect, estInput } = buildTypeEstimateRow(task, allTypes);
  const parentSelect = buildParentRow(project, task);

  const { checkList, others, chosen } = buildPrereqSection(project, task);

  const { fileInput, attList } = buildAttachmentsSection(pid, task);

  const { labelList, newLabelInput, newLabelColorInput, addLabelBtn, initialLabelIds } =
    buildLabelsSection(pid, task, actions);

  const { historyList } = buildHistorySection(pid, task);

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
    h("div", { class: "hint" }, "Moving a task forward also moves prerequisites still behind the target stage."),
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
          }, `Restore to ${DEFAULT_STATE}`)
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

// -- telegram users (the bot's password allowlist) ------------------------------

// Per-user visible projects (task #135): a nested dialog over the Telegram
// users list — one checkbox per project plus a "No restriction" checkbox.
// Save posts the checked ids (or [] when unrestricted — an empty list clears
// the restriction, the chat sees all projects again).
function openTelegramUserProjectsModal(chatId, onSaved) {
  const listEl = h("div", { class: "check-list" });
  const noRestrict = h("input", { type: "checkbox", id: "tgproj-no-restrict" });
  // When "No restriction" is on, the per-project checkboxes are disabled and
  // unchecked (there is nothing to pick); turning it off re-enables them.
  const syncDisabled = () => {
    for (const cb of listEl.querySelectorAll("input[type='checkbox']")) {
      cb.disabled = noRestrict.checked;
      if (noRestrict.checked) cb.checked = false;
    }
  };
  noRestrict.addEventListener("change", syncDisabled);

  (async () => {
    try {
      const [projects, userProjects] = await Promise.all([
        api.listProjects(),
        api.listTelegramUserProjects(chatId),
      ]);
      const chosen = new Set(userProjects.map((p) => p.id));
      for (const p of projects) {
        const cb = h("input", {
          type: "checkbox",
          value: String(p.id),
          checked: chosen.has(p.id) ? "checked" : null,
        });
        listEl.append(
          h("label", {}, cb, h("span", {}, p.name), h("span", { class: "dim" }, `(${p.id})`))
        );
      }
      if (!projects.length) {
        listEl.append(h("span", { class: "dim-sm" }, "No projects on the board."));
      }
      // No rows at all = unrestricted: check "No restriction" up front.
      noRestrict.checked = userProjects.length === 0;
      syncDisabled();
    } catch (err) {
      listEl.append(
        h("span", { class: "dim" }, `Could not load projects: ${err.message}`)
      );
    }
  })();

  const form = h(
    "form",
    {
      id: "tgproj-form",
      onsubmit: async (e) => {
        e.preventDefault();
        const saveBtn = form.querySelector("#tgproj-save");
        saveBtn.disabled = true;
        try {
          const ids = noRestrict.checked
            ? []
            : [...listEl.querySelectorAll("input:checked")].map((c) => Number(c.value));
          await api.setTelegramUserProjects(chatId, ids);
          toast(`Projects updated for chat ${chatId}`, "success");
          modal.close();
          if (onSaved) onSaved();
        } catch (err) {
          toastError(err);
          saveBtn.disabled = false;
        }
      },
    },
    h("h2", {}, `Projects for chat ${chatId}`),
    h("p", { class: "hint" },
      "Projects this Telegram user can see in the bot. Non-visible projects behave as if they do not exist."),
    h("label", { class: "tgproj-no-restrict" }, noRestrict, " No restriction — all projects visible"),
    listEl,
    h(
      "div",
      { class: "modal-actions" },
      h("span", { class: "spacer" }),
      h("button", { type: "submit", class: "btn", id: "tgproj-save" }, "Save")
    )
  );
  const modal = openModal(form, { small: true });
  return modal;
}

export function openTelegramUsersModal() {
  const listEl = h("div", { class: "tg-user-list" });
  const render = async () => {
    clear(listEl);
    let users;
    try {
      users = await api.listTelegramUsers();
    } catch (err) {
      listEl.append(
        h("span", { class: "dim" }, `Could not load users: ${err.message}`)
      );
      return;
    }
    if (!users.length) {
      listEl.append(
        h("span", { class: "dim-sm" },
          "No users yet. Add one below.")
      );
      return;
    }
    for (const u of users) {
      // inline password rotation (mirrors the label-color pattern): the
      // field is empty until a new password is typed; passwords are only
      // ever sent, never displayed (the API never returns them).
      const pwInput = h("input", {
        type: "password",
        placeholder: "new password",
        minlength: "12",
        autocomplete: "new-password",
      });
      const saveBtn = h("button", { class: "btn ghost", type: "button" }, "Save");
      saveBtn.addEventListener("click", async () => {
        const pw = pwInput.value;
        if (!pw) {
          toast("Enter a new password", "info");
          return;
        }
        if (pw.length < 12) {
          toast("Password must be at least 12 characters", "info");
          return;
        }
        saveBtn.disabled = true;
        try {
          await api.setTelegramUserPassword(u.chat_id, pw);
          pwInput.value = "";
          toast(`Password updated for chat ${u.chat_id}`, "success");
          render();
        } catch (err) {
          toastError(err);
        } finally {
          saveBtn.disabled = false;
        }
      });
      const delBtn = h("button", {
        class: "btn ghost tg-del",
        type: "button",
        title: `Remove Telegram user ${u.chat_id}`,
        "aria-label": `Remove Telegram user ${u.chat_id}`,
      }, "✕");
      delBtn.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: `Remove Telegram user ${u.chat_id}?`,
          message: "This chat loses bot access immediately.",
          confirmLabel: "Remove",
          danger: true,
        });
        if (!ok) return;
        try {
          await api.removeTelegramUser(u.chat_id);
          toast(`Removed chat ${u.chat_id}`, "success");
          render();
        } catch (err) {
          toastError(err);
        }
      });
      // per-user visible projects (task #135): the indicator is filled by a
      // background fetch ("all" = no restriction, "N of M" = restricted);
      // the button opens the project-checkbox modal above.
      const projIndicator = h("span", { class: "tg-projects-ind" }, "…");
      (async () => {
        try {
          const [userProjects, all] = await Promise.all([
            api.listTelegramUserProjects(u.chat_id),
            api.listProjects(),
          ]);
          projIndicator.textContent = userProjects.length
            ? `${userProjects.length} of ${all.length}`
            : "all";
        } catch {
          projIndicator.textContent = "";
        }
      })();
      const projBtn = h("button", {
        class: "btn ghost",
        type: "button",
        title: `Visible projects for chat ${u.chat_id}`,
        "aria-label": `Visible projects for chat ${u.chat_id}`,
      }, "Projects");
      projBtn.addEventListener("click", () =>
        openTelegramUserProjectsModal(u.chat_id, () => render())
      );
      listEl.append(
        h(
          "div",
          { class: "tg-user-row" },
          h("span", { class: "tg-chat-id" }, String(u.chat_id)),
          h("span", { class: "tg-ts" },
            `added ${fmtTime(u.created_at)} · updated ${fmtTime(u.updated_at)}`),
          projIndicator,
          projBtn,
          pwInput,
          saveBtn,
          delBtn
        )
      );
    }
  };
  render();

  const chatIdInput = h("input", {
    type: "number",
    id: "tg-add-chat",
    min: "1",
    step: "1",
    placeholder: "chat id",
  });
  const addPwInput = h("input", {
    type: "password",
    id: "tg-add-pw",
    placeholder: "password",
    minlength: "12",
    autocomplete: "new-password",
  });
  const addBtn = h("button", { class: "btn", type: "button", id: "tg-add-btn" }, "Add");
  const add = async () => {
    const chatId = Number(chatIdInput.value);
    const password = addPwInput.value;
    if (!Number.isInteger(chatId) || chatId <= 0) {
      toast("Enter a valid chat id (a positive integer)", "info");
      return;
    }
    if (!password) {
      toast("Enter a password", "info");
      return;
    }
    if (password.length < 12) {
      toast("Password must be at least 12 characters", "info");
      return;
    }
    addBtn.disabled = true;
    try {
      await api.addTelegramUser(chatId, password);
      toast(`Added chat ${chatId}`, "success");
      chatIdInput.value = "";
      addPwInput.value = "";
      render();
    } catch (err) {
      toastError(err);
    } finally {
      addBtn.disabled = false;
    }
  };
  addBtn.addEventListener("click", add);
  for (const el of [chatIdInput, addPwInput]) {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        add();
      }
    });
  }

  const body = h(
    "div",
    {},
    h("h2", {}, "Telegram users"),
    h("p", { class: "hint" },
      "Permitted Telegram chats and their passwords. Find a user's chat id with the bot's /whoami command."),
    listEl,
    h("div", { class: "section-title" }, "Add user"),
    h(
      "div",
      { class: "field-row tg-add-form" },
      h("div", { class: "field" }, h("label", {}, "Chat id"), chatIdInput),
      h("div", { class: "field" }, h("label", {}, "Password"), addPwInput),
      h("div", { class: "field" }, h("label", {}, "\u00a0"), addBtn)
    )
  );
  return openModal(body);
}
