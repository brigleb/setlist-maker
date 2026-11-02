// Element lookups and DOM helpers every module uses. A leaf: no imports.

export const audio = document.getElementById("audio");
export const playerEl = document.getElementById("player");
export const pplayEl = document.getElementById("pplay");
export const summaryEl = document.getElementById("summary");
export const epTitle = document.getElementById("ep-title");
export const epArtist = document.getElementById("ep-artist");
// Grows to fit, from a three-line floor. Skipped while its tab is hidden: a
// hidden textarea measures 0, which would collapse it to nothing.
export function autosizeSummary() {
  if (!summaryEl.offsetParent) return;
  summaryEl.style.height = "auto";
  summaryEl.style.height = summaryEl.scrollHeight + "px";
}
export function toast(msg) { const t = document.getElementById("toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 1800); }

// An element with a class and text; text via textContent, never innerHTML --
// artist and title come from Shazam and from whatever the user typed.
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
