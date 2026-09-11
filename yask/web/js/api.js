// REST client for the yask backend.

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }

  get needsConfirmation() {
    return (
      this.status === 409 &&
      typeof this.detail === "object" &&
      this.detail &&
      this.detail.requires_confirmation === true
    );
  }

  get affected() {
    return this.needsConfirmation ? this.detail.affected : null;
  }
}

async function req(method, url, body, isForm = false) {
  const opts = { method };
  if (body !== undefined) {
    if (isForm) {
      opts.body = body; // FormData sets its own content type
    } else {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
  }
  const res = await fetch(url, opts);
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data || res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data;
}

const api = {
  // projects
  listProjects: () => req("GET", "/api/projects"),
  createProject: (name) => req("POST", "/api/projects", { name }),
  getProject: (pid) => req("GET", `/api/projects/${pid}`),

  // tasks
  createTask: (pid, data) => req("POST", `/api/projects/${pid}/tasks`, data),
  getTask: (pid, num) => req("GET", `/api/projects/${pid}/tasks/${num}`),
  updateTask: (pid, num, data) => req("PATCH", `/api/projects/${pid}/tasks/${num}`, data),
  moveTask: (pid, num, toState, { confirm = false, before, after } = {}) =>
    req("POST", `/api/projects/${pid}/tasks/${num}/move`, {
      to_state: toState,
      confirm,
      ...(before !== undefined && { before_number: before }),
      ...(after !== undefined && { after_number: after }),
    }),
  archiveTask: (pid, num, confirm = false) =>
    req("POST", `/api/projects/${pid}/tasks/${num}/archive`, { confirm }),
  restoreTask: (pid, num, toState = "Backlog", confirm = false) =>
    req("POST", `/api/projects/${pid}/tasks/${num}/restore`, { to_state: toState, confirm }),
  deleteTask: (pid, num, confirm = false) =>
    req("DELETE", `/api/projects/${pid}/tasks/${num}${confirm ? "?confirm=true" : ""}`),
  reorderTask: (pid, num, { before, after } = {}) =>
    req("POST", `/api/projects/${pid}/tasks/${num}/reorder`, {
      ...(before !== undefined && { before_number: before }),
      ...(after !== undefined && { after_number: after }),
    }),

  // prerequisites / history
  setPrerequisites: (pid, num, prereqNumbers) =>
    req("PUT", `/api/projects/${pid}/tasks/${num}/prereqs`, { prereq_numbers: prereqNumbers }),
  getHistory: (pid, num) => req("GET", `/api/projects/${pid}/tasks/${num}/history`),

  // attachments
  uploadAttachment: (pid, num, file) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return req("POST", `/api/projects/${pid}/tasks/${num}/attachments`, form, true);
  },
  deleteAttachment: (attId) => req("DELETE", `/api/attachments/${attId}`),
  attachmentUrl: (attId) => `/api/attachments/${attId}`,

  // task types
  listTaskTypes: () => req("GET", "/api/task-types"),
  createTaskType: (name) => req("POST", "/api/task-types", { name }),

  // labels
  listLabels: (pid) => req("GET", `/api/projects/${pid}/labels`),
  createLabel: (pid, name, color = "") =>
    req("POST", `/api/projects/${pid}/labels`, { name, color }),
  setTaskLabels: (pid, num, labelIds) =>
    req("PUT", `/api/projects/${pid}/tasks/${num}/labels`, { label_ids: labelIds }),
  deleteLabel: (pid, labelId) => req("DELETE", `/api/projects/${pid}/labels/${labelId}`),
  updateLabel: (pid, labelId, color) =>
    req("PUT", `/api/projects/${pid}/labels/${labelId}`, { color }),

  // project roles (user-story role presets)
  listProjectRoles: (pid) => req("GET", `/api/projects/${pid}/roles`),
  setProjectRoles: (pid, names) =>
    req("PUT", `/api/projects/${pid}/roles`, { names }),
  removeProjectRole: (pid, name) =>
    req("DELETE", `/api/projects/${pid}/roles/${encodeURIComponent(name)}`),

  // telegram users (the bot's password allowlist)
  listTelegramUsers: () => req("GET", "/api/telegram-users"),
  addTelegramUser: (chatId, password) =>
    req("POST", "/api/telegram-users", { chat_id: chatId, password }),
  setTelegramUserPassword: (chatId, password) =>
    req("PUT", `/api/telegram-users/${chatId}`, { password }),
  removeTelegramUser: (chatId) => req("DELETE", `/api/telegram-users/${chatId}`),
};

export default api;
