// Pure formatting helpers.

export function fmt(s) {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = (h > 0) ? String(m).padStart(2, "0") : String(m);
  return (h > 0 ? h + ":" : "") + mm + ":" + String(sec).padStart(2, "0");
}

// Parse "M:SS", "MM:SS", or "H:MM:SS" into seconds; null if unparseable.
export function parseTime(str) {
  const parts = (str || "").trim().split(":");
  if (parts.length < 2 || parts.length > 3 || !parts.every(p => /^\d+$/.test(p.trim()))) return null;
  const n = parts.map(p => parseInt(p, 10));
  return n.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1];
}

export function trackName(t) {
  if (!t.artist && !t.title) return "Unidentified";
  return (t.artist || "Unknown artist") + (t.title ? " — " + t.title : "");
}
