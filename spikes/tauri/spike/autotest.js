// Spike 3 autotest (SPIKE_AUTOTEST=1): play the set's MP3 from inside the real page, then seek
// and scrub the way the player does (setting currentTime), timing each and checking playback
// moves on afterwards. One JSON report goes to /spike/report; the Rust log has every Range.
// Also injected as an initialization script into Python's http:// page (SPIKE_URL), for an A/B
// in the same webview; from there it reports cross-origin, so no-cors.
const BASE = location.protocol === "hindsight:" ? "" : "hindsight://localhost";
const report = (data) =>
  fetch(BASE + "/spike/report", { method: "POST", mode: "no-cors", body: JSON.stringify({ kind: "autotest", via: location.protocol, ...data }) });
let a;
const events = [];
const T0 = performance.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const once = (ev, ms = 20000) =>
  new Promise((res, rej) => {
    const to = setTimeout(() => rej(new Error("timeout waiting for " + ev)), ms);
    a.addEventListener(ev, () => { clearTimeout(to); res(performance.now()); }, { once: true });
  });

async function run() {
  a = document.getElementById("audio");
  for (const ev of ["seeking", "seeked", "waiting", "playing", "stalled", "canplay", "canplaythrough", "progress", "suspend"])
    a.addEventListener(ev, () => events.push([Math.round(performance.now() - T0), ev, +a.currentTime.toFixed(2)]));
  const t0 = performance.now();
  if (a.readyState < 1) await once("loadedmetadata");
  const meta = { ms: Math.round(performance.now() - t0), duration: a.duration };
  await sleep(4000); // let the page render the list, timeline and waveform
  let storage;
  try { localStorage.setItem("spike3", "1"); storage = localStorage.getItem("spike3") === "1"; }
  catch (e) { storage = "throws: " + e; }
  const page = {
    rows: document.querySelectorAll("#list .row").length,
    pins: document.querySelectorAll(".tl-pin").length,
    blocks: document.querySelectorAll(".tl-seg").length,
    stats: document.getElementById("tl-stats")?.innerText,
    activity: document.getElementById("activity")?.innerText,
    title: document.title,
    storage,
  };

  const play = async () => {
    const p0 = performance.now();
    try { await a.play(); return { ms: Math.round(performance.now() - p0) }; }
    catch (e) { return { err: String(e) }; }
  };
  a.volume = 0.03; // quiet: this plays on the real speakers
  const first = await play();
  if (first.err) { a.muted = true; first.mutedRetry = await play(); }
  await sleep(2000);
  first.advanced = +a.currentTime.toFixed(2);

  const seeks = [];
  const seekTo = async (t) => {
    const s0 = performance.now();
    const seeked = once("seeked");
    a.currentTime = t;
    let ms;
    try { ms = Math.round((await seeked) - s0); } catch (e) { ms = String(e); }
    const landed = a.currentTime;
    await sleep(1500);
    return { target: t, seekedMs: ms, landed: +landed.toFixed(3), plus1500: +(a.currentTime - landed).toFixed(2), paused: a.paused };
  };
  for (const round of [1, 2])
    for (const t of [3600, 120.25, 13000, 7200.5, 30, a.duration - 60, 5000, 5010, 4990]) seeks.push(await seekTo(t));

  // A scrubber drag: 20 positions ~50ms apart (timers may be throttled in a background window,
  // so the gaps are recorded), then time from the *last* position set to its seeked.
  events.length = 0;
  const gaps = [];
  let tLast = performance.now();
  for (let i = 0; i < 20; i++) {
    const now = performance.now();
    if (i) gaps.push(Math.round(now - tLast));
    tLast = now;
    a.currentTime = 9000 + i * 15;
    await sleep(50);
  }
  try { if (a.seeking) await once("seeked"); } catch (e) { /* reported below as no seeked */ }
  const landed = a.currentTime;
  await sleep(1500);
  // Each seeking -> its seeked, from the media events (not the throttled timers).
  const pairs = [];
  let open = null;
  for (const [t, ev] of events) {
    if (ev === "seeking") open = t;
    else if (ev === "seeked" && open !== null) { pairs.push(t - open); open = null; }
  }
  const scrub = { seekingToSeekedMs: pairs, gapsMs: gaps, landed: +landed.toFixed(2), plus1500: +(a.currentTime - landed).toFixed(2) };

  a.pause();
  await report({ meta, page, first, seeks, scrub, muted: a.muted, error: a.error && a.error.code });
}
const go = () => run().catch((e) => report({ fatal: String(e) }));
if (document.readyState === "loading") addEventListener("DOMContentLoaded", go); else go();
