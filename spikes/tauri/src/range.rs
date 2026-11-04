//! Which bytes of the audio file one request gets. A custom-scheme response is a whole `Vec`,
//! not a stream, so every ranged answer is capped at `cap` bytes; the webview asks again for
//! the rest. Malformed headers are served as if absent, as Python's `_send_audio` does.

#[derive(Debug, PartialEq, Eq)]
pub enum Slice {
    /// 200: the whole file (no usable `Range` header).
    Full { len: u64 },
    /// 206: bytes `start..=end` of `size`.
    Partial { start: u64, end: u64, size: u64 },
    /// 416, with `Content-Range: bytes */size`.
    Unsatisfiable { size: u64 },
}

pub fn slice(header: Option<&str>, size: u64, cap: u64) -> Slice {
    let full = Slice::Full { len: size };
    let Some(spec) = header.and_then(|h| h.trim().strip_prefix("bytes=")) else {
        return full;
    };
    // Only the first range of a multi-range request; nothing here sends multipart.
    let spec = spec.split(',').next().unwrap_or("").trim();
    let Some((lo, hi)) = spec.split_once('-') else {
        return full;
    };
    let (lo, hi) = (lo.trim(), hi.trim());
    let (start, end) = match (lo.is_empty(), hi.is_empty()) {
        // bytes=-n: the last n bytes.
        (true, false) => match hi.parse::<u64>() {
            Ok(0) => return Slice::Unsatisfiable { size },
            Ok(n) => (size.saturating_sub(n), size.saturating_sub(1)),
            Err(_) => return full,
        },
        (false, true) => match lo.parse::<u64>() {
            Ok(a) => (a, size.saturating_sub(1)),
            Err(_) => return full,
        },
        (false, false) => match (lo.parse::<u64>(), hi.parse::<u64>()) {
            (Ok(a), Ok(b)) => (a, b.min(size.saturating_sub(1))),
            _ => return full,
        },
        (true, true) => return full,
    };
    if size == 0 || start >= size || start > end {
        return Slice::Unsatisfiable { size };
    }
    let end = end.min(start + cap.max(1) - 1);
    Slice::Partial { start, end, size }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MB: u64 = 1 << 20;

    #[test]
    fn no_header_is_the_whole_file() {
        assert_eq!(slice(None, 100, MB), Slice::Full { len: 100 });
    }

    #[test]
    fn malformed_is_the_whole_file() {
        for h in ["items=0-1", "bytes=x-1", "bytes=1", "bytes=-", "bytes=1-y"] {
            assert_eq!(slice(Some(h), 100, MB), Slice::Full { len: 100 }, "{h}");
        }
    }

    #[test]
    fn closed_range_within_the_cap() {
        assert_eq!(
            slice(Some("bytes=0-1"), 100, MB),
            Slice::Partial {
                start: 0,
                end: 1,
                size: 100
            }
        );
    }

    #[test]
    fn closed_range_past_the_end_is_clamped() {
        assert_eq!(
            slice(Some("bytes=90-500"), 100, MB),
            Slice::Partial {
                start: 90,
                end: 99,
                size: 100
            }
        );
    }

    #[test]
    fn open_range_is_capped() {
        assert_eq!(
            slice(Some("bytes=100-"), 10 * MB, MB),
            Slice::Partial {
                start: 100,
                end: 100 + MB - 1,
                size: 10 * MB
            }
        );
    }

    #[test]
    fn closed_range_is_capped_too() {
        assert_eq!(
            slice(Some("bytes=0-9999999"), 10 * MB, MB),
            Slice::Partial {
                start: 0,
                end: MB - 1,
                size: 10 * MB
            }
        );
    }

    #[test]
    fn suffix_range() {
        assert_eq!(
            slice(Some("bytes=-10"), 100, MB),
            Slice::Partial {
                start: 90,
                end: 99,
                size: 100
            }
        );
        assert_eq!(
            slice(Some("bytes=-500"), 100, MB),
            Slice::Partial {
                start: 0,
                end: 99,
                size: 100
            }
        );
    }

    #[test]
    fn first_of_several_ranges() {
        assert_eq!(
            slice(Some("bytes=0-1, 50-60"), 100, MB),
            Slice::Partial {
                start: 0,
                end: 1,
                size: 100
            }
        );
    }

    #[test]
    fn unsatisfiable() {
        for h in ["bytes=100-", "bytes=100-200", "bytes=50-10", "bytes=-0"] {
            assert_eq!(
                slice(Some(h), 100, MB),
                Slice::Unsatisfiable { size: 100 },
                "{h}"
            );
        }
        assert_eq!(
            slice(Some("bytes=0-"), 0, MB),
            Slice::Unsatisfiable { size: 0 }
        );
    }
}
