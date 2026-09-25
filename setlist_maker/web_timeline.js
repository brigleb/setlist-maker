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

  // The windows of the tracks that will actually be written. A rejected track
  // is dropped from the markdown, so its span belongs to whatever kept track
  // precedes it; that is what "merge into previous" means.
  function keptWindows(tracks, duration) {
    const kept = [];
    tracks.forEach((t, i) => { if (!t.rejected) kept.push(i); });
    return kept.map((i, k) => {
      const next = kept[k + 1];
      const end = next !== undefined ? tracks[next].timestamp : Math.max(duration || 0, tracks[i].timestamp + 1);
      return { index: i, start: tracks[i].timestamp, end: Math.max(end, tracks[i].timestamp + 1) };
    });
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

    for (const w of kept) {
      const t = tracks[w.index];
      if (inChain.has(w.index)) continue;
      if (!t.artist && !t.title) {
        issues.push({
          kind: "unidentified", key: "unidentified:" + t.timestamp, index: w.index,
          title: `Unidentified · ${fmt(len(w))}`,
          detail: `${fmt(w.start)}–${fmt(w.end)}: nothing Shazam knew`,
          fix: { label: "Sample 3 more", sample: quarters(w.start, w.end) },
        });
      } else if (len(w) < SHORT_SECONDS && w.index !== kept[0].index) {
        issues.push({
          kind: "short", key: "short:" + t.timestamp, index: w.index,
          title: `Only ${fmt(len(w))} long`,
          detail: `${t.artist || "Unknown artist"} — ${t.title || ""} at ${fmt(w.start)}`,
          fix: { label: "Merge up", reject: [w.index] },
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
      const end = kept[k + 1] ? kept[k + 1].index : tracks.length;
      for (let r = i + 1; r < end; r++) {
        if (idOf(r) !== idOf(i)) byIndex[r].forEach(p => settled.add(p));
      }
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

  return {
    BLIP_SECONDS, SHORT_SECONDS, UNHEARD_SECONDS, HOUR,
    fmt, quarters, cleanTitle, identity, hue, windows, keptWindows, joinProbes, variants, unheard, findIssues,
  };
});
