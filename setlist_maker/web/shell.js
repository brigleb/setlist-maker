// The app shell: the inspector's tabs, the two pane dividers, and the
// slide-over inspector on a narrow window. A leaf apart from dom.js, so any
// module can switch tabs without an import cycle reaching load-time code.

import { autosizeSummary } from "./dom.js";

// ---- tabs ----
const tabs = [...document.querySelectorAll('#tabs [role="tab"]')];

export function showTab(name) {
  for (const b of tabs) {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
    document.getElementById(b.getAttribute("aria-controls")).hidden = !on;
  }
  // A hidden textarea measures 0 tall, so the description sizes itself only
  // once its tab is showing.
  if (name === "set") autosizeSummary();
  remember("tab", name);
}

for (const b of tabs) b.addEventListener("click", () => showTab(b.dataset.tab));
document.getElementById("tabs").addEventListener("keydown", e => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  e.stopPropagation();  // the page's arrows seek the audio
  const k = tabs.findIndex(b => b.getAttribute("aria-selected") === "true");
  const next = tabs[(k + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  showTab(next.dataset.tab);
  next.focus();
});

// ---- pane sizes ----
// Stored as CSS custom properties on :root; the grid clamps them, so a size
// saved on a bigger window cannot push a pane off a smaller one.
const root = document.documentElement;
const SIZES = { "split-tl": ["--tl-h", "y"], "split-side": ["--side-w", "x"] };

function remember(key, value) {
  try { localStorage.setItem("shell." + key, value); } catch (e) { /* private window */ }
}
function recall(key) {
  try { return localStorage.getItem("shell." + key); } catch (e) { return null; }
}

for (const [id, [prop, axis]] of Object.entries(SIZES)) {
  const bar = document.getElementById(id);
  const saved = recall(prop);
  if (saved) root.style.setProperty(prop, saved);
  const pane = axis === "y" ? document.getElementById("timeline") : document.getElementById("side");
  const set = px => {
    root.style.setProperty(prop, Math.round(px) + "px");
    remember(prop, Math.round(px) + "px");
  };
  bar.addEventListener("pointerdown", e => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    // Measured from the pane's rendered size, so the drag starts from what
    // the clamp actually allowed rather than from a stored value it ignored.
    const start = axis === "y" ? e.clientY : e.clientX;
    const from = axis === "y" ? pane.offsetHeight : pane.offsetWidth;
    const move = ev => {
      const d = (axis === "y" ? ev.clientY : ev.clientX) - start;
      set(axis === "y" ? from + d : from - d);  // the inspector grows leftwards
    };
    const up = () => {
      bar.classList.remove("dragging");
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
  // Keyboard resizing, for a focused divider.
  bar.addEventListener("keydown", e => {
    const keys = axis === "y" ? { ArrowUp: -24, ArrowDown: 24 } : { ArrowLeft: 24, ArrowRight: -24 };
    if (!(e.key in keys)) return;
    e.preventDefault();
    e.stopPropagation();
    set((axis === "y" ? pane.offsetHeight : pane.offsetWidth) + keys[e.key]);
  });
  // Double-click puts the default back.
  bar.addEventListener("dblclick", () => {
    root.style.removeProperty(prop);
    try { localStorage.removeItem("shell." + prop); } catch (e) { /* ignore */ }
  });
}

// ---- narrow windows ----
// Below the breakpoint in editor.css the inspector slides over the list
// rather than squeezing it; the toolbar button shows and hides it.
const toggle = document.getElementById("side-toggle");
export function setSideOpen(open) {
  document.body.classList.toggle("side-open", open);
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
}
toggle.addEventListener("click", () => setSideOpen(!document.body.classList.contains("side-open")));

const tab = recall("tab");
if (tab && tabs.some(b => b.dataset.tab === tab)) showTab(tab);
