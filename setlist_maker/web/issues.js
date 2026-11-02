// The "Needs a look" queue.

import { S, TL } from "./state.js";
import { el, toast } from "./dom.js";
import { setDuration } from "./model.js";
import { selectTrack } from "./grid.js";
import { applyFix } from "./edits.js";

export const dismissedIssues = new Set();
export const ISSUE_STYLE = {
  interrupted: ["≡", "var(--accent)"], short: ["↑", "var(--warn)"], variants: ["≠", "var(--accent)"],
  unidentified: ["?", "var(--muted)"], remaster: ["✂", "var(--ok)"], unheard: ["…", "var(--text-faint)"],
};

export function renderIssues() {
  const box = document.getElementById("issues");
  box.innerHTML = "";
  const issues = TL.findIssues(S.tracks, S.probes, setDuration()).filter(x => !dismissedIssues.has(x.key));
  document.getElementById("qcount").textContent = issues.length;
  if (!issues.length) { box.appendChild(el("div", "t2", "Nothing left that looks wrong.")); return; }
  for (const x of issues) {
    const [glyph, colour] = ISSUE_STYLE[x.kind] || ["•", "var(--muted)"];
    const row = el("div", "issue");
    const ico = el("div", "ico", glyph);
    ico.style.background = colour;
    const txt = el("div", "txt");
    txt.appendChild(el("div", "t1", x.title));
    txt.appendChild(el("div", "t2", x.detail));
    row.append(ico, txt);
    if (x.fix && (x.fix.sample ? S.canSample : !S.liveMode)) {
      const fix = el("button", "tbtn", x.fix.label);
      fix.onclick = e => {
        e.stopPropagation();
        applyFix(x.fix);
        if (!x.fix.sample) toast(x.fix.label + " — ⌘Z undoes it, Save writes it");
      };
      row.appendChild(fix);
    }
    const dismiss = el("button", "x", "×");
    dismiss.title = "Not a problem";
    dismiss.onclick = e => { e.stopPropagation(); dismissedIssues.add(x.key); renderIssues(); };
    row.appendChild(dismiss);
    row.onclick = () => selectTrack(S.tracks[x.index], { reveal: true });
    box.appendChild(row);
  }
}
