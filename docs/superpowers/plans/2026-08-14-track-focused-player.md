# Track-Focused Player Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the web editor's player so its scrubber spans the current track's window instead of the whole recording, add ±15 s / prev / next transport with keyboard shortcuts, and make the playing row visible in the list.

**Architecture:** All changes live in the single packaged asset `setlist_maker/web_editor.html` (markup, styles and script in one file — the established pattern; do not split it). Track windows are derived from the live `tracks` array on demand, never cached, so they follow inserts without separate bookkeeping. The scrubber becomes a 0–1000 range input mapped onto the current window rather than the whole file.

**Tech Stack:** Vanilla ES2020 in one HTML file, no build step, no framework, no dependencies. Python 3.10+ / pytest for the server-side presence tests.

## Global Constraints

- **No new runtime dependencies.** No npm, no build step, no CDN — a strict same-origin server serves this one file.
- **No JS test harness.** Verification is browser-based (see each task's verify step). This is a deliberate, spec-recorded tradeoff.
- **`textContent`, never `innerHTML`,** for any track-derived string. The existing code notes this as the XSS boundary.
- **`backgroundImage`, never the `background` shorthand,** when setting artwork in JS. The shorthand resets `background-size`/`background-position` and corner-crops the image (this bug has shipped twice).
- **`#artwork-overlay` must stay before `<script>`** in the document. Moving it after killed the whole script once, rendering a blank editor with a green test suite.
- **Element IDs are contract.** `tests/test_web_editor.py` asserts their presence; keep `#player`, `#pthumb`, `#pplay`, `#plabel`, `#seek`, `#ptime` and add the new IDs listed per task.
- **Seeks clamp to `[0, audio.duration]` only** — never to the track window.
- Python: line length 100, ruff rules E/F/W/I. Run `uv run ruff check .` and `uv run ruff format .`.
- Run the full suite with `uv run pytest tests/ -q`. There is no bare `python` on PATH; always `uv run`.

## Browser verification setup (every task uses this)

Start the editor against the real 56-track set:

```bash
cd /Users/ray/bin/setlist-maker
SET="/Users/ray/Library/Mobile Documents/com~apple~CloudDocs/Music/DJ Disarray 🎧"
uv run setlist-maker "$SET/2026-07-22-Keys-Lounge_tracklist.md" -w
```

It prints nothing useful; find the port with
`lsof -nP -iTCP -sTCP:LISTEN | grep Python` and confirm with
`curl -s -H "Host: 127.0.0.1:<port>" http://127.0.0.1:<port>/api/tracklist | head -c 80`.

**Mute before playing anything** — this plays out of the user's speakers:
`document.getElementById('audio').muted = true`.

**Never click Save** during verification. The tracklist on disk is the user's
real work. If a step dirties the page, reload instead of saving.

---

### Task 1: Window helpers

**Files:**
- Modify: `setlist_maker/web_editor.html` (script, after `fmt()` at line ~109)

**Interfaces:**
- Consumes: the global `tracks` array; `audio`; `fmt(seconds)`.
- Produces: `windowFor(i)` → `{start, end, length}`; `trackIndexAt(seconds)` → integer index; `fmtWindowTimes(i, seconds)` → `{into, left}` strings.

- [ ] **Step 1: Add the three helpers**

Insert after `fmt()`:

```javascript
  // A track's window runs from its own timestamp to the next track's; the last
  // runs to the end of the audio. Derived on demand, never cached, so inserting
  // a row needs no separate bookkeeping. Clamped to >=1s so two tracks sharing a
  // timestamp cannot produce a zero-width scrubber and a division by zero.
  function windowFor(i) {
    const t = tracks[i];
    if (!t) return { start: 0, end: 1, length: 1 };
    const start = t.timestamp;
    const next = tracks[i + 1];
    const rawEnd = next ? next.timestamp : (audio.duration || start + 1);
    const end = Math.max(rawEnd, start + 1);
    return { start, end, length: end - start };
  }

  // Which track's window contains this playback position. Returns the last
  // track for a position past the final boundary, 0 for a position before the
  // first track's timestamp (a set whose first track does not start at 0).
  function trackIndexAt(seconds) {
    if (!tracks.length) return null;
    for (let i = tracks.length - 1; i >= 0; i--) {
      if (seconds >= tracks[i].timestamp) return i;
    }
    return 0;
  }

  // "0:43 in" / "3:17 left" for the position within track i's window.
  function fmtWindowTimes(i, seconds) {
    const w = windowFor(i);
    const into = Math.max(0, Math.min(seconds - w.start, w.length));
    return { into: fmt(into) + " in", left: fmt(w.length - into) + " left" };
  }
```

- [ ] **Step 2: Verify the window maths in the live page**

Start the editor (see setup above), open it, and run in the console via the
page context:

```javascript
(() => {
  const out = [];
  for (let i = 0; i < tracks.length; i++) {
    const w = windowFor(i);
    if (w.start !== tracks[i].timestamp) out.push(`start mismatch at ${i}`);
    if (i + 1 < tracks.length && w.end !== tracks[i + 1].timestamp) out.push(`end mismatch at ${i}`);
    if (w.length < 1) out.push(`zero-width window at ${i}`);
  }
  const last = windowFor(tracks.length - 1);
  if (Math.abs(last.end - audio.duration) > 0.01) out.push("last window does not end at duration");
  if (trackIndexAt(0) !== 0) out.push("trackIndexAt(0) wrong");
  if (trackIndexAt(tracks[5].timestamp) !== 5) out.push("trackIndexAt on a boundary wrong");
  if (trackIndexAt(tracks[5].timestamp - 1) !== 4) out.push("trackIndexAt just before a boundary wrong");
  if (trackIndexAt(audio.duration) !== tracks.length - 1) out.push("trackIndexAt at EOF wrong");
  return out.length ? out : "ALL WINDOW ASSERTIONS PASS";
})()
```

Expected: `"ALL WINDOW ASSERTIONS PASS"`. Do not proceed on any other result.

- [ ] **Step 3: Commit**

```bash
git add setlist_maker/web_editor.html
git commit -m "feat(web-editor): derive per-track playback windows"
```

---

### Task 2: Player markup and layout

**Files:**
- Modify: `setlist_maker/web_editor.html` — `body` rule (line ~10), player CSS (lines ~49-58), player markup (lines ~76-84)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: element IDs `#pprev`, `#pback15`, `#pplay`, `#pfwd15`, `#pnext`, `#ppos` (track-relative timings), `#ptime` (set-relative), `#pcount` (`track N of M`), `#plabel`, `#seek`, `#pthumb`.

- [ ] **Step 1: Replace the player markup**

Replace lines ~76-84 (`<div id="player" class="disabled"> … </div>`) with:

```html
  <div id="player" class="disabled">
    <div id="pthumb"></div>
    <div id="pbody">
      <div id="ptop">
        <div id="ptransport">
          <button id="pprev" title="Previous track (Up)">⏮</button>
          <button id="pback15" title="Back 15 seconds (Left)">↺<span>15</span></button>
          <button id="pplay" title="Play/pause (Space)">▶</button>
          <button id="pfwd15" title="Forward 15 seconds (Right)"><span>15</span>↻</button>
          <button id="pnext" title="Next track (Down)">⏭</button>
        </div>
        <div id="plabel">Nothing playing</div>
        <div id="pcount"></div>
      </div>
      <input id="seek" type="range" min="0" max="1000" value="0" step="1" aria-label="Seek within track">
      <div id="pfoot">
        <div id="ppos">—</div>
        <div id="ptime">0:00 / 0:00</div>
      </div>
    </div>
  </div>
```

Note `#seek` is now 0–1000 over the *window*, not 0–100 over the file.

- [ ] **Step 2: Replace the player CSS**

Replace the `#player` … `#ptime` rules (lines ~49-58) with:

```css
  #player { position:fixed; left:0; right:0; bottom:0; background:#1c1c1e; color:#fff; display:flex; align-items:center; gap:14px; padding:10px 20px; }
  #player.disabled { opacity:.4; pointer-events:none; }
  /* background-color, not the `background` shorthand: the shorthand would reset
     background-size:cover declared beside it and crop the artwork to its corner. */
  #pthumb { width:48px; height:48px; border-radius:6px; flex:none; background-color:#333; background-size:cover; background-position:center; }
  #pbody { flex:1; min-width:0; display:flex; flex-direction:column; gap:5px; }
  #ptop { display:flex; align-items:center; gap:14px; }
  #ptransport { display:flex; align-items:center; gap:2px; flex:none; }
  #ptransport button { min-width:34px; height:28px; font-size:14px; background:none; border:none; color:#fff; cursor:pointer; border-radius:6px; line-height:1; }
  #ptransport button:hover { background:#2f2f33; }
  #ptransport button span { font-size:9px; vertical-align:super; }
  #pplay { font-size:17px; }
  #plabel { flex:1; min-width:0; font-weight:600; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #pcount, #ppos, #ptime { color:#9a9a9e; font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap; flex:none; }
  #seek { width:100%; margin:0; accent-color:var(--accent); }
  #pfoot { display:flex; justify-content:space-between; align-items:center; }
```

- [ ] **Step 3: Update the body padding to clear the taller bar**

The bar grows from 66 px to ~84 px. Change line ~10 from `padding-bottom:84px;`
to `padding-bottom:104px;`. Without this the final list row hides behind the
player.

- [ ] **Step 4: Verify visually in the browser**

Reload the editor. Confirm:
- the five transport buttons render left of the label, artwork at 48 px;
- the scrubber spans the bar's full width beneath them;
- `—` sits bottom-left, `0:00 / 4:00:16` bottom-right;
- scroll to the very bottom of the list: **the last row is fully visible and
  not obscured by the player**;
- measure the bar and confirm the padding covers it:

```javascript
JSON.stringify({
  playerHeight: Math.round(document.getElementById('player').getBoundingClientRect().height),
  bodyPadBottom: getComputedStyle(document.body).paddingBottom
})
```

Expected: `bodyPadBottom` numerically greater than `playerHeight`.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/web_editor.html
git commit -m "feat(web-editor): rework the player bar layout"
```

---

### Task 3: Window-scoped scrubber and transport controls

**Files:**
- Modify: `setlist_maker/web_editor.html` — `playFrom()` (lines ~284-295) and the player event handlers below it (lines ~297-309)

**Interfaces:**
- Consumes: `windowFor(i)`, `trackIndexAt(seconds)`, `fmtWindowTimes(i, seconds)` (Task 1); the IDs from Task 2.
- Produces: `seekTo(seconds)`, `syncPlayerUI()`, `setPlayingIndex(i, {scroll})`, `SKIP_SECONDS`, `PREV_RESTART_THRESHOLD`.

- [ ] **Step 1: Add a `highlightPlayingRow` stub first**

Task 4 implements it; the code in Step 2 calls it. Function declarations hoist,
but adding the stub first keeps the page working at every step. Insert it just
above where `playFrom` currently sits:

```javascript
  function highlightPlayingRow(scroll) { /* implemented in the highlight task */ }
```

- [ ] **Step 2: Replace `playFrom` and add the transport primitives**

Replace `playFrom()` (lines ~284-295) with:

```javascript
  const SKIP_SECONDS = 15;
  const PREV_RESTART_THRESHOLD = 3;  // ⏮ within this many seconds goes to the previous track

  // Seeks clamp to the audio's real bounds, never to the track window: crossing
  // a boundary is how a user checks whether that boundary is right.
  function seekTo(seconds) {
    if (!audio.duration) return;
    audio.currentTime = Math.max(0, Math.min(seconds, audio.duration));
    syncPlayerUI();
  }

  // Point the player at track i without seeking. `scroll` brings its row into
  // view -- true only on automatic advance, since a row the user just clicked is
  // already under their cursor and yanking the list would be hostile.
  function setPlayingIndex(i, opts) {
    if (i === null || !tracks[i]) return;
    playingIndex = i;
    const t = tracks[i];
    document.getElementById("plabel").textContent =
      (t.artist || "Unidentified") + (t.title ? " — " + t.title : "");
    document.getElementById("pcount").textContent = "track " + (i + 1) + " of " + tracks.length;
    const pt = document.getElementById("pthumb");
    // backgroundImage, never the `background` shorthand -- see the CSS comment.
    pt.style.backgroundImage = t.coverart_url
      ? "url('" + t.coverart_url + "')"
      : GRADS[i % GRADS.length];
    highlightPlayingRow(opts && opts.scroll);
  }

  // `scroll` defaults to true: any jump the user did not initiate by clicking a
  // specific row should bring the destination into view. Only the row's own ▶
  // passes false, because that row is already under the cursor.
  function playFrom(i, opts) {
    const scroll = !opts || opts.scroll !== false;
    setPlayingIndex(i, { scroll });
    const start = () => { audio.currentTime = tracks[i].timestamp; audio.play(); };
    if (audio.readyState >= 1) start();
    else audio.addEventListener("loadedmetadata", start, { once: true });
  }

  function skip(delta) { seekTo(audio.currentTime + delta); }

  // Prev/next step through `tracks` by position, so rejected tracks are included
  // -- a track is often auditioned before the decision to un-reject it.
  function prevTrack() {
    const i = playingIndex === null ? trackIndexAt(audio.currentTime) : playingIndex;
    if (i === null) return;
    // Familiar transport convention: restart this track unless already near its start.
    const target = (audio.currentTime - tracks[i].timestamp > PREV_RESTART_THRESHOLD)
      ? i : Math.max(0, i - 1);
    playFrom(target);
  }

  function nextTrack() {
    const i = playingIndex === null ? trackIndexAt(audio.currentTime) : playingIndex;
    if (i === null) return;
    playFrom(Math.min(tracks.length - 1, i + 1));
  }
```

Note the row button in `rowEl()` (line ~210) must be updated to opt out of
scrolling — change `play.onclick` to:

```javascript
    play.onclick = (e) => { e.stopPropagation(); playFrom(i, { scroll: false }); };
```

- [ ] **Step 3: Replace the player event handlers**

Replace lines ~297-309 (the `pplay` onclick through the `seek` input listener)
with:

```javascript
  // Declared before syncPlayerUI, which reads it: `let` has no hoisted value,
  // so defining it after would leave a temporal-dead-zone trap for any future
  // caller that runs earlier than these listeners.
  let seekDragging = false;

  // Reflect audio state into the bar. Called on timeupdate and after any seek.
  function syncPlayerUI() {
    const seek = document.getElementById("seek");
    document.getElementById("ptime").textContent =
      fmt(audio.currentTime) + " / " + fmt(audio.duration);
    if (playingIndex === null) return;
    const w = windowFor(playingIndex);
    const frac = Math.max(0, Math.min((audio.currentTime - w.start) / w.length, 1));
    if (!seekDragging) seek.value = String(Math.round(frac * 1000));
    const times = fmtWindowTimes(playingIndex, audio.currentTime);
    document.getElementById("ppos").textContent = times.into + " · " + times.left;
  }

  document.getElementById("pplay").onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
  document.getElementById("pback15").onclick = () => skip(-SKIP_SECONDS);
  document.getElementById("pfwd15").onclick = () => skip(SKIP_SECONDS);
  document.getElementById("pprev").onclick = prevTrack;
  document.getElementById("pnext").onclick = nextTrack;

  audio.addEventListener("play", () => document.getElementById("pplay").textContent = "⏸");
  audio.addEventListener("pause", () => document.getElementById("pplay").textContent = "▶");
  audio.addEventListener("loadedmetadata", () => {
    document.getElementById("player").classList.remove("disabled");
    syncPlayerUI();
  });
  audio.addEventListener("error", () => document.getElementById("player").classList.add("disabled"));
  audio.addEventListener("timeupdate", syncPlayerUI);

  const seekEl = document.getElementById("seek");
  seekEl.addEventListener("pointerdown", () => { seekDragging = true; });
  seekEl.addEventListener("pointerup", () => { seekDragging = false; });
  seekEl.addEventListener("input", e => {
    if (playingIndex === null) return;
    const w = windowFor(playingIndex);
    seekTo(w.start + (e.target.value / 1000) * w.length);
  });
```

The `seekDragging` flag stops `timeupdate` writing back to the thumb mid-drag,
which otherwise fights the user's pointer.

- [ ] **Step 4: Verify the controls in the browser**

Reload, mute (`document.getElementById('audio').muted = true`), click ▶ on
Wilson Pickett (8:30), then run this. It **pauses first** — with playback
running, `currentTime` advances between the two reads and the deltas drift:

```javascript
(() => {
  audio.pause();
  const out = [];
  const at = () => audio.currentTime;
  const before = at();
  document.getElementById('pfwd15').click();
  if (Math.abs(at() - (before + 15)) > 0.3) out.push(`forward 15 wrong: ${at() - before}`);
  document.getElementById('pback15').click();
  if (Math.abs(at() - before) > 0.3) out.push(`back 15 wrong: ${at() - before}`);
  // ±15 must cross a boundary rather than clamp to the window
  const idxBefore = playingIndex;
  seekTo(tracks[idxBefore].timestamp + 2);
  document.getElementById('pback15').click();
  if (at() >= tracks[idxBefore].timestamp) out.push("back 15 clamped to the window -- it must not");
  // prev/next must not skip rejected tracks
  const rejectedAt = tracks.findIndex(t => t.rejected);
  if (rejectedAt > 0) {
    playFrom(rejectedAt - 1);
    nextTrack();
    if (playingIndex !== rejectedAt) out.push("next track skipped a rejected track -- it must not");
  }
  return out.length ? out : "ALL TRANSPORT ASSERTIONS PASS";
})()
```

If no track is rejected, temporarily reject one with its ✕ to exercise that
branch, then click ✕ again to restore it. **Do not save.**

Then confirm by eye: the scrubber thumb sits proportionally within the *track*
(near the left edge just after a track starts, not near the left edge of a
4-hour file), and `#ppos` reads e.g. `0:02 in · 2:58 left`.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/web_editor.html
git commit -m "feat(web-editor): scope the scrubber to the playing track and add transport"
```

---

### Task 4: Auto-advance and playing-row highlight

**Files:**
- Modify: `setlist_maker/web_editor.html` — `rowEl()` (line ~171), `render()` (line ~131), the `highlightPlayingRow` stub from Task 3

**Interfaces:**
- Consumes: `trackIndexAt()`, `setPlayingIndex()`, `playingIndex`.
- Produces: `.row.playing` CSS class; a real `highlightPlayingRow(scroll)`.

- [ ] **Step 1: Add the CSS class**

After the `.row.editing` rule (line ~24) add:

```css
  .row.playing { background:#f0f7ff; box-shadow:inset 3px 0 0 var(--accent); }
```

- [ ] **Step 2: Implement `highlightPlayingRow`**

Replace the stub with:

```javascript
  // The highlight must be reapplied after render(), which rebuilds every row.
  function highlightPlayingRow(scroll) {
    const list = document.getElementById("list");
    list.querySelectorAll(".row.playing").forEach(el => el.classList.remove("playing"));
    if (playingIndex === null) return;
    const row = list.children[playingIndex];
    if (!row) return;
    row.classList.add("playing");
    if (scroll) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
```

- [ ] **Step 3: Reapply the highlight after every render**

In `render()` (line ~131), add `highlightPlayingRow(false);` as the last line
of the function, after `updateCounts();`.

- [ ] **Step 4: Advance the player when playback crosses a boundary**

This cannot live inside `syncPlayerUI()`, which returns early when
`playingIndex` is null and would never run the check. Add it as a second
`timeupdate` listener, directly below
`audio.addEventListener("timeupdate", syncPlayerUI);`:

```javascript
  // Playback deliberately runs past a window's end: hearing the transition is
  // how a boundary gets verified. Re-scope to whatever track we are now inside.
  audio.addEventListener("timeupdate", () => {
    const i = trackIndexAt(audio.currentTime);
    if (i !== null && i !== playingIndex) setPlayingIndex(i, { scroll: true });
  });
```

- [ ] **Step 5: Verify the highlight and advance in the browser**

Reload, mute, click ▶ on a row, then:

```javascript
(() => {
  const out = [];
  if (!document.querySelectorAll('.row.playing').length) out.push("no row highlighted");
  if (document.querySelectorAll('.row.playing').length > 1) out.push("more than one row highlighted");
  const i = playingIndex;
  // land 2s before the next boundary and let playback cross it
  seekTo(windowFor(i).end - 2);
  audio.play();
  return "seeded -- re-run the check below in ~4s";
})()
```

Then after ~4 seconds:

```javascript
JSON.stringify({
  advanced: playingIndex,
  highlightedRows: document.querySelectorAll('.row.playing').length,
  labelMatchesIndex: document.getElementById('plabel').textContent.includes(tracks[playingIndex].artist)
})
```

Expected: `advanced` is the *next* index, `highlightedRows` is 1, and
`labelMatchesIndex` is `true`.

Then confirm by eye that clicking a row's ▶ does **not** scroll the list, while
the automatic advance above **did**.

- [ ] **Step 6: Commit**

```bash
git add setlist_maker/web_editor.html
git commit -m "feat(web-editor): follow playback across track boundaries"
```

---

### Task 5: Keyboard shortcuts

**Files:**
- Modify: `setlist_maker/web_editor.html` — the existing `keydown` listener (lines ~164-166)

**Interfaces:**
- Consumes: `skip()`, `prevTrack()`, `nextTrack()`, `SKIP_SECONDS`.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Extend the existing listener**

The page has exactly one `document`-level `keydown` listener, handling `Escape`
for the artwork overlay. Extend it rather than adding a second. Replace lines
~164-166 with:

```javascript
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.classList.contains("open")) { overlay.click(); return; }
    // Never steal keys from an editor: artist/title/time fields and the summary
    // textarea all need Space and the arrows to behave normally.
    const el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.getElementById("player").classList.contains("disabled")) return;
    switch (e.key) {
      case " ":        e.preventDefault(); document.getElementById("pplay").click(); break;
      case "ArrowLeft":  e.preventDefault(); skip(-SKIP_SECONDS); break;
      case "ArrowRight": e.preventDefault(); skip(SKIP_SECONDS); break;
      case "ArrowUp":    e.preventDefault(); prevTrack(); break;
      case "ArrowDown":  e.preventDefault(); nextTrack(); break;
    }
  });
```

`preventDefault` on Space and the arrows stops the page scrolling, and stops
Space re-triggering a focused transport button (which would toggle twice).

- [ ] **Step 2: Verify suppression and action in the browser**

Reload, mute, click ▶ on a row, and confirm by hand:

1. Press `→` — playback jumps forward 15 s.
2. Press `Space` — playback pauses; press again — resumes.
3. Press `↓` — the next track starts.
4. Click a track's artist text to open its editor, type `a b c` **with spaces**
   into the artist field, and confirm playback does **not** toggle and the
   spaces appear in the field. Press `Escape` to abandon the edit.
5. Click into the set-description textarea, press `←`/`→`, and confirm the
   caret moves and playback does not seek. Click away without editing.

Then confirm no double-toggle:

```javascript
document.getElementById('pplay').focus();
// press Space by hand here; playback must toggle exactly once
```

- [ ] **Step 3: Commit**

```bash
git add setlist_maker/web_editor.html
git commit -m "feat(web-editor): add player keyboard shortcuts"
```

---

### Task 6: Server-side presence tests and documentation

**Files:**
- Modify: `tests/test_web_editor.py` (extend `test_page_asset_exists_and_has_hooks`, add one new test)
- Modify: `CLAUDE.md` (web-editor section)
- Modify: `README.md` ("Editing in the browser" section)

**Interfaces:**
- Consumes: the element IDs from Task 2.
- Produces: nothing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_editor.py`:

```python
def test_page_asset_has_player_controls():
    """The transport controls and their IDs are the contract the script binds to."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    for element_id in ("pprev", "pback15", "pplay", "pfwd15", "pnext", "ppos", "pcount", "seek"):
        assert f'id="{element_id}"' in html, f"missing #{element_id}"


def test_body_padding_clears_the_player_bar():
    """The list must not hide behind the fixed player.

    Substring-level, because there is no JS harness -- it catches the specific
    regression of growing the bar without growing the padding.
    """
    import re

    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    padding = re.search(r"body\s*\{[^}]*padding-bottom:\s*(\d+)px", html)
    assert padding, "body padding-bottom not found"
    assert int(padding.group(1)) >= 100, (
        f"body padding-bottom is {padding.group(1)}px; the reworked player bar is ~84px "
        f"and needs more clearance than that"
    )
```

- [ ] **Step 2: Run them to verify they pass against the finished markup**

Run: `uv run pytest tests/test_web_editor.py -q`
Expected: PASS (Tasks 2-5 already added the markup these assert on).

To confirm they have teeth, temporarily change `padding-bottom:104px` to
`padding-bottom:84px` and re-run — `test_body_padding_clears_the_player_bar`
must FAIL. Restore it.

- [ ] **Step 3: Update CLAUDE.md**

In the `setlist_maker/web_editor.py` section, after the "Editable set
description" bullet, add:

```markdown
- **Track-focused player:** the scrubber spans the *current track's window*
  (its timestamp → the next track's; the last → audio duration), not the whole
  recording. On a 4-hour set the global bar was 13.6 s/px, so fine positioning
  was unavailable; the window takes that to ~0.23 s/px. Windows come from
  `windowFor(i)`, derived on demand so an inserted row needs no bookkeeping,
  and clamped to ≥1s so duplicate timestamps cannot divide by zero. Playback
  deliberately runs *past* a window's end and re-scopes via `trackIndexAt()` —
  hearing the transition is how a boundary gets verified — and ±15s seeks clamp
  only to `[0, duration]`, never to the window, for the same reason. The playing
  row is highlighted (`.row.playing`) and scrolled into view **only** on
  automatic advance, never when the user clicked that row. Keyboard: Space,
  ←/→ ±15s, ↑/↓ prev/next, all suppressed while focus is in an input/textarea.
```

- [ ] **Step 4: Update README.md**

In "Editing in the browser", after the paragraph ending "`--edit` and
`--web-edit` cannot be combined.", add:

```markdown
The player is scoped to whichever track is playing: the scrubber spans that
track's window rather than the whole recording, so positioning inside a track
is precise even on a four-hour set. Transport controls give you ±15 seconds and
previous/next track, and the playing row is highlighted in the list.

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `←` / `→` | Back / forward 15 seconds |
| `↑` / `↓` | Previous / next track |

Playback deliberately continues past the end of a track rather than stopping,
so you can hear whether a boundary lands in the right place; the player follows
along and re-scopes to the new track.
```

- [ ] **Step 5: Run the full suite and lint**

```bash
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_web_editor.py CLAUDE.md README.md
git commit -m "test(web-editor): assert player controls and bar clearance"
```

---

## Final verification (run before opening a PR)

The Python suite cannot see any of this working. Do a last pass in the browser
against the real 56-track set and confirm all of:

1. Bar renders at ~84 px; last list row fully visible.
2. Scrubber spans the track, not the file (`#ppos` counts within the track).
3. All five transport buttons work; ±15 s crosses boundaries rather than clamping.
4. Automatic advance re-scopes label, count, artwork, and highlight, and scrolls.
5. Clicking a row's ▶ does not scroll the list.
6. All five keyboard shortcuts work, and none fires while typing in a field.
7. Confirm nothing was saved. The set lives outside any repo, so record the
   tracklist's checksum before verification and compare after:

```bash
SET="/Users/ray/Library/Mobile Documents/com~apple~CloudDocs/Music/DJ Disarray 🎧"
shasum -a 256 "$SET/2026-07-22-Keys-Lounge_tracklist.md" "$SET/2026-07-22-Keys-Lounge_tracklist.json"
```

Both digests must match the values taken before any browser work began. If
either changed, the editor saved over the user's real tracklist — stop and
report it rather than continuing.
