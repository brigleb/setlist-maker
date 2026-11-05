// The app's own behaviour (Hindsight). Inert when Python's editor serves the
// page: S.app is only set by a /api/tracklist that says whether a set is
// open, and window.__TAURI__ exists only in the app's window.
//
// - The drop screen while nothing is open, and the paths that open a set: a
//   drop on the window (tauri://drag-drop, which the native drop handler
//   delivers instead of any HTML5 drop, spike 3) or on the Dock icon
//   (hindsight://open, then /api/pending, held until the page asks).
// - An image dropped over the episode cover or the artwork panel is an upload
//   from its path; anything else opens a set.
// - Leaving a set (another drop, Done, closing the window, quitting) asks
//   first: Save & close / Discard / Cancel while editing, Finish now / Keep
//   running / Stop while a run is identifying.
// - Finish now, Resume (Shazam throttling), Continue identifying, Generate.

import { S } from "./state.js";
import { audio, autosizeSummary, summaryEl, toast } from "./dom.js";
import { ask } from "./dialog.js";
import { during } from "./activity.js";
import { load, save } from "./main.js";
import { artDialog, uploadCover, uploadTrackArt } from "./artwork.js";

const tauri = window.__TAURI__;
const dropScreen = document.getElementById("drop-screen");
const dropNotice = document.getElementById("drop-notice");
const finishBtn = document.getElementById("finish-now");
const resumeBtn = document.getElementById("resume");
const continueBtn = document.getElementById("continue");
const genBtn = document.getElementById("gen-summary");

const IMAGE = /\.(jpe?g|png|gif|webp|heic)$/i;

function post(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(r => r.json().catch(() => ({ ok: false, error: "HTTP " + r.status })));
}

export function showDropScreen(notice) {
  dropScreen.hidden = false;
  dropNotice.hidden = !notice;
  dropNotice.textContent = notice || "";
  document.body.classList.add("empty");
  document.title = "Hindsight";
  audio.pause();
  audio.removeAttribute("src");
}

export function hideDropScreen() {
  dropScreen.hidden = true;
  document.body.classList.remove("empty");
}

// The toolbar's run controls follow what the editor says about the run.
export function syncRunControls() {
  const app = !!S.app;
  finishBtn.hidden = !(app && S.liveMode);
  if (!S.liveMode) finishBtn.disabled = false;  // ready for the next run
  resumeBtn.hidden = !(app && S.liveMode && S.paused);
  continueBtn.hidden = !(app && !S.liveMode && S.run);
  document.body.classList.toggle("paused", !!(S.liveMode && S.paused));
  const pill = document.getElementById("livepill");
  pill.textContent = S.paused ? "paused: Shazam is throttling" : "identifying";
  pill.title = S.paused
    ? S.paused + ". Resume asks again; Finish now makes the tracklist from what was heard."
    : "Following the run as it samples; editing opens when it finishes";
  if (app && S.summaryAvailable === undefined) {
    S.summaryAvailable = false;
    fetch("/api/summary").then(r => r.json()).then(j => {
      S.summaryAvailable = !!j.available;
      genBtn.hidden = !(S.summaryAvailable && !S.liveMode);
    }).catch(() => {});
  }
  genBtn.hidden = !(app && S.summaryAvailable && !S.liveMode);
}

// What should happen to the run that's identifying, asked of the user.
// Resolves to "finish", "stop", or null (keep running).
export async function endRunChoice(why) {
  const v = await ask({
    title: why,
    body: "Hindsight is still identifying this set. Finish now makes the tracklist from what it has heard; Stop keeps the samples, so dropping the recording again carries on.",
    buttons: [
      { label: "Stop", value: "stop", danger: true },
      { label: "Keep running", value: null },
      { label: "Finish now", value: "finish", primary: true },
    ],
  });
  return v || null;
}

// Unsaved edits: Save & close / Discard / Cancel. Resolves to whether to go on.
async function leaveEdits(title) {
  if (!S.dirty) return true;
  const v = await ask({
    title,
    body: "You have unsaved changes to this tracklist.",
    buttons: [
      { label: "Discard", value: "discard", danger: true },
      { label: "Cancel", value: null },
      { label: "Save & close", value: "save", primary: true },
    ],
  });
  if (!v) return false;
  if (v === "save" && !(await save())) return false;  // save() has said why
  S.dirty = false;
  return true;
}

// Before leaving the open set, ask what the user wants: how a live run ends,
// or what happens to unsaved edits. Resolves to null to stay, else
// { run } with run "finish", "stop" or undefined (no run was live).
async function leaveSet(runTitle, editTitle) {
  if (S.liveMode) {
    const run = await endRunChoice(runTitle);
    return run ? { run } : null;
  }
  if (!(await leaveEdits(editTitle))) return null;
  S.dirty = false;  // or beforeunload would ask again
  return { run: undefined };
}

// Opens a path dropped on the window or the Dock icon, asking first about
// whatever is open.
export async function openPath(path) {
  const leave = await leaveSet("Open another set?", "Save your changes before opening another set?");
  if (!leave) return;
  const run = leave.run;
  const label = run === "finish" ? "Finishing the run…" : "Opening " + path.split("/").pop() + "…";
  const res = await during("open", label, () => post("/api/open", { path, run }));
  if (!res.ok) toast(res.error || "Can't open that", { kind: "error" });
  await load();
}

// Done in the app: back to the drop screen, a live run finished or stopped
// first as the user says.
export async function done() {
  const leave = await leaveSet("Stop identifying?", "Save your changes before closing?");
  if (!leave) return;
  audio.pause();
  const label = leave.run === "finish" ? "Finishing the run…" : leave.run ? "Stopping the run…" : "Closing…";
  await during("open", label, () => post("/api/done", { run: leave.run })).catch(() => {});
  await load();
}

finishBtn.onclick = async () => {
  finishBtn.disabled = true;
  await post("/api/finish");
  toast("Finishing after this sample…");
};

resumeBtn.onclick = async () => {
  await post("/api/resume");
  S.paused = null;
  syncRunControls();
  toast("Asking Shazam again");
};

continueBtn.onclick = async () => {
  if (S.run && S.run.edited) {
    const v = await ask({
      title: "Carry on identifying?",
      body: "The tracklist was edited since this run stopped early. When the run finishes it writes a new tracklist over those edits.",
      buttons: [
        { label: "Cancel", value: null },
        { label: "Carry on", value: "go", danger: true },
      ],
    });
    if (!v) return;
  } else if (!(await leaveEdits("Save your changes before carrying on?"))) {
    return;
  }
  S.dirty = false;
  const res = await post("/api/continue");
  if (!res.ok) toast(res.error || "Can't carry on", { kind: "error" });
  await load();
};

genBtn.onclick = async () => {
  genBtn.disabled = true;
  try {
    const res = await during("summary", "Writing the description…", () => post("/api/summary"));
    if (res.ok) {
      summaryEl.value = res.summary;
      autosizeSummary();
      summaryEl.dispatchEvent(new Event("input", { bubbles: true }));  // dirty, as typing is
      toast("Description written — Save to keep it");
    } else {
      toast("No description: " + (res.error || "the claude CLI failed"), { kind: "error" });
    }
  } finally {
    genBtn.disabled = false;
  }
};

// ---- what arrives through the app's window ----

// The drop zone under a window position: the open artwork panel, the
// episode cover, or the drop screen.
function zoneAt(position) {
  if (!position) return null;
  const node = document.elementFromPoint(position.x, position.y);
  if (!node) return null;
  if (artDialog.open && artDialog.contains(node)) return artDialog;
  return node.closest("#episode, #drop-screen");
}

let lit = null;
function light(zone) {
  if (lit === zone) return;
  if (lit) lit.classList.remove(lit === dropScreen ? "over" : "drop");
  lit = zone;
  if (lit) lit.classList.add(lit === dropScreen ? "over" : "drop");
}

function dropped(paths, position) {
  light(null);
  const path = paths && paths[0];
  if (!path) return;
  const zone = zoneAt(position);
  if (IMAGE.test(path)) {
    const file = { path, name: path.split("/").pop(), type: "image/*", size: 0 };
    if (S.liveMode) return;
    if (zone === artDialog) { uploadTrackArt(file); return; }
    if (zone && zone.id === "episode") { uploadCover(file); return; }
    if (S.tracks.length) {
      toast("Drop an image on the episode cover (Set tab) or the artwork panel", { kind: "error" });
      return;
    }
  }
  openPath(path);
}

async function takePending() {
  const j = await (await fetch("/api/pending")).json().catch(() => ({}));
  if (j.path) openPath(j.path);
}

if (tauri && tauri.event) {
  const on = (name, fn) => tauri.event.listen(name, e => fn(e.payload || {}));
  on("tauri://drag-enter", p => light(zoneAt(p.position)));
  on("tauri://drag-over", p => light(zoneAt(p.position)));
  on("tauri://drag-leave", () => light(null));
  on("tauri://drag-drop", p => dropped(p.paths, p.position));
  on("hindsight://open", () => takePending());
  on("hindsight://close", async () => {
    const leave = await leaveSet("Close Hindsight?", "Save your changes before closing?");
    if (!leave) return;
    if (leave.run) toast(leave.run === "finish" ? "Finishing the run, then closing…" : "Stopping, then closing…");
    await post("/api/close", { run: leave.run });
  });
  // A path from a cold launch (the Dock, Finder) arrives before the page did.
  queueMicrotask(takePending);
}
