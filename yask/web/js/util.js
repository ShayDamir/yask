// Tiny DOM helper: h("div.class", { attr: v, on: { click } }, ...children)

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // only use with static, trusted strings
    else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2), v);
    } else if (k === "dataset") {
      Object.assign(el.dataset, v);
    } else {
      el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function fmtEstimate(v) {
  if (v === null || v === undefined) return null;
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function fmtTime(iso) {
  // "2026-01-02T03:04:05Z" -> "01-02 03:04"
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return iso || "";
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

export function typeClass(type) {
  return ["Story", "Task", "Bug", "Epic"].includes(type) ? `type-${type}` : "type-custom";
}

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
