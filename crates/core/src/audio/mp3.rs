//! Whether an MP3 states its own length: a Xing, Info or VBRI frame in place of the first
//! audio frame. symphonia reads one when it's there but reports a length either way, so this
//! looks for itself.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// How far past the ID3 tag the first frame is looked for.
const SEARCH: usize = 64 * 1024;

pub fn states_length(path: &Path) -> bool {
    let Ok(mut file) = File::open(path) else {
        return false;
    };
    let mut head = vec![0; 10];
    if file.read_exact(&mut head).is_err() {
        return false;
    }
    let mut skip = 0u64;
    if &head[..3] == b"ID3" {
        let size = head[6..10]
            .iter()
            .fold(0u64, |n, b| (n << 7) | u64::from(b & 0x7f));
        let footer = if head[5] & 0x10 != 0 { 10 } else { 0 };
        skip = 10 + size + footer;
    }
    // The tag can be megabytes: an embedded set carries every chapter's artwork.
    let mut buf = Vec::with_capacity(SEARCH);
    if file.seek(SeekFrom::Start(skip)).is_err()
        || file.take(SEARCH as u64).read_to_end(&mut buf).is_err()
    {
        return false;
    }
    first_frame_states_length(&buf)
}

/// Finds the first plausible MPEG audio Layer III header in `buf` and checks for a Xing or
/// Info tag after its side information, or a VBRI tag 32 bytes after the header.
pub fn first_frame_states_length(buf: &[u8]) -> bool {
    let Some(at) = (0..buf.len().saturating_sub(4)).find(|&i| is_layer3_header(&buf[i..i + 4]))
    else {
        return false;
    };
    let h = &buf[at..];
    let mpeg1 = (h[1] >> 3) & 3 == 3;
    let mono = h[3] >> 6 == 3;
    let side = match (mpeg1, mono) {
        (true, true) => 17,
        (true, false) => 32,
        (false, true) => 9,
        (false, false) => 17,
    };
    let tag_at = |off: usize| h.get(4 + off..8 + off);
    matches!(tag_at(side), Some(b"Xing") | Some(b"Info")) || tag_at(32) == Some(b"VBRI")
}

fn is_layer3_header(h: &[u8]) -> bool {
    h[0] == 0xff
        && h[1] & 0xe0 == 0xe0
        && (h[1] >> 3) & 3 != 1 // version reserved
        && (h[1] >> 1) & 3 == 1 // Layer III
        && !matches!(h[2] >> 4, 0 | 15) // free or bad bitrate
        && (h[2] >> 2) & 3 != 3 // reserved sample rate
}
