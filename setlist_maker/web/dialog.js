// The page's dialogs (#61): native <dialog> opened with showModal(), which is
// what gives the focus trap, Escape, the ::backdrop and inertness of the page
// behind it for free. The artwork picker and the shortcut sheet are static
// <dialog>s in the markup; ask() replaces the browser's own confirm dialog.
// A leaf apart from dom.js: other modules import it, it imports none of them.

import { el } from "./dom.js";

// Any modal dialog open? The page's keyboard map stands down while one is:
// keys pressed inside a dialog still bubble to the document.
export const dialogOpen = () => !!document.querySelector("dialog[open]");

// A click on the ::backdrop is reported on the <dialog> itself, but so is one
// on the dialog's own padding, so the test is the pointer's position.
function onBackdrop(dlg, e) {
  if (e.target !== dlg) return false;
  const r = dlg.getBoundingClientRect();
  return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
}

// Wires the shared behaviour onto a static dialog: [data-close] buttons and a
// backdrop click close it. Closing goes through dlg.close() either way, so
// the dialog's own "close" listener is the one place to clean up.
export function sheet(dlg) {
  dlg.addEventListener("click", e => {
    if (onBackdrop(dlg, e) || e.target.closest("[data-close]")) dlg.close();
  });
  // showModal() keeps focus out of the page, but Chrome lets Tab carry on
  // into its own toolbar, and keys pressed there never reach the dialog. And
  // WebKit's Tab skips buttons (Safari's default), so from the last field it
  // left the dialog for <body>. So the dialog moves focus itself, through
  // every focusable it holds, wrapping at either end as a desktop sheet's does.
  dlg.addEventListener("keydown", e => {
    if (e.key !== "Tab" || e.altKey || e.metaKey || e.ctrlKey) return;
    const all = focusables(dlg);
    if (!all.length) return;
    e.preventDefault();
    const i = all.indexOf(document.activeElement);
    const next = i < 0 ? (e.shiftKey ? all.length - 1 : 0)
      : (i + (e.shiftKey ? -1 : 1) + all.length) % all.length;
    all[next].focus();
  });
  return dlg;
}

// What Tab can reach inside a dialog, in order.
function focusables(dlg) {
  return [...dlg.querySelectorAll("button, input, textarea, select, [tabindex]")]
    .filter(n => !n.disabled && n.tabIndex >= 0 && n.type !== "file" && n.getClientRects().length);
}

// A dialog that lost its focus (the focused node was re-rendered away) takes
// it back, so its own keys keep working; `prefer` is where it should land.
export function keepFocus(dlg, prefer) {
  const at = document.activeElement;
  if (!dlg.open || (dlg.contains(at) && !at.disabled)) return;
  (prefer && !prefer.disabled ? prefer : focusables(dlg)[0] || dlg).focus();
}

// ---- ask(): an in-app confirm ----
// Resolves with the chosen button's `value`, or null for Escape, the ✕ or a
// backdrop click. `body` is a string or a node; `buttons` run left to right,
// and the one marked `primary` takes focus (Enter picks it).
const askDlg = sheet(document.getElementById("ask"));
let askDone = null;

export function ask({ title, body, buttons, onClose }) {
  if (askDone) askDone(null);  // never two at once: a second ask cancels the first
  document.getElementById("ask-title").textContent = title;
  const bodyEl = document.getElementById("ask-body");
  bodyEl.replaceChildren(typeof body === "string" ? el("p", null, body) : body);
  const row = document.getElementById("ask-buttons");
  row.replaceChildren();
  let first = null;
  for (const b of buttons) {
    const btn = el("button", "artbtn" + (b.primary ? " on" : "") + (b.danger ? " danger" : ""), b.label);
    btn.type = "button";
    btn.onclick = () => finish(b.value);
    row.appendChild(btn);
    if (b.primary) first = btn;
  }
  return new Promise(resolve => {
    askDone = (v) => {
      askDone = null;
      if (onClose) onClose();
      resolve(v);
    };
    askDlg.returnValue = "";
    askDlg.showModal();
    (first || row.lastChild).focus();
  });
}

let picked = null;
function finish(v) { picked = v; askDlg.close(); }
askDlg.addEventListener("close", () => {
  const v = picked;
  picked = null;
  if (askDone) askDone(v);
});
