// Toast feedback for user actions.

const root = () => document.getElementById("toast-root");

export function toast(message, kind = "info", ms = 3200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  root().append(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.25s";
    setTimeout(() => el.remove(), 260);
  }, ms);
}

export function toastError(err) {
  const msg =
    err && typeof err.detail === "string"
      ? err.detail
      : err?.message || String(err);
  toast(msg, "error", 5000);
}
