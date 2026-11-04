//! A reader for exactly the ID3v2.3 layout the embed writes, kept apart from the `id3` crate so
//! the tests see the bytes on disk, in file order. It panics on anything else, which in a test
//! is the failure wanted. (From spike 4.)

/// Header plus body: where the audio starts. 0 when there's no tag.
pub fn tag_len(b: &[u8]) -> usize {
    if b.len() < 10 || &b[..3] != b"ID3" {
        return 0;
    }
    10 + syncsafe(&b[6..10])
}

fn syncsafe(b: &[u8]) -> usize {
    b.iter().fold(0, |n, &x| (n << 7) | (x & 0x7f) as usize)
}

fn be32(b: &[u8]) -> u32 {
    u32::from_be_bytes(b[..4].try_into().unwrap())
}

#[derive(Clone, Debug)]
pub struct RawFrame {
    pub id: String,
    /// The size field, which v2.3 writes as a plain big-endian integer (#17).
    pub size: usize,
    pub flags: u16,
    /// The text encoding byte, for frames that carry one.
    pub encoding: Option<u8>,
    /// Decoded text of a T*** frame.
    pub text: Option<String>,
    pub data: Vec<u8>,
}

/// v2.3 frames in `body`, up to the padding.
fn walk(body: &[u8]) -> Vec<RawFrame> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos + 10 <= body.len() && body[pos] != 0 {
        let id = String::from_utf8(body[pos..pos + 4].to_vec()).unwrap();
        let size = be32(&body[pos + 4..]) as usize;
        let flags = u16::from_be_bytes([body[pos + 8], body[pos + 9]]);
        let data = body[pos + 10..pos + 10 + size].to_vec();
        let encoding = (id.starts_with('T') || id == "APIC" || id == "COMM").then(|| data[0]);
        let text = (id.starts_with('T') && id != "TXXX").then(|| decode(data[0], &data[1..]));
        out.push(RawFrame {
            id,
            size,
            flags,
            encoding,
            text,
            data,
        });
        pos += 10 + size;
    }
    out
}

fn decode(encoding: u8, b: &[u8]) -> String {
    let s = match encoding {
        0 => b.iter().map(|&c| c as char).collect(),
        1 | 2 => {
            let (be, b) = match b {
                [0xfe, 0xff, rest @ ..] => (true, rest),
                [0xff, 0xfe, rest @ ..] => (false, rest),
                _ => (encoding == 2, b),
            };
            let units: Vec<u16> = b
                .as_chunks::<2>()
                .0
                .iter()
                .map(|p| {
                    if be {
                        u16::from_be_bytes([p[0], p[1]])
                    } else {
                        u16::from_le_bytes([p[0], p[1]])
                    }
                })
                .collect();
            String::from_utf16(&units).unwrap()
        }
        3 => String::from_utf8(b.to_vec()).unwrap(),
        e => panic!("text encoding {e}"),
    };
    s.trim_end_matches('\0').to_string()
}

/// Top-level frames in file order.
pub fn frames(b: &[u8]) -> Vec<RawFrame> {
    assert_eq!(&b[..5], b"ID3\x03\x00", "a v2.3 tag");
    assert_eq!(b[5], 0, "no tag flags");
    walk(&b[10..tag_len(b)])
}

fn cstr(b: &[u8]) -> (String, &[u8]) {
    let end = b.iter().position(|&c| c == 0).unwrap();
    (String::from_utf8(b[..end].to_vec()).unwrap(), &b[end + 1..])
}

#[derive(Clone, Debug)]
pub struct RawChapter {
    pub element_id: String,
    pub start_ms: u32,
    pub end_ms: u32,
    pub start_offset: u32,
    pub end_offset: u32,
    pub sub_frames: Vec<RawFrame>,
}

/// CHAP frames in file order: the order ffprobe lists them in.
pub fn chapters(b: &[u8]) -> Vec<RawChapter> {
    frames(b)
        .into_iter()
        .filter(|f| f.id == "CHAP")
        .map(|f| {
            let (element_id, rest) = cstr(&f.data);
            RawChapter {
                element_id,
                start_ms: be32(rest),
                end_ms: be32(&rest[4..]),
                start_offset: be32(&rest[8..]),
                end_offset: be32(&rest[12..]),
                sub_frames: walk(&rest[16..]),
            }
        })
        .collect()
}

#[derive(Clone, Debug)]
pub struct RawToc {
    pub element_id: String,
    pub flags: u8,
    pub children: Vec<String>,
    pub sub_frames: Vec<RawFrame>,
}

pub fn toc(b: &[u8]) -> Option<RawToc> {
    let f = frames(b).into_iter().find(|f| f.id == "CTOC")?;
    let (element_id, rest) = cstr(&f.data);
    let (flags, count) = (rest[0], rest[1]);
    let mut rest = &rest[2..];
    let mut children = Vec::new();
    for _ in 0..count {
        let (c, r) = cstr(rest);
        children.push(c);
        rest = r;
    }
    Some(RawToc {
        element_id,
        flags,
        children,
        sub_frames: walk(rest),
    })
}

/// Zero bytes after the last frame, inside the tag.
pub fn padding(b: &[u8]) -> usize {
    let body = &b[10..tag_len(b)];
    let used: usize = walk(body).iter().map(|f| 10 + f.size).sum();
    body.len() - used
}
