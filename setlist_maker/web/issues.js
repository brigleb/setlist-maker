// The "Needs a look" queue.

import { S, TL } from "./state.js";
import { toast } from "./dom.js";
import { setDuration } from "./model.js";
import { selectTrack } from "./grid.js";
import { applyFix } from "./edits.js";
import { html, render } from "./ui.js";

export const dismissedIssues = new Set();
export const ISSUE_STYLE = {
  interrupted: ["≡", "var(--accent)"], short: ["↑", "var(--warn)"], variants: ["≠", "var(--accent)"],
  unidentified: ["?", "var(--muted)"], remaster: ["✂", "var(--ok)"], unheard: ["…", "var(--text-faint)"],
};

function Issue({ x }) {
  const [glyph, colour] = ISSUE_STYLE[x.kind] || ["•", "var(--muted)"];
  const canFix = x.fix && (x.fix.sample ? S.canSample : !S.liveMode);
  const fix = e => {
    e.stopPropagation();
    applyFix(x.fix);
    if (!x.fix.sample) toast(x.fix.label + " — ⌘Z undoes it, Save writes it");
  };
  const dismiss = e => { e.stopPropagation(); dismissedIssues.add(x.key); renderIssues(); };
  return html`
    <div class="issue" onClick=${() => selectTrack(S.tracks[x.index], { reveal: true })}>
      <div class="ico" style=${{ background: colour }}>${glyph}</div>
      <div class="txt"><div class="t1">${x.title}</div><div class="t2">${x.detail}</div></div>
      ${canFix && html`<button class="tbtn" onClick=${fix}>${x.fix.label}</button>`}
      <button class="x" title="Not a problem" onClick=${dismiss}>×</button>
    </div>`;
}

export function renderIssues() {
  const issues = TL.findIssues(S.tracks, S.probes, setDuration()).filter(x => !dismissedIssues.has(x.key));
  document.getElementById("qcount").textContent = issues.length;
  render(issues.length
    ? issues.map(x => html`<${Issue} key=${x.key} x=${x} />`)
    : html`<div class="t2">Nothing left that looks wrong.</div>`,
  document.getElementById("issues"));
}
