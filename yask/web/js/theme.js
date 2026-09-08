// Dark / light theme with persistence.

const KEY = "yask.theme";
const btn = () => document.getElementById("theme-toggle");

export function initTheme() {
  const saved = localStorage.getItem(KEY);
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
  btn().addEventListener("click", () => {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    localStorage.setItem(KEY, next);
  });
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", (e) => {
      if (!localStorage.getItem(KEY)) applyTheme(e.matches ? "dark" : "light");
    });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  btn().textContent = theme === "dark" ? "☾" : "☀";
  btn().title = `Switch to ${theme === "dark" ? "light" : "dark"} theme`;
}
