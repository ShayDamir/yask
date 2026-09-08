// Modals: task editor, new task, confirmation, attachment viewer.

import api from "./api.js";
import { h, clear, fmtEstimate, fmtBytes, fmtTime, typeClass } from "./util.js";
import { renderMarkdown } from "./markdown.js";
import { toast, toastError } from "./toast.js";

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
      body.push(h("p", { style: "margin:12px 0 0;font-size:13px" }, "This also affects:"));
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
  const typeSelect = h(
    "select",
    { id: "nt-type" },
    types.map((t) => h("option", { value: t.name, selected: t.name === "Task" ? "selected" : null }, t.name))
  );
  const estInput = h("input", { type: "number", id: "nt-est", min: "0", step: "0.5", placeholder: "—" });
  const syncEpic = () => {
    const isEpic = types.find((t) => t.name === typeSelect.value)?.is_epic;
    estInput.disabled = !!isEpic;
  };
  typeSelect.addEventListener("change", syncEpic);
  syncEpic();

  const form = h(
    "form",
    {
      id: "nt-form",
      onsubmit: async (e) => {
        e.preventDefault();
        const title = titleInput.value.trim();
        if (!title) return;
        const isEpic = types.find((t) => t.name === typeSelect.value)?.is_epic;
        const est = isEpic ? null : estInput.value === "" ? null : Number(estInput.value);
        try {
          await api.createTask(pid, {
            title,
            type: typeSelect.value,
            estimate: est,
            state,
          });
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
  const others = flattenTasks(project.tasks).filter((t) => t.id !== task.id);
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
  if (!others.length) checkList.append(h("span", { style: "color:var(--text-dim);font-size:12px" }, "No other tasks in this project."));

  // attachments
  const attList = h("div", { class: "attachment-list" });
  const fileInput = h("input", { type: "file", id: "ed-file", accept: ".md,.markdown,.txt,image/*", style: "max-width:100%" });
  const renderAttachments = (list) => {
    clear(attList);
    if (!list.length) {
      attList.append(h("span", { style: "color:var(--text-dim);font-size:12px" }, "No attachments."));
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
          const checked = [...checkList.querySelectorAll("input:checked")].map((c) => Number(c.value));
          if (checked.join() !== [...chosen].sort((a, b) => a - b).join()) {
            await api.setPrerequisites(pid, task.number, checked);
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
      h("button", {
        class: "btn danger",
        type: "button",
        onclick: async () => {
          modal.close();
          actions.onDelete(task);
        },
      }, "Delete"),
      h("span", { class: "spacer" }),
      h("button", { class: "btn ghost", type: "button", onclick: () => modal.close() }, "Cancel"),
      h("button", { class: "btn", type: "submit", id: "ed-save" }, "Save")
    )
  );

  const modal = openModal(form);
  setTimeout(() => titleInput.focus(), 0);
  return modal;
}
