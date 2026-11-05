//! The audio's byte ranges: what part of the recording a request gets.

/// `[start, start + len)` of a file of `size` bytes, and whether it's a partial answer (206),
/// as Python's `_send_audio` read the header: a malformed header or none is the whole file,
/// `bytes=a-` runs to the end, a range past the end is unsatisfiable (`None`). `cap`, when
/// set, shortens a range to at most that many bytes and makes the answer partial: the app's
/// webview gets each reply as one buffer, so a whole 350MB file would be one allocation.
pub fn byte_range(header: Option<&str>, size: u64, cap: Option<u64>) -> Option<(u64, u64, bool)> {
    let last = size.checked_sub(1);
    let mut range = (0, last, false);
    if let Some(spec) = header.and_then(|h| h.strip_prefix("bytes=")) {
        let (lo, hi) = spec.split_once('-').unwrap_or((spec, ""));
        let parse = |s: &str| s.trim().parse::<u64>();
        let start = if lo.trim().is_empty() {
            Ok(0)
        } else {
            parse(lo)
        };
        let end = if hi.trim().is_empty() {
            Ok(last)
        } else {
            parse(hi).map(|h| last.map(|l| h.min(l)))
        };
        if let (Ok(start), Ok(end)) = (start, end) {
            match end {
                Some(end) if start <= end => range = (start, Some(end), true),
                _ => return None,
            }
        }
    }
    let (start, end, partial) = range;
    let Some(end) = end else {
        return Some((0, 0, false)); // an empty file
    };
    let len = end - start + 1;
    match cap {
        Some(cap) if len > cap => Some((start, cap, true)),
        _ => Some((start, len, partial)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranges_read_as_python_read_them() {
        let r = |h| byte_range(h, 1000, None);
        assert_eq!(r(None), Some((0, 1000, false)));
        assert_eq!(r(Some("bytes=0-1")), Some((0, 2, true)));
        assert_eq!(r(Some("bytes=10-")), Some((10, 990, true)));
        assert_eq!(r(Some("bytes=990-5000")), Some((990, 10, true)));
        // Malformed: the whole file (test_get_audio_ignores_malformed_range).
        assert_eq!(r(Some("bytes=abc-def")), Some((0, 1000, false)));
        assert_eq!(r(Some("items=0-1")), Some((0, 1000, false)));
        // Past the end, or backwards: 416.
        assert_eq!(r(Some("bytes=1000-")), None);
        assert_eq!(r(Some("bytes=50-10")), None);
    }

    #[test]
    fn a_cap_shortens_the_answer_and_makes_it_partial() {
        assert_eq!(byte_range(None, 1000, Some(100)), Some((0, 100, true)));
        assert_eq!(
            byte_range(Some("bytes=950-"), 1000, Some(100)),
            Some((950, 50, true))
        );
        assert_eq!(
            byte_range(Some("bytes=0-1"), 1000, Some(100)),
            Some((0, 2, true))
        );
    }

    #[test]
    fn an_empty_file_is_an_empty_answer() {
        assert_eq!(byte_range(None, 0, None), Some((0, 0, false)));
        assert_eq!(byte_range(Some("bytes=0-1"), 0, None), None);
    }
}
