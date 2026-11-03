// The ⌘K palette (#59): one box that finds a command, a track or a moment.
// Commands come from COMMANDS, the same list the context menus read; tracks
// match on artist and title ("luvm" finds The Luvmenauts); a time like
// 2:15:00 or 75:30 jumps the playhead there. Ranking is TL.fuzzy, which puts a
// word found whole far above scattered letters.
//
// Focus stays in the box: ↑/↓ move the highlight (aria-activedescendant),
// Enter runs it. The pick runs after the dialog closes, as a menu's does.

import { S, TL } from "./state.js";
import { audio, el, playerEl } from "./dom.js";
import { parseTime, trackName } from "./util.js";
import { keptTrackAt } from "./model.js";
import { seekTo } from "./player.js";
import { selectTrack } from "./grid.js";
import { focusSelected } from "./list.js";
import { startOf } from "./edits.js";
import { sheet } from "./dialog.js";
import { COMMANDS, paletteContext, resolve } from "./commands.js";

const dlg = sheet(document.getElementById("palette"));
const box = document.getElementById("pal-q");
const list = document.getElementById("pal-list");
const MAX = 60;
let results = [], active = 0, picked = null;

export function openPalette() {
  if (dlg.open) return;
  box.value = "";
  refresh();
  dlg.showModal();
  box.focus();
}

// Everything the query finds, best first. An empty query lists the commands.
function search(q) {
  q = q.trim();
  const out = [];
  const secs = parseTime(q);
  if (secs !== null) {
    const D = audio.duration || 0;
    const t = keptTrackAt(secs);
    out.push({
      kind: "Go to", label: TL.fmt(secs) + (t ? " — in " + trackName(t) : ""), score: Infinity,
      disabled: playerEl.classList.contains("disabled") ? "The audio is still loading"
        : secs > D ? "The set is only " + TL.fmt(D) + " long" : "",
      run: () => { seekTo(secs); if (t) { selectTrack(t, { keepTab: true }); focusSelected(); } },
    });
  }
  for (const cmd of COMMANDS) {
    const r = resolve(cmd, paletteContext(cmd), "palette");
    if (!r) continue;
    const score = q ? TL.fuzzy(q, r.label + " " + cmd.group) : 0;
    if (score < 0) continue;
    out.push({ kind: cmd.group, label: r.label, keys: r.keys, disabled: r.disabled, run: r.run, score });
  }
  if (q && secs === null) {
    S.tracks.forEach((t, i) => {
      const score = TL.fuzzy(q, (t.artist || "") + " " + (t.title || ""));
      if (score < 0) return;
      out.push({
        kind: "Track " + (i + 1), label: trackName(t), hint: TL.fmt(startOf(t)) + (t.rejected ? " · merged" : ""),
        // A tie goes to the track: finding one is what a typed name is for.
        score: score + 1, run: () => { selectTrack(t); focusSelected(); },
      });
    });
  }
  if (q) out.sort((a, b) => b.score - a.score);
  return out.slice(0, MAX);
}

function refresh() {
  results = search(box.value);
  active = Math.max(0, results.findIndex(r => !r.disabled));
  list.replaceChildren();
  results.forEach((r, n) => {
    const row = el("div", "pal-item" + (r.disabled ? " off" : ""));
    row.id = "pal-" + n;
    row.setAttribute("role", "option");
    if (r.disabled) row.setAttribute("aria-disabled", "true");
    row.appendChild(el("span", "pal-kind", r.kind));
    row.appendChild(el("span", "pal-label", r.label));
    const side = el("span", "pal-hint");
    if (r.disabled) side.textContent = r.disabled;
    else if (r.keys) r.keys.forEach(k => side.appendChild(el("kbd", null, k)));
    else if (r.hint) side.textContent = r.hint;
    row.appendChild(side);
    row.onpointermove = () => { if (active !== n) { active = n; mark(false); } };
    row.onclick = () => pick(n);
    list.appendChild(row);
  });
  if (!results.length) list.appendChild(el("div", "pal-none", "Nothing matches “" + box.value.trim() + "”"));
  mark(true);
}

function mark(scroll) {
  [...list.children].forEach((row, n) => row.classList.toggle("on", n === active));
  const row = document.getElementById("pal-" + active);
  if (row) {
    box.setAttribute("aria-activedescendant", row.id);
    if (scroll) row.scrollIntoView({ block: "nearest" });
  } else box.removeAttribute("aria-activedescendant");
}

function pick(n) {
  const r = results[n];
  if (!r || r.disabled) return;
  picked = r;
  dlg.close();
}

box.addEventListener("input", refresh);
document.getElementById("pal-btn").onclick = () => openPalette();
dlg.addEventListener("keydown", e => {
  // ⌘K again closes it. Stopped here, or the page's handler, finding no
  // dialog open any more, would open it again (the ? sheet's trap, #61).
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault(); e.stopPropagation(); dlg.close(); return;
  }
  const n = results.length;
  if (!n) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    active = (active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    mark(true);
  } else if (e.key === "Enter") {
    e.preventDefault();
    pick(active);
  }
});
dlg.addEventListener("close", () => {
  const r = picked;
  picked = null;
  if (r) r.run();
});
