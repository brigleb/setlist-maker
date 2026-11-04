//! Images the user uploaded in the editor, kept beside the tracklist so they travel with the
//! set (iCloud included) rather than living in a cache that can be cleared.
//!
//! - **Track artwork**, in `<set>_artwork/<hash>.jpg`. A track points at one with
//!   `coverart_url = "upload:<hash>.jpg"`: a reference, not a URL, so nothing ever tries to
//!   fetch it. Named by a hash of the stored bytes, so the same picture uploaded twice is one
//!   file. Hindsight's JPEG encoder isn't Pillow's, so a picture Python stored and the same
//!   picture stored here get different names; both resolve, being plain files.
//! - **The episode cover**, `<set>_cover.jpg`. It belongs to the set, and the sidecar is a
//!   bare list of tracks, so the file's presence *is* the setting.
//!
//! References come back from a hand-editable sidecar, so every read validates the name before
//! it goes anywhere near a path.

use std::fmt;
use std::fs;
use std::io::{self, Cursor};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use image::codecs::jpeg::JpegEncoder;
use image::imageops::FilterType;
use image::{ImageReader, RgbImage, imageops};
use regex::Regex;

use super::fileio::atomic_write;

pub const UPLOAD_PREFIX: &str = "upload:";
/// Python's `$` also matches before a final newline, so a reference with one is accepted too.
static REF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^upload:([0-9a-f]{24})\.jpg\n?\z").unwrap());

/// Refused before the body is read: a cover photo straight off a camera is a few MB.
pub const MAX_UPLOAD_BYTES: usize = 25 * 1024 * 1024;
/// Stored at up to this edge. Podcast apps show the episode cover large.
pub const MAX_STORED_EDGE: u32 = 1400;

#[derive(Debug)]
pub enum UploadError {
    /// Not an image this can decode (HEIC among them).
    Unreadable,
    /// The episode cover names an upload that isn't on disk.
    Missing(String),
    Io(io::Error),
}

impl fmt::Display for UploadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            UploadError::Unreadable => f.write_str(
                "not an image this tool can read (JPEG, PNG, WebP or GIF work; HEIC photos \
                 need exporting as JPEG first)",
            ),
            UploadError::Missing(r) => write!(f, "uploaded image {r:?} is missing"),
            UploadError::Io(e) => e.fmt(f),
        }
    }
}

impl std::error::Error for UploadError {}

impl From<io::Error> for UploadError {
    fn from(e: io::Error) -> Self {
        UploadError::Io(e)
    }
}

/// `2026-09-23-Keys-Lounge` for `2026-09-23-Keys-Lounge_tracklist.md`; else the file stem.
pub fn set_stem(tracklist_path: &Path) -> String {
    let name = tracklist_path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy();
    match name.strip_suffix("_tracklist.md") {
        Some(stem) => stem.to_string(),
        None => tracklist_path
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
    }
}

/// Where this set's uploaded track artwork lives (not created).
pub fn artwork_dir_for(tracklist_path: &Path) -> PathBuf {
    tracklist_path.with_file_name(format!("{}_artwork", set_stem(tracklist_path)))
}

/// Where this set's uploaded episode cover lives, if it has one.
pub fn cover_path_for(tracklist_path: &Path) -> PathBuf {
    tracklist_path.with_file_name(format!("{}_cover.jpg", set_stem(tracklist_path)))
}

/// A well-formed `upload:<hash>.jpg` reference.
pub fn is_upload_ref(value: Option<&str>) -> bool {
    value.is_some_and(|v| REF.is_match(v))
}

/// The file a reference names, or `None` if it's malformed.
pub fn upload_path(artwork_dir: &Path, reference: &str) -> Option<PathBuf> {
    let m = REF.captures(reference)?;
    Some(artwork_dir.join(format!("{}.jpg", &m[1])))
}

/// The stored bytes for a reference, or `None` if it's malformed, missing or empty.
pub fn read_upload(artwork_dir: Option<&Path>, reference: &str) -> Option<Vec<u8>> {
    let data = fs::read(upload_path(artwork_dir?, reference)?).ok()?;
    (!data.is_empty()).then_some(data)
}

/// The middle square of an image (itself when it's square already). Every consumer of a
/// cover is square: uploads, chapter pictures and the episode cover.
pub fn centre_square(image: RgbImage) -> RgbImage {
    let (width, height) = image.dimensions();
    if width == height {
        return image;
    }
    let edge = width.min(height);
    let (left, top) = ((width - edge) / 2, (height - edge) / 2);
    imageops::crop_imm(&image, left, top, edge, edge).to_image()
}

/// Decodes, centre-crops to square, caps the edge and re-encodes as JPEG.
///
/// Square because every consumer is square; re-encoding means what's stored is known to
/// decode, carries no metadata, and is the one format its `.jpg` name claims.
pub fn normalize_image(data: &[u8], max_edge: u32) -> Result<Vec<u8>, UploadError> {
    let decoded = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(|_| UploadError::Unreadable)?
        .decode()
        .map_err(|_| UploadError::Unreadable)?;
    let mut image = centre_square(decoded.to_rgb8());
    if image.width() > max_edge {
        image = imageops::resize(&image, max_edge, max_edge, FilterType::Lanczos3);
    }
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, 90)
        .encode_image(&image)
        .map_err(|e| UploadError::Io(io::Error::other(e)))?;
    Ok(out)
}

/// Normalizes and stores an uploaded image; returns its `upload:` reference.
pub fn store_upload(artwork_dir: &Path, data: &[u8]) -> Result<String, UploadError> {
    let stored = normalize_image(data, MAX_STORED_EDGE)?;
    let name = crate::sha256_hex(&stored);
    let name = &name[..24];
    let path = artwork_dir.join(format!("{name}.jpg"));
    if !path.exists() {
        atomic_write(&path, &stored)?;
    }
    Ok(format!("{UPLOAD_PREFIX}{name}.jpg"))
}

/// Makes an uploaded image the set's episode cover, or removes it (`None`).
pub fn set_episode_cover(
    tracklist_path: &Path,
    reference: Option<&str>,
) -> Result<(), UploadError> {
    let cover = cover_path_for(tracklist_path);
    let Some(reference) = reference else {
        return match fs::remove_file(&cover) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        };
    };
    let data = read_upload(Some(&artwork_dir_for(tracklist_path)), reference)
        .ok_or_else(|| UploadError::Missing(reference.to_string()))?;
    atomic_write(&cover, &data)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageFormat, Rgb, RgbImage};

    fn image_bytes(width: u32, height: u32, color: [u8; 3], format: ImageFormat) -> Vec<u8> {
        let img = RgbImage::from_pixel(width, height, Rgb(color));
        let mut buf = Cursor::new(Vec::new());
        img.write_to(&mut buf, format).unwrap();
        buf.into_inner()
    }

    fn png(width: u32, height: u32) -> Vec<u8> {
        image_bytes(width, height, [200, 20, 21], ImageFormat::Png)
    }

    fn decoded(data: &[u8]) -> (ImageFormat, (u32, u32)) {
        let reader = ImageReader::new(Cursor::new(data))
            .with_guessed_format()
            .unwrap();
        let format = reader.format().unwrap();
        let img = reader.decode().unwrap();
        (format, (img.width(), img.height()))
    }

    fn count(dir: &Path) -> usize {
        fs::read_dir(dir).map(|d| d.count()).unwrap_or(0)
    }

    #[test]
    fn paths_sit_beside_the_tracklist() {
        let md = Path::new("/s/2026-09-23-Keys-Lounge_tracklist.md");
        assert_eq!(
            artwork_dir_for(md),
            Path::new("/s/2026-09-23-Keys-Lounge_artwork")
        );
        assert_eq!(
            cover_path_for(md),
            Path::new("/s/2026-09-23-Keys-Lounge_cover.jpg")
        );
        assert_eq!(set_stem(Path::new("/s/renamed.md")), "renamed");
    }

    #[test]
    fn store_normalizes_to_a_square_jpeg_named_by_its_content() {
        let dir = tempfile::tempdir().unwrap();
        let reference = store_upload(dir.path(), &png(800, 600)).unwrap();
        assert!(is_upload_ref(Some(&reference)));
        let stored = fs::read(upload_path(dir.path(), &reference).unwrap()).unwrap();
        assert_eq!(decoded(&stored), (ImageFormat::Jpeg, (600, 600)));
        // The same picture uploaded again is the same file, not a second copy.
        assert_eq!(store_upload(dir.path(), &png(800, 600)).unwrap(), reference);
        assert_eq!(count(dir.path()), 1);
    }

    #[test]
    fn store_takes_the_middle_of_a_portrait() {
        // Left/right halves differ in colour; a centre crop of a tall image keeps both.
        let mut img = RgbImage::from_pixel(100, 300, Rgb([0, 0, 255]));
        for y in 0..100 {
            for x in 0..100 {
                img.put_pixel(x, y, Rgb([255, 0, 0]));
                img.put_pixel(x, y + 200, Rgb([255, 0, 0]));
            }
        }
        let mut buf = Cursor::new(Vec::new());
        img.write_to(&mut buf, ImageFormat::Png).unwrap();
        let out = normalize_image(&buf.into_inner(), MAX_STORED_EDGE).unwrap();
        let out = image::load_from_memory(&out).unwrap().to_rgb8();
        assert_eq!(out.dimensions(), (100, 100));
        let Rgb([r, _, b]) = *out.get_pixel(50, 50);
        assert!(b > 200 && r < 60, "the blue middle, not the red ends");
    }

    #[test]
    fn store_caps_the_edge() {
        let dir = tempfile::tempdir().unwrap();
        let big = image_bytes(3000, 3000, [10, 200, 30], ImageFormat::Jpeg);
        let reference = store_upload(dir.path(), &big).unwrap();
        let stored = read_upload(Some(dir.path()), &reference).unwrap();
        assert_eq!(decoded(&stored).1, (1400, 1400));
    }

    #[test]
    fn gif_and_webp_are_read_too() {
        for format in [ImageFormat::Gif, ImageFormat::WebP] {
            let data = image_bytes(40, 30, [1, 2, 3], format);
            let out = normalize_image(&data, MAX_STORED_EDGE).unwrap();
            assert_eq!(decoded(&out), (ImageFormat::Jpeg, (30, 30)));
        }
    }

    #[test]
    fn store_refuses_what_it_cannot_decode() {
        let mut heic = b"\x00\x00\x00\x18ftypheic".to_vec();
        heic.extend([0; 64]);
        for junk in [b"not an image".to_vec(), heic] {
            let dir = tempfile::tempdir().unwrap();
            let err = store_upload(&dir.path().join("art"), &junk).unwrap_err();
            assert!(err.to_string().contains("HEIC"));
            assert!(!dir.path().join("art").exists());
        }
    }

    #[test]
    fn malformed_references_resolve_to_nothing() {
        let dir = tempfile::tempdir().unwrap();
        for reference in [
            "upload:../../etc/passwd",
            "upload:0123456789abcdef01234567.png",
            "upload:0123456789ABCDEF01234567.jpg",
            "https://example.test/a.jpg",
            "",
        ] {
            assert!(!is_upload_ref(Some(reference)), "{reference}");
            assert_eq!(upload_path(dir.path(), reference), None);
            assert_eq!(read_upload(Some(dir.path()), reference), None);
        }
        assert!(!is_upload_ref(None));
        assert_eq!(
            read_upload(None, "upload:0123456789abcdef01234567.jpg"),
            None
        );
    }

    #[test]
    fn a_trailing_newline_is_accepted_as_pythons_dollar_does() {
        let reference = "upload:0123456789abcdef01234567.jpg\n";
        assert!(is_upload_ref(Some(reference)));
        assert_eq!(
            upload_path(Path::new("/a"), reference),
            Some(PathBuf::from("/a/0123456789abcdef01234567.jpg"))
        );
    }

    #[test]
    fn the_episode_cover_is_set_and_removed() {
        let dir = tempfile::tempdir().unwrap();
        let md = dir.path().join("set_tracklist.md");
        let reference = store_upload(&artwork_dir_for(&md), &png(80, 60)).unwrap();
        set_episode_cover(&md, Some(&reference)).unwrap();
        assert_eq!(
            fs::read(cover_path_for(&md)).unwrap(),
            read_upload(Some(&artwork_dir_for(&md)), &reference).unwrap()
        );
        set_episode_cover(&md, None).unwrap();
        assert!(!cover_path_for(&md).exists());
        set_episode_cover(&md, None).unwrap();
    }

    #[test]
    fn the_episode_cover_refuses_a_missing_upload() {
        let dir = tempfile::tempdir().unwrap();
        let md = dir.path().join("set_tracklist.md");
        let missing = format!("upload:{}.jpg", "0".repeat(24));
        assert!(matches!(
            set_episode_cover(&md, Some(&missing)),
            Err(UploadError::Missing(_))
        ));
    }
}
