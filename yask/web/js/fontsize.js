// Minimum font size (pt) floor for the whole UI, persisted like the theme.

const KEY = "yask.fontsize";

const select = () => document.getElementById("min-font");

export function initFontSize() {
  const saved = localStorage.getItem(KEY);
  const valid = saved && select().querySelector(`option[value="${saved}"]`);
  applyFontSize(valid ? saved : "0");
  if (valid) select().value = saved;
  select().addEventListener("change", (e) => {
    applyFontSize(e.target.value);
    localStorage.setItem(KEY, e.target.value);
  });
}

function applyFontSize(ptValue) {
  const pt = parseFloat(ptValue) || 0;
  const px = (pt * 96) / 72; // 1pt = 96/72 px
  document.documentElement.style.setProperty("--min-font", `${px}px`);
}
