//! `<set>_tracklist.md`: the writer and the reader.
//!
//! The markdown is authoritative for which tracks exist, and it is the set description's only
//! store, so every rule here exists because a file once lost a row or a description without
//! saying so. A save must produce the same bytes Python's `to_markdown()` does.
//!
//! One deliberate difference from Python: the reader's two warnings are returned, not printed.
//! Two accidental ones, both on input no writer produces: `strip()` here is Rust's `trim()`,
//! which leaves the control characters `\x1c`–`\x1f` that Python's strips, and a time written
//! in non-ASCII digits (which Python's `\d` and `int()` accept) is not read as a time.

use std::fmt;
use std::sync::LazyLock;

use regex::Regex;

use super::track::{Track, Tracklist};

/// The set description is free text the user types, so it can contain any shape this format
/// otherwise uses, including a line that reads exactly like a track. Fencing it makes the file
/// unambiguous instead of merely unlikely (#16). HTML comments, because the `.md` is published
/// as show notes and these render as nothing.
pub const SUMMARY_OPEN_MARKER: &str = "<!-- summary -->";
pub const SUMMARY_CLOSE_MARKER: &str = "<!-- /summary -->";

/// Stand-ins for a field the listing has no value for. Italic, which makes them unmistakable:
/// a real artist is always written **bold**, so no value can serialize to either (#44).
pub const UNIDENTIFIED_MARKER: &str = "*Unidentified*";
pub const UNKNOWN_ARTIST_MARKER: &str = "*Unknown artist*";

/// Either marker, with any number of backslashes already in front of it. Built from the
/// markers, so a renamed marker can't quietly stop being escaped.
static SUMMARY_MARKER_LOOKALIKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"^\\*(?:{}|{})$",
        regex::escape(SUMMARY_OPEN_MARKER),
        regex::escape(SUMMARY_CLOSE_MARKER)
    ))
    .unwrap()
});

/// One listing line, in four shapes, because a track may know its artist, its title, both, or
/// neither (#44); a shape missing here is a row silently deleted on reopen.
///
/// Matched at the start of the stripped line and **unanchored at the end**: anchoring would
/// turn a hand-annotated `1. **A** - T (0:00) [live]` into a deleted row (#45). The artist span
/// may be empty, so a file the old writer damaged into `**** - Title` gives its row back. The
/// title span must not be: allowed to match nothing, it would let the time group bind to a
/// `(m:ss)` inside the title, and `**A** - (1:23) Reprise (10:00)` would read as an empty
/// title at 1:23. The regex crate's leftmost-first matching picks the same span Python's
/// backtracking does.
static TRACK_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        concat!(
            r"^\d+\.\s+",
            r"(?:",
            r"{unknown}\s*-\s*(?P<titled>.+?)",
            r"|",
            r"{unidentified}",
            r"|",
            r"\*\*(?P<artist>.*?)\*\*\s*-\s*(?P<title>.+?)",
            r"|",
            r"\*\*(?P<artist_only>.+?)\*\*",
            r")\s*",
            r"\((?P<time>[0-9]+:[0-9]+(?::[0-9]+)?)\)",
        ),
        unknown = regex::escape(UNKNOWN_ARTIST_MARKER),
        unidentified = regex::escape(UNIDENTIFIED_MARKER),
    ))
    .unwrap()
});

static GENERATED_ON: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\*Generated on (.+)\*").unwrap());

/// Something the reader noticed in a hand-edited file. The parse still stands.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ParseWarning {
    /// An opening marker with nothing closing it: the description was read as unfenced prose.
    UnclosedFence,
    /// A closing marker below the listing swallowed every track into the description.
    FenceSwallowedListing,
}

impl fmt::Display for ParseWarning {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseWarning::UnclosedFence => write!(
                f,
                "'{SUMMARY_OPEN_MARKER}' has no closing marker; reading the set description \
                 as unfenced prose. Some of it may be missing."
            ),
            ParseWarning::FenceSwallowedListing => write!(
                f,
                "the set description runs past the listing; check the \
                 '{SUMMARY_CLOSE_MARKER}' marker's position. No tracks were read from this file."
            ),
        }
    }
}

/// A parsed file and what the reader noticed about it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Parsed {
    pub tracklist: Tracklist,
    pub warnings: Vec<ParseWarning>,
}

/// Neutralizes a description line that would otherwise read as a marker: one leading
/// backslash, which markdown renders away. Escaping and unescaping one backslash is an
/// involution, so a description that really contains `\<!-- /summary -->` survives too. The
/// backslash is spliced in rather than the line rebuilt, so its indentation comes back as is.
pub(crate) fn escape_summary_line(line: &str) -> String {
    let stripped = line.trim();
    if !SUMMARY_MARKER_LOOKALIKE.is_match(stripped) {
        return line.to_string();
    }
    line.replacen(stripped, &format!("\\{stripped}"), 1)
}

/// Undoes [`escape_summary_line`] for one line of a fenced description.
pub(crate) fn unescape_summary_line(line: &str) -> String {
    let stripped = line.trim();
    if stripped.starts_with('\\') && SUMMARY_MARKER_LOOKALIKE.is_match(stripped) {
        return line.replacen(stripped, &stripped[1..], 1);
    }
    line.to_string()
}

fn normalize_newlines(s: &str) -> String {
    s.replace("\r\n", "\n").replace('\r', "\n")
}

impl Tracklist {
    /// The markdown, stamping `generated_on` with the current local time if it's missing.
    pub fn to_markdown(&self) -> String {
        self.to_markdown_stamped(|| chrono::Local::now().format("%Y-%m-%d %H:%M").to_string())
    }

    /// [`Tracklist::to_markdown`] with the stamp for a missing date supplied.
    pub fn to_markdown_stamped(&self, now: impl FnOnce() -> String) -> String {
        let generated = match self.generated_on.as_deref() {
            Some(g) if !g.is_empty() => g.to_string(),
            _ => now(),
        };
        let mut lines = vec![
            format!("# Tracklist: {}", self.source_file),
            String::new(),
            format!("*Generated on {generated}*"),
            String::new(),
        ];

        if let Some(summary) = self.summary.as_deref().filter(|s| !s.trim().is_empty()) {
            // Carriage returns are normalized here because the reader treats a lone CR as a
            // line break: one left in would split the description at a point this never
            // escaped, and a marker after it could close the fence from inside. The blank
            // lines inside the fence keep the description its own paragraph in renderers that
            // escape raw HTML instead of hiding it.
            let summary = normalize_newlines(summary);
            lines.push(SUMMARY_OPEN_MARKER.into());
            lines.push(String::new());
            lines.extend(summary.trim().split('\n').map(escape_summary_line));
            lines.push(String::new());
            lines.push(SUMMARY_CLOSE_MARKER.into());
            lines.push(String::new());
        }

        // Rejected tracks are left out and the rest renumbered. Written from the stripped
        // values, so a field of only spaces is stored as the empty field it already is rather
        // than surviving one round trip and vanishing on the next.
        let kept = self.tracks.iter().filter(|t| !t.rejected);
        for (n, track) in (1..).zip(kept) {
            let time = track.time_str();
            let artist = track.artist.trim();
            let title = track.title.trim();
            lines.push(if track.is_unidentified() {
                format!("{n}. {UNIDENTIFIED_MARKER} ({time})")
            } else if artist.is_empty() {
                format!("{n}. {UNKNOWN_ARTIST_MARKER} - {title} ({time})")
            } else if title.is_empty() {
                format!("{n}. **{artist}** ({time})")
            } else {
                format!("{n}. **{artist}** - {title} ({time})")
            });
        }

        lines.push(String::new());
        lines.join("\n")
    }
}

/// The set description and the lines it occupies, which every later scan skips, so prose can
/// no longer contribute a phantom track, date or header (#16).
///
/// Fenced (what the writer produces): everything between the markers, verbatim bar the
/// whitespace around the whole. Legacy (no fence, files written before it existed): the prose
/// after the `*Generated on` line, ending at the first blank or track-shaped line. That form is
/// ambiguous, and the next save rewrites the file fenced.
fn parse_summary(lines: &[&str], warnings: &mut Vec<ParseWarning>) -> (Option<String>, Vec<bool>) {
    let mut span = vec![false; lines.len()];
    let open = lines.iter().position(|l| l.trim() == SUMMARY_OPEN_MARKER);
    if let Some(start) = open {
        let close = (start + 1..lines.len()).find(|&i| lines[i].trim() == SUMMARY_CLOSE_MARKER);
        if let Some(end) = close {
            let text: Vec<String> = lines[start + 1..end]
                .iter()
                .map(|l| unescape_summary_line(l))
                .collect();
            let text = text.join("\n").trim().to_string();
            span[start..=end].iter_mut().for_each(|s| *s = true);
            return ((!text.is_empty()).then_some(text), span);
        }
        // A damaged file, not an old one: fall back to the legacy scan rather than read the
        // rest of the file as a description, and say so.
        warnings.push(ParseWarning::UnclosedFence);
    }

    let mut summary: Vec<&str> = Vec::new();
    let mut seen_generated = false;
    for (i, line) in lines.iter().enumerate() {
        let stripped = line.trim();
        if stripped.starts_with("*Generated on") {
            seen_generated = true;
            continue;
        }
        if !seen_generated {
            continue;
        }
        if stripped == SUMMARY_OPEN_MARKER || stripped == SUMMARY_CLOSE_MARKER {
            span[i] = true;
            continue;
        }
        if TRACK_LINE.is_match(stripped) {
            break;
        }
        if !stripped.is_empty() {
            summary.push(stripped);
            span[i] = true;
        } else if !summary.is_empty() {
            break;
        }
    }
    let text = summary.join(" ");
    ((!text.is_empty()).then_some(text), span)
}

/// Seconds from `M:SS` or `H:MM:SS`. `None` only for a number too big for an `i64`.
fn parse_time(time: &str) -> Option<i64> {
    let parts: Vec<i64> = time
        .split(':')
        .map(|p| p.parse().ok())
        .collect::<Option<_>>()?;
    match parts[..] {
        [h, m, s] => h
            .checked_mul(3600)?
            .checked_add(m.checked_mul(60)?)?
            .checked_add(s),
        [m, s] => m.checked_mul(60)?.checked_add(s),
        _ => None,
    }
}

/// Reads a markdown tracklist.
pub fn parse_markdown_tracklist(content: &str) -> Parsed {
    // Line endings are normalized once, up front, because the fenced description is kept
    // verbatim: every other field is matched stripped, which absorbs a stray CR, but a
    // multi-line description would carry one into the middle of its text.
    let content = normalize_newlines(content);
    let lines: Vec<&str> = content.trim().split('\n').collect();
    let mut warnings = Vec::new();
    let mut tracklist = Tracklist::default();

    // The description comes first and everything else reads around it: it's prose that may
    // contain a header, a date or a track line, none of which are the file's own (#16).
    let (summary, span) = parse_summary(&lines, &mut warnings);
    tracklist.summary = summary;
    let body: Vec<&str> = lines
        .iter()
        .zip(&span)
        .filter(|&(_, &in_summary)| !in_summary)
        .map(|(l, _)| *l)
        .collect();

    if let Some(header) = body.iter().find(|l| l.starts_with("# Tracklist:")) {
        tracklist.source_file = header.replace("# Tracklist:", "").trim().to_string();
    }
    if let Some(line) = body.iter().find(|l| l.starts_with("*Generated on")) {
        tracklist.generated_on = GENERATED_ON.captures(line).map(|c| c[1].to_string());
    }

    for line in &body {
        let Some(m) = TRACK_LINE.captures(line.trim()) else {
            continue;
        };
        // Each shape fills its own groups; an empty string from the one that matched is the
        // same answer as the fallback.
        let group = |name| m.name(name).map(|g| g.as_str()).filter(|s| !s.is_empty());
        let artist = group("artist").or(group("artist_only")).unwrap_or("");
        let title = group("title").or(group("titled")).unwrap_or("");
        let Some(timestamp) = parse_time(&m["time"]) else {
            continue;
        };
        tracklist.tracks.push(Track {
            // Python tests the unstripped span: empty is None, spaces are Some("").
            original_artist: (!artist.is_empty()).then(|| artist.trim().to_string()),
            original_title: (!title.is_empty()).then(|| title.trim().to_string()),
            ..Track::new(timestamp, artist.trim(), title.trim())
        });
    }

    // A closing marker moved below the listing (only a hand edit puts one there) swallows
    // every track, and the next save would write that back as a well-formed file and take
    // the sidecar's artwork with it. The parse stands, but it says so.
    if tracklist.tracks.is_empty()
        && lines
            .iter()
            .zip(&span)
            .any(|(l, &in_summary)| in_summary && TRACK_LINE.is_match(l.trim()))
    {
        warnings.push(ParseWarning::FenceSwallowedListing);
    }

    Parsed {
        tracklist,
        warnings,
    }
}

#[cfg(test)]
mod tests;
