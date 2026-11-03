// The page's one context menu (#59), a <dialog> opened with showModal(), as
// every other dialog here is (#61). Being modal is what makes it behave like a
// desktop menu: the page behind it is inert, so a click outside closes the
// menu without also landing on what was under it, and the keyboard map stands
// down while it is open (keys.js's dialogOpen()). Its ::backdrop is clear.
//
// Items are { label, keys, disabled, run }, "-" for a separator, or
// { heading } for the grey title line. A greyed item can still be focused,
// and the line at the bottom says why it is greyed. The chosen item runs after
// the menu has closed and focus has gone back where it was, so an action that
// moves focus (Rename, Insert) keeps it.

import { el } from "./dom.js";

const dlg = document.getElementById("menu");
const list = document.getElementById("menu-items");
const why = document.getElementById("menu-why");
let picked = null;
let openedAt = 0;

// The menu key fires keydown, which opens the menu, and then a contextmenu
// event, which must not open a second one. Only a keyboard menu sets this, so
// a quick second right-click elsewhere still moves the menu there.
export const menuJustOpened = () => performance.now() - openedAt < 400;

const items = () => [...list.querySelectorAll(".mi")];

// Opens the menu with its top-left corner at { x, y } (viewport pixels),
// moved in to fit the window. Returns whether it opened.
export function openMenu(entries, at) {
  // No separator first, last, or twice running, and no menu of nothing.
  const rows = [];
  for (const e of entries) {
    if (e === "-" && (!rows.length || rows[rows.length - 1] === "-" || rows[rows.length - 1].heading !== undefined)) continue;
    if (e && e.heading !== undefined && !e.heading) continue;
    rows.push(e);
  }
  while (rows.length && rows[rows.length - 1] === "-") rows.pop();
  if (!rows.some(r => r !== "-" && r.heading === undefined)) return false;

  list.replaceChildren();
  for (const r of rows) {
    if (r === "-") { list.appendChild(el("div", "menu-sep")).setAttribute("role", "separator"); continue; }
    if (r.heading !== undefined) { list.appendChild(el("div", "menu-head", r.heading)); continue; }
    const b = el("button", "mi" + (r.disabled ? " off" : ""));
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.tabIndex = -1;
    if (r.disabled) { b.setAttribute("aria-disabled", "true"); b.dataset.why = r.disabled; }
    b.appendChild(el("span", "mi-label", r.label));
    if (r.keys) b.appendChild(el("kbd", null, r.keys.join(" ")));
    b.onclick = () => { if (!r.disabled) { picked = r; dlg.close(); } };
    list.appendChild(b);
  }
  why.textContent = "";
  why.hidden = true;
  dlg.style.left = "0px";
  dlg.style.top = "0px";
  dlg.showModal();
  openedAt = at.keyboard ? performance.now() : 0;
  // Moved in to fit: to the left of the point if it would run off the right,
  // above it if it would run off the bottom, and never off the top.
  const r = dlg.getBoundingClientRect(), pad = 8;
  let x = at.x, y = at.y;
  if (x + r.width > innerWidth - pad) x = Math.max(pad, innerWidth - pad - r.width);
  if (y + r.height > innerHeight - pad) y = Math.max(pad, y - r.height);
  dlg.style.left = Math.round(x) + "px";
  dlg.style.top = Math.round(y) + "px";
  // A menu the pointer opened starts with nothing highlighted, as a desktop
  // menu does; ↓ then goes to the first item. The keyboard's starts on it.
  if (at.keyboard) focusItem(items()[0]); else list.focus();
  return true;
}

function focusItem(b) {
  if (!b) return;
  b.focus();
  why.textContent = b.dataset.why || "";
  why.hidden = !b.dataset.why;
}

list.addEventListener("pointermove", e => {
  const b = e.target.closest(".mi");
  if (b && b !== document.activeElement) focusItem(b);
});
list.addEventListener("pointerleave", () => { list.focus(); why.hidden = true; });

dlg.addEventListener("keydown", e => {
  const all = items(), at = all.indexOf(document.activeElement);
  const go = i => { e.preventDefault(); focusItem(all[(i + all.length) % all.length]); };
  if (e.key === "ArrowDown") go(at + 1);
  else if (e.key === "ArrowUp") go(at === -1 ? -1 : at - 1);
  else if (e.key === "Home") go(0);
  else if (e.key === "End") go(-1);
  else if (e.key === "Tab") { e.preventDefault(); dlg.close(); }
  else if ((e.key === "Enter" || e.key === " ") && at !== -1) { e.preventDefault(); all[at].click(); }
  else if (e.key.length === 1 && /\S/.test(e.key) && !e.metaKey && !e.ctrlKey && !e.altKey) {
    // Type-ahead: the next item starting with that letter.
    const k = e.key.toLowerCase();
    const order = [...all.slice(at + 1), ...all.slice(0, at + 1)];
    const hit = order.find(b => b.textContent.trim().toLowerCase().startsWith(k));
    if (hit) { e.preventDefault(); focusItem(hit); }
  } else return;
  e.stopPropagation();
});

// A press outside the menu closes it. The ::backdrop is the dialog's own, so
// "outside" is the pointer's position, as in dialog.js.
const outside = e => {
  const r = dlg.getBoundingClientRect();
  return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
};
dlg.addEventListener("click", e => { if (e.target === dlg && outside(e)) dlg.close(); });
// A right-click outside closes this menu and opens the one for wherever it
// landed, as a desktop menu does. The browser's own menu never shows here.
dlg.addEventListener("contextmenu", e => {
  e.preventDefault();
  if (!outside(e) || menuJustOpened()) return;
  const { clientX: x, clientY: y } = e;
  dlg.close();
  // While the menu was open the backdrop was on top; now the page is.
  const under = document.elementFromPoint(x, y);
  if (under) {
    under.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 2 }));
  }
});

// "close" arrives a task after close(), by which time a right-click outside
// may already have opened the next menu, so this touches nothing but the pick.
dlg.addEventListener("close", () => {
  const r = picked;
  picked = null;
  if (r) r.run();
});
