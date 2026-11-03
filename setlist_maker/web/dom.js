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
// Toasts (#62) stack instead of overwriting one another, newest at the
// bottom, each on its own timer, so "Saved" no longer wipes a sample's answer.
// They are for things that finished; work still running shows in the
// toolbar's activity area (activity.js). The stack is a manual popover (#61):
// shown afresh for each message, which puts it at the top of the top layer,
// above any modal dialog opened since. `kind` "error" draws the alert icon.
const TOAST_MAX = 4;
export function toast(msg, opts) {
  const box = document.getElementById("toast");
  const kind = (opts && opts.kind) || "";
  const item = el("div", "toast" + (kind ? " " + kind : ""));
  if (kind === "error") item.append(icon("alert"));
  item.append(el("span", null, msg));
  box.append(item);
  // Just above the player bar, whose height depends on the waveform strip.
  box.style.bottom = (playerEl.offsetHeight || 0) + 14 + "px";
  while (box.children.length > TOAST_MAX) box.firstElementChild.remove();
  if (box.showPopover) {
    try { if (box.matches(":popover-open")) box.hidePopover(); box.showPopover(); } catch (e) {}
  }
  // Long enough to read: a short "Saved" goes quickly, a sentence stays.
  const ms = Math.min(7000, 2200 + 45 * msg.length) + (kind === "error" ? 2000 : 0);
  setTimeout(() => {
    item.classList.add("out");
    setTimeout(() => {
      item.remove();
      if (!box.children.length && box.hidePopover) { try { box.hidePopover(); } catch (e) {} }
    }, 200);
  }, ms);
}

// An icon from the page's sprite (#62): <svg class="ic"><use href="#i-name">.
// `cls` adds classes, e.g. "fill" for a solid shape. Decorative: the button
// or text beside it carries the name.
const SVG = "http://www.w3.org/2000/svg";
export function icon(name, cls) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "ic" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

// An element with a class and text; text via textContent, never innerHTML --
// artist and title come from Shazam and from whatever the user typed.
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
