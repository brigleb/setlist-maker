//! Ported from `tests/test_editor.py` (TestTracklist's markdown cases,
//! TestParseMarkdownTracklist, TestSummaryRoundTrip, TestPartialIdentificationRoundTrip).

use super::*;

const SAMPLE_MARKDOWN: &str = "# Tracklist: test_mix.mp3

*Generated on 2026-01-31 20:00*

1. **Daft Punk** - Around the World (0:00)
2. **The Chemical Brothers** - Block Rockin' Beats (3:00)
3. *Unidentified* (6:00)
4. **Fatboy Slim** - Praise You (9:00)
";

fn sample() -> Tracklist {
    Tracklist {
        source_file: "test_mix.mp3".into(),
        generated_on: Some("2026-01-31 20:00".into()),
        tracks: vec![
            Track::new(0, "Daft Punk", "Around the World"),
            Track::new(180, "The Chemical Brothers", "Block Rockin' Beats"),
            Track::new(360, "", ""),
            Track::new(540, "Fatboy Slim", "Praise You"),
        ],
        summary: None,
    }
}

fn with_summary(summary: &str) -> Tracklist {
    Tracklist {
        summary: Some(summary.into()),
        ..sample()
    }
}

fn parse(md: &str) -> Tracklist {
    parse_markdown_tracklist(md).tracklist
}

fn rows(tl: &Tracklist) -> Vec<(String, String, i64)> {
    tl.tracks
        .iter()
        .map(|t| (t.artist.clone(), t.title.clone(), t.timestamp))
        .collect()
}

fn expected_tracks() -> Vec<(String, String, i64)> {
    rows(&sample())
}

fn mix(tracks: Vec<Track>, summary: Option<&str>) -> Tracklist {
    Tracklist {
        source_file: "mix.mp3".into(),
        generated_on: Some("2026-01-31 20:00".into()),
        tracks,
        summary: summary.map(String::from),
    }
}

// --- writing ---------------------------------------------------------------------------------

#[test]
fn to_markdown() {
    let md = sample().to_markdown();
    for line in [
        "# Tracklist: test_mix.mp3",
        "*Generated on 2026-01-31 20:00*",
        "**Daft Punk** - Around the World (0:00)",
        "**The Chemical Brothers** - Block Rockin' Beats (3:00)",
        "*Unidentified* (6:00)",
        "**Fatboy Slim** - Praise You (9:00)",
    ] {
        assert!(md.contains(line), "{line}");
    }
    // The sample file is exactly what the sample tracklist writes.
    assert_eq!(md, SAMPLE_MARKDOWN);
}

#[test]
fn to_markdown_excludes_rejected() {
    let mut tl = sample();
    tl.tracks[1].rejected = true;
    let md = tl.to_markdown();
    assert!(!md.contains("Block Rockin' Beats"));
    assert!(md.contains("Daft Punk") && md.contains("Fatboy Slim"));
}

#[test]
fn to_markdown_renumbers_after_rejection() {
    let mut tl = sample();
    tl.tracks[0].rejected = true;
    let md = tl.to_markdown();
    assert!(md.contains("1. **The Chemical Brothers**"));
    assert!(md.contains("2. *Unidentified*"));
}

#[test]
fn an_empty_tracklist_still_has_its_header_and_a_date() {
    let tl = Tracklist {
        source_file: "empty.mp3".into(),
        ..Tracklist::default()
    };
    assert_eq!(
        tl.to_markdown_stamped(|| "2026-11-03 19:00".into()),
        "# Tracklist: empty.mp3\n\n*Generated on 2026-11-03 19:00*\n\n"
    );
    assert!(tl.to_markdown().contains("*Generated on "));
}

#[test]
fn an_empty_date_is_stamped_like_a_missing_one() {
    let tl = Tracklist {
        generated_on: Some(String::new()),
        ..sample()
    };
    assert!(
        tl.to_markdown_stamped(|| "NOW".into())
            .contains("*Generated on NOW*")
    );
}

#[test]
fn to_markdown_includes_summary_fenced_before_the_listing() {
    // The blank lines inside the fence are deliberate, and asserted on the bytes because
    // nothing downstream would notice if they went.
    let summary = "A propulsive big-beat set with funk-laced breaks.";
    let md = with_summary(summary).to_markdown();
    let lines: Vec<&str> = md.split('\n').collect();
    let at = lines.iter().position(|l| *l == summary).unwrap();
    let first_track = lines.iter().position(|l| l.starts_with("1. ")).unwrap();
    assert!(at < first_track);
    assert_eq!(lines[at - 2..at], ["<!-- summary -->", ""]);
    assert_eq!(lines[at + 1..at + 4], ["", "<!-- /summary -->", ""]);
}

#[test]
fn to_markdown_omits_summary_when_absent() {
    let md = sample().to_markdown();
    let before = md.split("1. ").next().unwrap();
    assert_eq!(before.matches("\n\n").count(), 2);
}

// --- reading ---------------------------------------------------------------------------------

#[test]
fn parse_basic() {
    let tl = parse(SAMPLE_MARKDOWN);
    assert_eq!(tl.source_file, "test_mix.mp3");
    assert_eq!(tl.generated_on.as_deref(), Some("2026-01-31 20:00"));
    assert_eq!(tl.tracks.len(), 4);
    assert_eq!(tl.summary, None);
}

#[test]
fn parse_tracks_and_unidentified() {
    let tl = parse(SAMPLE_MARKDOWN);
    assert_eq!(rows(&tl), expected_tracks());
    assert!(tl.tracks[2].is_unidentified());
}

#[test]
fn parse_sets_the_originals_from_the_file() {
    let tl = parse(SAMPLE_MARKDOWN);
    assert_eq!(tl.tracks[0].original_artist.as_deref(), Some("Daft Punk"));
    assert_eq!(
        tl.tracks[0].original_title.as_deref(),
        Some("Around the World")
    );
    assert_eq!(tl.tracks[2].original_artist, None);
    assert_eq!(tl.tracks[2].original_title, None);
}

#[test]
fn parse_hour_timestamp() {
    let tl = parse(
        "# Tracklist: long_mix.mp3\n\n*Generated on 2026-01-31*\n\n1. **Artist** - Song (1:30:45)\n",
    );
    assert_eq!(tl.tracks[0].timestamp, 5445);
}

#[test]
fn parse_empty_content() {
    let tl = parse("");
    assert_eq!(tl.source_file, "");
    assert!(tl.tracks.is_empty());
}

#[test]
fn roundtrip() {
    let tl = sample();
    assert_eq!(rows(&parse(&tl.to_markdown())), rows(&tl));
    assert_eq!(parse(&tl.to_markdown()).source_file, tl.source_file);
}

#[test]
fn parse_legacy_summary() {
    // The legacy, unfenced shape: the back-compat guard, not what the writer produces.
    let tl = parse(
        "# Tracklist: mix.mp3

*Generated on 2026-01-31 20:00*

A driving techno set with dub-inflected low end and hypnotic, minimal arrangements.

1. **Artist** - Song (0:00)
",
    );
    assert_eq!(
        tl.summary.as_deref(),
        Some("A driving techno set with dub-inflected low end and hypnotic, minimal arrangements.")
    );
    assert_eq!(tl.tracks.len(), 1);
}

#[test]
fn summary_roundtrip() {
    let tl = with_summary("A propulsive big-beat set with funk-laced breaks.");
    assert_eq!(parse(&tl.to_markdown()).summary, tl.summary);
}

// --- the description survives whatever was typed into it (#16) -------------------------------

#[test]
fn a_description_survives_and_invents_no_tracks() {
    for summary in [
        "1. **Test** - Song (0:00)",
        "2. *Unidentified* (6:00)",
        "Opens with 1. **Test** - Song (0:00) and never looks back.",
        "*Generated on* a rainy Tuesday.",
        "# Tracklist: not-a-real-file.mp3",
        "First paragraph.\n\nSecond paragraph.",
        "A set with\ntwo prose lines.",
        "Ends on a numbered thought.\n\n1. yes",
    ] {
        let back = parse(&with_summary(summary).to_markdown());
        assert_eq!(back.summary.as_deref(), Some(summary), "{summary:?}");
        assert_eq!(rows(&back), expected_tracks(), "{summary:?}");
    }
}

#[test]
fn a_description_cannot_close_its_own_fence() {
    let escaped = format!("\\{SUMMARY_CLOSE_MARKER}");
    for summary in [
        SUMMARY_CLOSE_MARKER,
        SUMMARY_OPEN_MARKER,
        &escaped,
        "Fenced like\n<!-- /summary -->\nthis.",
    ] {
        let back = parse(&with_summary(summary).to_markdown());
        assert_eq!(back.summary.as_deref(), Some(summary), "{summary:?}");
        assert_eq!(rows(&back), expected_tracks(), "{summary:?}");
    }
}

#[test]
fn escaping_a_line_is_an_involution() {
    // A near-miss like "<!-- summary --> trailing" must come back with no backslash it didn't
    // have: escaping more than the fence would put marks in the user's prose.
    for line in [
        "ordinary prose".to_string(),
        SUMMARY_OPEN_MARKER.to_string(),
        SUMMARY_CLOSE_MARKER.to_string(),
        format!("\\{SUMMARY_CLOSE_MARKER}"),
        format!("\\\\{SUMMARY_OPEN_MARKER}"),
        format!("  {SUMMARY_CLOSE_MARKER}  "),
        "<!-- summary --> trailing".to_string(),
    ] {
        assert_eq!(unescape_summary_line(&escape_summary_line(&line)), line);
    }
    assert_eq!(
        escape_summary_line("  <!-- /summary -->"),
        "  \\<!-- /summary -->"
    );
    assert_eq!(
        escape_summary_line("<!-- summary --> trailing"),
        "<!-- summary --> trailing"
    );
}

#[test]
fn header_and_date_come_from_the_file_not_the_description() {
    let tl = with_summary("# Tracklist: fake.mp3\n*Generated on 1999-01-01 00:00*");
    let back = parse(&tl.to_markdown());
    assert_eq!(back.source_file, "test_mix.mp3");
    assert_eq!(back.generated_on.as_deref(), Some("2026-01-31 20:00"));
    assert_eq!(back.summary, tl.summary);
}

#[test]
fn an_empty_fence_parses_as_no_summary() {
    let back = parse(&format!(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n{SUMMARY_OPEN_MARKER}\n\
         {SUMMARY_CLOSE_MARKER}\n\n1. **Artist** - Song (0:00)\n"
    ));
    assert_eq!(back.summary, None);
    assert_eq!(back.tracks.len(), 1);
}

#[test]
fn a_whitespace_only_description_writes_no_fence() {
    let md = with_summary("   ").to_markdown();
    assert!(!md.contains(SUMMARY_OPEN_MARKER));
    assert_eq!(parse(&md).summary, None);
}

#[test]
fn resaving_is_byte_identical() {
    // A parser that read its own markers back as prose would nest another pair each save.
    let once = with_summary("1. **Test** - Song (0:00)").to_markdown();
    let twice = parse(&once).to_markdown();
    assert_eq!(twice, once);
    assert_eq!(once.matches(SUMMARY_OPEN_MARKER).count(), 1);
}

#[test]
fn legacy_markdown_upgrades_in_place_on_the_next_save() {
    let legacy = "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\nA driving techno set.\n\n1. **Artist** - Song (0:00)\n";
    let upgraded = parse(legacy).to_markdown();
    assert!(upgraded.contains(SUMMARY_OPEN_MARKER));
    assert_eq!(
        parse(&upgraded).summary.as_deref(),
        Some("A driving techno set.")
    );
}

#[test]
fn a_carriage_return_cannot_split_a_line_open() {
    // A lone CR is a line break to the reader, so the writer makes it one first; otherwise
    // "\r<!-- /summary -->" would reach the file unescaped and close the fence from inside.
    let tl = with_summary("Opening line.\r<!-- /summary -->\r1. **Ghost** - It (9:00)");
    let back = parse(&tl.to_markdown());
    assert_eq!(
        back.summary.as_deref(),
        Some("Opening line.\n<!-- /summary -->\n1. **Ghost** - It (9:00)")
    );
    assert_eq!(rows(&back), expected_tracks());
}

#[test]
fn crlf_files_read_like_lf_ones() {
    let crlf = with_summary("Two\nlines.")
        .to_markdown()
        .replace('\n', "\r\n");
    let back = parse(&crlf);
    assert_eq!(back.summary.as_deref(), Some("Two\nlines."));
    assert_eq!(rows(&back), expected_tracks());
}

#[test]
fn a_fence_closed_past_the_listing_says_so() {
    let parsed = parse_markdown_tracklist(&format!(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n{SUMMARY_OPEN_MARKER}\n\
         A techno set.\n\n1. **Daft Punk** - Around the World (0:00)\n\
         2. **Kraftwerk** - Autobahn (3:00)\n\n{SUMMARY_CLOSE_MARKER}\n"
    ));
    assert!(parsed.tracklist.tracks.is_empty());
    assert_eq!(parsed.warnings, [ParseWarning::FenceSwallowedListing]);
    assert!(
        parsed.warnings[0]
            .to_string()
            .contains("runs past the listing")
    );
}

#[test]
fn an_unclosed_fence_does_not_swallow_the_listing() {
    let parsed = parse_markdown_tracklist(&format!(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n{SUMMARY_OPEN_MARKER}\n\
         A driving techno set.\n\n1. **Artist** - Song (0:00)\n2. **Other** - Tune (4:00)\n"
    ));
    assert_eq!(
        parsed.tracklist.summary.as_deref(),
        Some("A driving techno set.")
    );
    let times: Vec<i64> = parsed
        .tracklist
        .tracks
        .iter()
        .map(|t| t.timestamp)
        .collect();
    assert_eq!(times, [0, 240]);
    assert_eq!(parsed.warnings, [ParseWarning::UnclosedFence]);
    assert!(parsed.warnings[0].to_string().contains("no closing marker"));
}

#[test]
fn legacy_markdown_without_delimiters_parses_without_a_warning() {
    let parsed = parse_markdown_tracklist(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n\
         A driving techno set with dub-inflected low end.\n\n\
         1. **Artist** - Song (0:00)\n2. **Other** - Tune (4:00)\n",
    );
    assert_eq!(
        parsed.tracklist.summary.as_deref(),
        Some("A driving techno set with dub-inflected low end.")
    );
    assert_eq!(parsed.tracklist.tracks.len(), 2);
    assert!(parsed.warnings.is_empty());
}

// --- partially identified rows (#44, #45) -----------------------------------------------------

#[test]
fn every_emptiness_shape_round_trips() {
    for (artist, title) in [
        ("Daft Punk", "One More Time"),
        ("", "Titled Only"),
        ("Artist Only", ""),
        ("", ""),
    ] {
        let tl = mix(vec![Track::new(180, artist, title)], None);
        assert_eq!(
            rows(&parse(&tl.to_markdown())),
            [(artist.to_string(), title.to_string(), 180)]
        );
    }
}

#[test]
fn a_title_starting_with_a_time_keeps_its_own_timestamp() {
    for (artist, title) in [
        ("Kraftwerk", "(1:23) Reprise"),
        ("", "(1:23) Reprise"),
        ("Aphex Twin", "(4:33)"),
        ("Kraftwerk", "(1:02:03) Reprise"),
    ] {
        let tl = mix(vec![Track::new(600, artist, title)], None);
        assert_eq!(
            rows(&parse(&tl.to_markdown())),
            [(artist.to_string(), title.to_string(), 600)],
            "{title}"
        );
    }
}

#[test]
fn a_titled_row_with_no_artist_is_never_dropped() {
    for position in 0..3 {
        for summary in [None, Some("A late-night set.")] {
            let mut tracks = vec![
                Track::new(0, "Daft Punk", "One More Time"),
                Track::new(360, "Kraftwerk", "Autobahn"),
            ];
            tracks.insert(position, Track::new(180, "", "Titled Only"));
            let tl = mix(tracks, summary);
            let back = parse(&tl.to_markdown());
            assert_eq!(rows(&back), rows(&tl));
            assert_eq!(back.summary.as_deref(), summary);
        }
    }
}

#[test]
fn partial_rows_read_as_show_notes() {
    let tl = mix(
        vec![
            Track::new(0, "", "Titled Only"),
            Track::new(180, "Artist Only", ""),
            Track::new(360, "", ""),
        ],
        None,
    );
    let md = tl.to_markdown();
    let listing: Vec<&str> = md
        .split('\n')
        .filter(|l| l.starts_with(|c: char| c.is_ascii_digit()))
        .collect();
    assert_eq!(
        listing,
        [
            "1. *Unknown artist* - Titled Only (0:00)",
            "2. **Artist Only** (3:00)",
            "3. *Unidentified* (6:00)",
        ]
    );
}

#[test]
fn resaving_a_partial_row_is_byte_identical() {
    let first = mix(
        vec![
            Track::new(0, "", "Titled Only"),
            Track::new(180, "Artist Only", ""),
        ],
        None,
    )
    .to_markdown();
    assert_eq!(parse(&first).to_markdown(), first);
}

#[test]
fn a_whitespace_only_field_is_written_as_the_empty_field_it_is() {
    for (artist, title, expected) in [
        ("   ", "Song", "1. *Unknown artist* - Song (0:00)"),
        ("Artist", "   ", "1. **Artist** (0:00)"),
        ("   ", "   ", "1. *Unidentified* (0:00)"),
        ("  Artist  ", "  Song  ", "1. **Artist** - Song (0:00)"),
    ] {
        let md = mix(vec![Track::new(0, artist, title)], None).to_markdown();
        assert!(md.contains(expected), "{expected}");
        assert_eq!(parse(&md).to_markdown(), md);
    }
}

#[test]
fn an_artist_named_like_a_marker_is_still_a_real_artist() {
    for artist in [
        "Unknown artist",
        "*Unknown artist*",
        "Unidentified",
        "*Unidentified*",
    ] {
        let tl = mix(vec![Track::new(0, artist, "Song")], None);
        let back = parse(&tl.to_markdown());
        assert_eq!(
            (
                back.tracks[0].artist.as_str(),
                back.tracks[0].title.as_str()
            ),
            (artist, "Song")
        );
    }
}

#[test]
fn a_file_damaged_by_the_old_writer_recovers_its_track() {
    let tl = parse(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n\
         1. **Daft Punk** - One More Time (0:00)\n2. **** - Titled Only (3:00)\n\
         3. **Kraftwerk** - Autobahn (6:00)\n",
    );
    assert_eq!(
        rows(&tl),
        [
            ("Daft Punk".into(), "One More Time".into(), 0),
            (String::new(), "Titled Only".into(), 180),
            ("Kraftwerk".into(), "Autobahn".into(), 360),
        ]
    );
}

#[test]
fn the_old_dangling_dash_shape_still_parses() {
    let tl = parse(
        "# Tracklist: mix.mp3\n\n*Generated on 2026-01-31 20:00*\n\n1. **Artist Only** -  (0:00)\n",
    );
    assert_eq!(rows(&tl), [("Artist Only".into(), String::new(), 0)]);
}

#[test]
fn an_annotated_line_keeps_its_row() {
    // Unanchored at the end, so a hand annotation after the time is not a deleted row (#45).
    let tl = parse("# Tracklist: m.mp3\n\n*Generated on x*\n\n1. **A** - T (0:00) [live]\n");
    assert_eq!(rows(&tl), [("A".into(), "T".into(), 0)]);
}

#[test]
fn a_whitespace_only_span_is_an_empty_original_not_a_missing_one() {
    // Python tests the unstripped span: `**   ** - T` has an artist span, so its original is
    // "" rather than None. That decides `was_corrected` after a later edit.
    let tl = parse("# Tracklist: m.mp3\n\n*Generated on x*\n\n1. **   ** - T (0:00)\n");
    assert_eq!(tl.tracks[0].artist, "");
    assert_eq!(tl.tracks[0].original_artist.as_deref(), Some(""));
}
