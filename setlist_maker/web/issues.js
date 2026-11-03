// The "Needs a look" queue.

import { S, TL } from "./state.js";
import { toast } from "./dom.js";
import { setDuration } from "./model.js";
import { selectTrack } from "./grid.js";
import { applyFix } from "./edits.js";
import { html, Icon, render } from "./ui.js";

export const dismissedIssues = new Set();
export const ISSUE_STYLE = {
  interrupted: ["merge", "var(--accent)"], short: ["clock", "var(--warn)"], variants: ["diff", "var(--accent)"],
  unidentified: ["help", "var(--muted)"], remaster: ["eraser", "var(--ok)"], unheard: ["ear", "var(--text-faint)"],
};

function Issue({ x }) {
  const [name, colour] = ISSUE_STYLE[x.kind] || ["help", "var(--muted)"];
  const canFix = x.fix && (x.fix.sample ? S.canSample : !S.liveMode);
  const run = f => e => {
    e.stopPropagation();
    applyFix(f);
    if (!f.sample) toast(f.label + " — ⌘Z undoes it, Save writes it");
  };
  const dismiss = e => { e.stopPropagation(); dismissedIssues.add(x.key); renderIssues(); };
  return html`
    <div class="issue" onClick=${() => selectTrack(S.tracks[x.index], { reveal: true, keepTab: true })}>
      <div class="ico" style=${{ background: colour }}><${Icon} name=${name} /></div>
      <div class="txt"><div class="t1">${x.title}</div><div class="t2">${x.detail}</div></div>
      ${canFix && html`<div class="fixes">
        <button class="tbtn" onClick=${run(x.fix)}>${x.fix.label}</button>
        ${x.alt && html`<button class="tbtn quiet" onClick=${run(x.alt)}>${x.alt.label}</button>`}
      </div>`}
      <button class="x" title="Not a problem" aria-label="Not a problem" onClick=${dismiss}><${Icon} name="x" /></button>
    </div>`;
}

export function renderIssues() {
  const issues = TL.findIssues(S.tracks, S.probes, setDuration()).filter(x => !dismissedIssues.has(x.key));
  document.getElementById("qcount").textContent = issues.length;
  render(issues.length
    ? issues.map(x => html`<${Issue} key=${x.key} x=${x} />`)
    : html`<div class="blank">
        <div class="blank-ico ok"><${Icon} name="check-circle" /></div>
        <div class="blank-title">All clear</div>
        <p>Nothing left that looks wrong.</p>
      </div>`,
  document.getElementById("issues"));
}
