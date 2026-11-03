// Pure derivations behind the web editor's timeline: no DOM, no fetch, no
// audio. The page draws what these return; tests/test_web_timeline.py runs
// them under Node. Everything here takes the tracklist as the truth about
// which tracks exist and treats the run's probes as evidence laid over it --
// the tracklist is authoritative (it is what gets saved), and deriving tracks
// from the probes instead would put two sources of truth one edit apart.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Timeline = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Tunables. Named so the page and the tests agree on what "short" means.
  const BLIP_SECONDS = 90;      // a track this short between two of the same track is an interruption
  const SHORT_SECONDS = 45;     // shorter than this and it is probably not a track at all
  const UNHEARD_SECONDS = 300;  // a stretch this long with no probe was never listened to
  const HOUR = 3600;

  const REMASTER = /\s*[([](?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))?[)\]]/gi;
  const REMASTER_DASH = /\s+-\s+(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))?\s*$/i;

  function fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(sec).padStart(2, "0");
  }

  // A title with its reissue bookkeeping removed: "(2009 Remaster)",
  // "[Remastered]", "- 2012 Remaster". Nothing else -- "(Vocal)" or
  // "(Maxi Version)" name a different recording and must survive.
  function cleanTitle(title) {
    return (title || "").replace(REMASTER, "").replace(REMASTER_DASH, "").trim();
  }

  // Identity for "is this the same track?" -- lenient on the metadata drift
  // Shazam produces for one recording (remaster tags, featured artists,
  // punctuation, case) and strict on everything else.
  function identity(artist, title) {
    if (!artist && !title) return null;
    const norm = s => (s || "").toLowerCase()
      .replace(/[([](?:feat|ft|with)\.?[^)\]]*[)\]]/g, "")
      .replace(/\s(?:feat|ft)\.?\s.*$/, "")
      .replace(/[^\p{L}\p{N}]+/gu, " ")
      .trim();
    return norm(artist) + "\u0000" + norm(cleanTitle(title));
  }

  // Stable hue per identity, so one track reads as one colour wherever it recurs
  // -- which is what makes an A B A interruption visible before it is read.
  function hue(key) {
    let h = 0;
    for (const c of key || "?") h = (h * 31 + c.charCodeAt(0)) % 360;
    return h;
  }

  // Window i runs from track i's timestamp to the next track's (the last to the
  // end of the audio), clamped to >= 1s -- the same rule as the player's
  // windowFor(), so the grid and the scrubber can never disagree about a track.
  function windows(tracks, duration) {
    return tracks.map((t, i) => {
      const next = tracks[i + 1];
      const end = next ? next.timestamp : Math.max(duration || 0, t.timestamp + 1);
      return { start: t.timestamp, end: Math.max(end, t.timestamp + 1) };
    });
  }

  // The windows of the tracks that will actually be written. A rejected row is
  // dropped from the markdown, so its span goes to a kept neighbour: the one
  // before it ("merge into previous", the default), or, for a row marked
  // merged: "next", the one after it, whose start moves back to that row's
  // (#56). Within one run of rejected rows between two kept tracks, rows go to
  // the earlier track up to the first one marked "next" and to the later track
  // from there on, so both windows stay contiguous. A "next" row with no kept
  // track after it has nothing to join and stays with the one before.
  function keptWindows(tracks, duration) {
    const kept = [];
    tracks.forEach((t, i) => { if (!t.rejected) kept.push(i); });
    const starts = kept.map((i, k) => tracks[startRow(tracks, kept, k)].timestamp);
    return kept.map((i, k) => {
      const end = k + 1 < kept.length ? starts[k + 1] : Math.max(duration || 0, starts[k] + 1);
      return { index: i, start: starts[k], end: Math.max(end, starts[k] + 1) };
    });
  }

  // The row whose timestamp is kept track kept[k]'s start: the first row
  // merged into it from before, else the track itself.
  function startRow(tracks, kept, k) {
    for (let r = k ? kept[k - 1] + 1 : 0; r < kept[k]; r++) if (tracks[r].merged === "next") return r;
    return kept[k];
  }

  // For each row, the index of the kept track that owns its start (itself, for
  // a kept track), or -1 for a rejected row before the first kept window.
  function owners(tracks, duration) {
    const kept = keptWindows(tracks, duration);
    return tracks.map((t, i) => {
      if (!t.rejected) return i;
      const w = kept.find(x => t.timestamp >= x.start && t.timestamp < x.end);
      return w ? w.index : -1;
    });
  }

  // The index of the row holding track i's start, for whatever moves it: the
  // dragged edge, [ and ], the Start fields. A merged row holds its own.
  function startIndex(tracks, i) {
    if (!tracks[i] || tracks[i].rejected) return i;
    const kept = [];
    tracks.forEach((t, j) => { if (!t.rejected) kept.push(j); });
    return startRow(tracks, kept, kept.indexOf(i));
  }

  // Attach each probe to the track whose window holds its midpoint. Probes
  // before the first track's timestamp belong to the first track. A kept track
  // owns its *kept* window, so merging pools the merged rows' evidence into it;
  // a merged row still lists the probes in its own old span, which is what its
  // inspector shows while the user decides whether to unmerge it.
  function joinProbes(tracks, probes, duration) {
    const out = tracks.map(() => []);
    if (!tracks.length) return out;
    const kept = keptWindows(tracks, duration);
    const raw = windows(tracks, duration);
    const home = (ws, mid) => {
      let k = 0;
      while (k + 1 < ws.length && mid >= ws[k + 1].start) k++;
      return k;
    };
    for (const p of probes || []) {
      const mid = p.t + p.window / 2;
      if (kept.length) out[kept[home(kept, mid)].index].push(p);
      const r = home(raw, mid);
      if (tracks[r].rejected) out[r].push(p);
    }
    for (const list of out) list.sort((a, b) => a.t - b.t);
    return out;
  }

  // The names Shazam gave the audio inside one track's window, most-heard first.
  // Names that match the neighbouring tracks are left out: a probe at the edge
  // of a window hearing the next track is boundary evidence, not a second
  // opinion about this one.
  function variants(trackProbes, neighbours) {
    const skip = new Set((neighbours || []).filter(Boolean).map(n => identity(n.artist, n.title)));
    const byName = new Map();
    for (const p of trackProbes || []) {
      if (!p.artist && !p.title) continue;
      if (skip.has(identity(p.artist, p.title))) continue;
      const name = (p.artist || "") + "\u0000" + (p.title || "");
      const v = byName.get(name) || { artist: p.artist || "", title: p.title || "", count: 0, best: 0 };
      v.count++;
      v.best = Math.max(v.best, p.confidence || 0);
      byName.set(name, v);
    }
    return [...byName.values()].sort((a, b) => b.count - a.count || b.best - a.best);
  }

  // Stretches longer than UNHEARD_SECONDS that no probe listened to.
  function unheard(probes, duration, minGap) {
    const gap = minGap || UNHEARD_SECONDS;
    const spans = (probes || []).map(p => [p.t, p.t + p.window]).sort((a, b) => a[0] - b[0]);
    if (!spans.length || !duration) return [];
    const out = [];
    let reached = 0;
    for (const [a, b] of spans) {
      if (a - reached >= gap) out.push([reached, a]);
      reached = Math.max(reached, b);
    }
    if (duration - reached >= gap) out.push([reached, duration]);
    return out;
  }

  // Three moments spread through a span, for "sample 3 more": the quarter
  // points, so no two land on the same ten seconds of audio.
  function quarters(a, b) {
    return [1, 2, 3].map(k => Math.round(a + (b - a) * k / 4));
  }

  // What the user should look at, derived fresh from the current tracklist so
  // a fixed problem simply stops being found. Each issue names the track the
  // timeline should select and, where there is a one-click fix, what it does.
  function findIssues(tracks, probes, duration) {
    const issues = [];
    const kept = keptWindows(tracks, duration);
    const byIndex = joinProbes(tracks, probes, duration);
    const ownerOf = owners(tracks, duration);
    const idOf = i => identity(tracks[i].artist, tracks[i].title);
    const len = w => w.end - w.start;
    const inChain = new Set();

    // A B A -> A: one track interrupted by blips Shazam heard inside it.
    for (let k = 0; k < kept.length; k++) {
      const home = idOf(kept[k].index);
      if (!home || inChain.has(kept[k].index)) continue;
      const absorb = [];
      let pending = [], j = k + 1;
      while (j < kept.length) {
        const id = idOf(kept[j].index);
        if (id === home) {
          if (pending.length) { absorb.push(...pending, kept[j].index); pending = []; }
          else absorb.push(kept[j].index);  // a plain repeat is still the same track
          j++;
        } else if (len(kept[j]) < BLIP_SECONDS) {
          pending.push(kept[j].index);
          j++;
        } else break;
      }
      const blips = absorb.filter(i => idOf(i) !== home);
      if (!absorb.length) continue;
      const t = tracks[kept[k].index];
      absorb.forEach(i => inChain.add(i));
      inChain.add(kept[k].index);
      issues.push({
        kind: "interrupted",
        key: "interrupted:" + t.timestamp,
        index: kept[k].index,
        title: blips.length
          ? `${cleanTitle(t.title) || "A track"} is split by ${blips.length} short blip${blips.length > 1 ? "s" : ""}`
          : `${cleanTitle(t.title) || "A track"} is listed twice in a row`,
        detail: [kept[k].index, ...absorb].map(i => `${fmt(tracks[i].timestamp)} ${tracks[i].artist || "Unidentified"}`).join(" · "),
        fix: { label: "Merge", reject: absorb },
      });
    }

    // Which neighbour a short track more likely belongs to: the neighbours'
    // names heard inside its window, plus its own name heard inside theirs
    // (Shazam reading the same record under the wrong name). A tie keeps the
    // old default, the track before.
    const leansNext = (k) => {
      const i = kept[k].index, p = kept[k - 1].index, n = kept[k + 1].index;
      const me = idOf(i);
      const votes = (other, oid) =>
        byIndex[i].filter(x => oid && identity(x.artist, x.title) === oid).length
        + byIndex[other].filter(x => me && identity(x.artist, x.title) === me).length;
      return votes(n, idOf(n)) > votes(p, idOf(p));
    };

    for (let k = 0; k < kept.length; k++) {
      const w = kept[k], t = tracks[w.index];
      if (inChain.has(w.index)) continue;
      if (!t.artist && !t.title) {
        issues.push({
          kind: "unidentified", key: "unidentified:" + t.timestamp, index: w.index,
          title: `Unidentified · ${fmt(len(w))}`,
          detail: `${fmt(w.start)}–${fmt(w.end)}: nothing Shazam knew`,
          fix: { label: "Sample 3 more", sample: quarters(w.start, w.end) },
        });
      } else if (len(w) < SHORT_SECONDS && kept.length > 1) {
        // Up into the track before, or down into the one after (#56): the
        // first track can only go down, the last only up, and in between the
        // evidence picks the default and the other is offered beside it.
        const up = { label: "Merge up", reject: [w.index] };
        const down = { label: "Merge down", mergeNext: [w.index] };
        const first = k === 0, last = k === kept.length - 1;
        const goDown = first || (!last && leansNext(k));
        issues.push({
          kind: "short", key: "short:" + t.timestamp, index: w.index,
          title: `Only ${fmt(len(w))} long`,
          detail: `${t.artist || "Unknown artist"} — ${t.title || ""} at ${fmt(w.start)}`,
          fix: goDown ? down : up,
          alt: first || last ? null : (goDown ? up : down),
        });
      }
    }

    for (let k = 0; k < kept.length; k++) {
      const i = kept[k].index, t = tracks[i];
      if (!t.artist && !t.title) continue;
      const neighbours = [kept[k - 1], kept[k + 1]].filter(Boolean).map(w => tracks[w.index]);
      if (inChain.has(i)) continue;  // its evidence is about to be pooled by the merge
      // Worth a look only when the disagreement is real: the same artist under
      // another title (Trio's "Da Da Da" in German and in English), or a second
      // name Shazam gave more than once. A lone stray from another artist is the
      // noise any long track collects; the inspector still lists it.
      // A different track the user merged away was a decision, not a question:
      // what was heard inside its span is pooled into this track's evidence (the
      // inspector lists it) but is not a rival worth flagging. A merged *repeat*
      // of this track settles nothing -- that span was never in dispute.
      const settled = new Set();
      ownerOf.forEach((o, r) => {
        if (o === i && r !== i && idOf(r) !== idOf(i)) byIndex[r].forEach(p => settled.add(p));
      });
      const names = variants(byIndex[i].filter(p => !settled.has(p)), neighbours);
      const own = identity(t.artist, "").split("\u0000")[0];
      // Compared by identity, not spelling: once a title is tidied, Shazam's
      // "(2009 Remaster)" spelling of it is the same name, not a rival.
      const mine = identity(t.artist, t.title);
      const rivals = names.filter(n => identity(n.artist, n.title) !== mine);
      const real = rivals.filter(n => n.count >= 2 || identity(n.artist, "").split("\u0000")[0] === own);
      if (!real.length) continue;
      issues.push({
        kind: "variants", key: "variants:" + t.timestamp, index: i,
        title: `Heard under ${real.length + 1} names`,
        detail: [t.title, ...real.map(n => n.title)].map(n => `“${n}”`).join(" vs "),
        fix: null,
      });
    }

    const tagged = kept.map(w => w.index).filter(i => tracks[i].title && cleanTitle(tracks[i].title) !== tracks[i].title);
    if (tagged.length) {
      issues.push({
        kind: "remaster", key: "remaster", index: tagged[0],
        title: `${tagged.length} title${tagged.length > 1 ? "s carry" : " carries"} a remaster tag`,
        detail: tagged.map(i => cleanTitle(tracks[i].title)).join(", "),
        fix: { label: "Tidy", retitle: tagged.map(i => ({ index: i, title: cleanTitle(tracks[i].title) })) },
      });
    }

    for (const [a, b] of unheard(probes, duration)) {
      const w = kept.find(x => x.end > a) || kept[kept.length - 1];
      if (!w) break;
      issues.push({
        kind: "unheard", key: "unheard:" + Math.round(a), index: w.index,
        title: `Never sampled · ${fmt(b - a)}`,
        detail: `${fmt(a)}–${fmt(b)}: no probe listened here`,
        fix: { label: "Sample 3", sample: quarters(a, b) },
      });
    }
    return issues;
  }

  // Where a dragged boundary should snap: the moments the evidence changes from
  // one identified track to another. Shazam fingerprints a centred 10s excerpt
  // of each probe's window, so between two neighbouring probes the cut lies in
  // the gap between the first one's excerpt and the second's; its midpoint is
  // the best guess the probes can give. Unidentified probes are no evidence
  // either way and are skipped over.
  const EXCERPT = 10;
  function snapPoints(probes) {
    const heard = (probes || []).filter(p => p.artist || p.title).slice().sort((a, b) => a.t - b.t);
    const out = [];
    for (let k = 1; k < heard.length; k++) {
      const p = heard[k - 1], q = heard[k];
      if (identity(p.artist, p.title) === identity(q.artist, q.title)) continue;
      const pEnd = p.t + Math.max(0, (p.window - EXCERPT) / 2) + Math.min(EXCERPT, p.window);
      const qStart = q.t + Math.max(0, (q.window - EXCERPT) / 2);
      out.push((pEnd + qStart) / 2);
    }
    return out;
  }

  // ---- the zoomable view (#57) ----
  // A view is { start, span } in seconds across a pane `width` px wide. The
  // deepest zoom is MIN_SPP seconds a pixel; the widest is the whole set.
  const MIN_SPP = 0.05;
  function clampView(v, duration, width) {
    const D = Math.max(duration || 0, 1);
    const span = Math.min(D, Math.max(MIN_SPP * Math.max(width || 1, 1), v.span || D));
    const start = Math.min(Math.max(0, v.start || 0), D - span);
    return { start, span };
  }

  // Zoom by `factor` (above 1 zooms out) keeping the moment under `frac` (0-1
  // across the pane) where it is.
  function zoomAt(v, factor, frac, duration, width) {
    const at = v.start + frac * v.span;
    const span = clampView({ start: 0, span: v.span * factor }, duration, width).span;
    return clampView({ start: at - frac * span, span }, duration, width);
  }

  // Seconds between ruler ticks: the smallest round step at least `minPx` apart.
  const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  function tickStep(span, width, minPx) {
    const per = span / Math.max(width, 1);
    for (const s of STEPS) if (s / per >= (minPx || 70)) return s;
    return STEPS[STEPS.length - 1];
  }

  // Peaks are int8 (min, max) pairs at a fixed rate. Each level halves the
  // one before, so drawing any view reads about one pair per pixel.
  function peakLevels(pairs) {
    const levels = [pairs];
    let cur = pairs;
    while (cur.length > 4) {
      const m = cur.length >> 1, n = (m + 1) >> 1;  // pairs here, pairs in the next level
      const next = new Int8Array(n * 2);
      for (let k = 0; k < n; k++) {
        const two = 2 * k + 1 < m;  // an odd count's last pair has no partner
        next[2 * k] = two ? Math.min(cur[4 * k], cur[4 * k + 2]) : cur[4 * k];
        next[2 * k + 1] = two ? Math.max(cur[4 * k + 1], cur[4 * k + 3]) : cur[4 * k + 1];
      }
      levels.push(next);
      cur = next;
    }
    return levels;
  }

  // One (min, max) per column for `cols` columns covering [start, start+span).
  // Columns past the audio stay 0.
  function peakColumns(levels, rate, start, span, cols) {
    const out = new Int8Array(cols * 2);
    const perCol = span * rate / cols;  // level-0 pairs per column
    const k = Math.max(0, Math.min(levels.length - 1, Math.floor(Math.log2(Math.max(perCol, 1)))));
    const lv = levels[k], r = rate / (1 << k), n = lv.length >> 1;
    for (let c = 0; c < cols; c++) {
      let a = Math.floor((start + c * span / cols) * r);
      // Rounded up: a coarse bin the column only partly covers still counts.
      let b = Math.max(a + 1, Math.ceil((start + (c + 1) * span / cols) * r));
      a = Math.max(0, a); b = Math.min(n, b);
      let lo = 0, hi = 0;
      for (let j = a; j < b; j++) {
        if (lv[2 * j] < lo) lo = lv[2 * j];
        if (lv[2 * j + 1] > hi) hi = lv[2 * j + 1];
      }
      out[2 * c] = lo; out[2 * c + 1] = hi;
    }
    return out;
  }


  // ---- the command palette (#59) ----
  // How well `query` matches `text`: -1 for no match, higher is better. Each
  // space-separated word of the query must match on its own. A word found
  // whole scores far above one found only as scattered letters, and more at
  // the start of a word, so "luvm" ranks The Luvmenauts above a command whose
  // label merely contains l, u, v and m in order. Case and accents are ignored.
  function fold(s) {
    return String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }
  function fuzzy(query, text) {
    const words = fold(query).split(/\s+/).filter(Boolean);
    if (!words.length) return 0;
    const t = fold(text);
    const startsWord = i => i === 0 || !/[a-z0-9]/.test(t[i - 1]);
    let total = 0;
    for (const w of words) {
      let best = -1;
      for (let i = t.indexOf(w); i !== -1; i = t.indexOf(w, i + 1)) {
        const s = 1000 + (startsWord(i) ? 500 : 0) - Math.min(i, 200);
        if (s > best) best = s;
      }
      if (best === -1) {
        // Scattered letters, in order: a weak match, docked for every gap.
        let at = -1, s = 100;
        for (const ch of w) {
          const j = t.indexOf(ch, at + 1);
          if (j === -1) return -1;
          s += j === at + 1 ? 10 : (startsWord(j) ? 4 : -2);
          at = j;
        }
        best = s;
      }
      total += best;
    }
    return total;
  }

  // The selected kept tracks grouped into runs that sit next to each other in
  // kept order (#77): a merged row between two selected tracks doesn't break a
  // run, an unselected kept track does. `indices` are positions in `tracks`;
  // each run is a list of them, in order. Merging a selection collapses each
  // run of two or more into one track.
  function selectionRuns(tracks, indices) {
    const sel = new Set(indices), runs = [];
    let cur = null;
    tracks.forEach((t, i) => {
      if (t.rejected) return;
      if (sel.has(i)) { if (!cur) runs.push(cur = []); cur.push(i); } else cur = null;
    });
    return runs;
  }

  return {
    BLIP_SECONDS, SHORT_SECONDS, UNHEARD_SECONDS, HOUR,
    fmt, quarters, cleanTitle, identity, hue, windows, keptWindows, owners, startIndex, joinProbes, variants, unheard, findIssues,
    snapPoints, selectionRuns, MIN_SPP, clampView, zoomAt, tickStep, peakLevels, peakColumns, fuzzy,
  };
});
