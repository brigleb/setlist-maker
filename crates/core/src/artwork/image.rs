//! The picture a chapter (or the episode) embeds: the cover centre-cropped to square, resized
//! to [`CHAPTER_IMAGE_SIZE`] and compressed under [`MAX_IMAGE_BYTES`], or nothing.
//!
//! Python drew a lower-third overlay on each chapter and a gradient card for a track with no
//! art; both are gone (the plan), so a cover that won't decode is no picture rather than a card.

use std::io::Cursor;

use image::codecs::jpeg::JpegEncoder;
use image::imageops::{self, FilterType};
use image::{ImageReader, RgbImage};

use super::CHAPTER_IMAGE_SIZE;
use crate::model::uploads::centre_square;

/// The most an embedded picture may weigh, as Python's `MAX_IMAGE_BYTES`.
pub const MAX_IMAGE_BYTES: usize = 200_000;
/// Where the quality search starts; it steps down by 10 to 30.
pub const JPEG_INITIAL_QUALITY: u8 = 90;

/// The chapter picture for a cover in any format the `image` crate reads, or `None` when it
/// doesn't decode.
pub fn chapter_jpeg(data: &[u8]) -> Option<Vec<u8>> {
    let decoded = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .ok()?
        .decode()
        .ok()?;
    let square = centre_square(decoded.to_rgb8());
    let size = CHAPTER_IMAGE_SIZE;
    let sized = if square.width() == size {
        square
    } else {
        imageops::resize(&square, size, size, FilterType::Lanczos3)
    };
    compress(&sized)
}

fn encode(image: &RgbImage, quality: u8) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, quality)
        .encode_image(image)
        .ok()?;
    Some(out)
}

/// JPEG under [`MAX_IMAGE_BYTES`]: quality 90, 80, … 30, and failing that 400px at 60, as
/// Python's `_compress_to_jpeg`.
fn compress(image: &RgbImage) -> Option<Vec<u8>> {
    for quality in (30..=JPEG_INITIAL_QUALITY).rev().step_by(10) {
        let data = encode(image, quality)?;
        if data.len() <= MAX_IMAGE_BYTES {
            return Some(data);
        }
    }
    let smaller = imageops::resize(image, 400, 400, FilterType::Lanczos3);
    encode(&smaller, 60)
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageFormat, Rgb};

    fn png(width: u32, height: u32, mut paint: impl FnMut(u32, u32) -> [u8; 3]) -> Vec<u8> {
        let img = RgbImage::from_fn(width, height, |x, y| Rgb(paint(x, y)));
        let mut out = Vec::new();
        img.write_to(&mut Cursor::new(&mut out), ImageFormat::Png)
            .unwrap();
        out
    }

    fn decode(jpeg: &[u8]) -> RgbImage {
        image::load_from_memory(jpeg).unwrap().to_rgb8()
    }

    #[test]
    fn a_square_cover_comes_out_at_the_chapter_size() {
        let out = chapter_jpeg(&png(300, 300, |_, _| [10, 120, 90])).unwrap();
        assert_eq!(&out[..2], b"\xff\xd8");
        assert_eq!(decode(&out).dimensions(), (600, 600));
    }

    #[test]
    fn a_wide_cover_is_centre_cropped_not_squashed() {
        // Red | green | red, the green square in the middle: cropping keeps only green.
        let out = chapter_jpeg(&png(900, 300, |x, _| {
            if (300..600).contains(&x) {
                [0, 200, 0]
            } else {
                [200, 0, 0]
            }
        }))
        .unwrap();
        let img = decode(&out);
        for (x, y) in [(5, 300), (300, 300), (594, 300)] {
            let Rgb([r, g, _]) = *img.get_pixel(x, y);
            assert!(g > 150 && r < 60, "({x},{y}) is {r},{g}");
        }
    }

    #[test]
    fn noise_still_fits_under_the_limit() {
        let mut seed = 7u32;
        let out = chapter_jpeg(&png(1200, 1200, |_, _| {
            seed = seed.wrapping_mul(1_103_515_245).wrapping_add(12345);
            let b = seed.to_be_bytes();
            [b[0], b[1], b[2]]
        }))
        .unwrap();
        assert!(out.len() <= MAX_IMAGE_BYTES, "{} bytes", out.len());
    }

    #[test]
    fn something_that_isnt_an_image_is_no_picture() {
        assert_eq!(chapter_jpeg(b"not an image"), None);
        assert_eq!(chapter_jpeg(b""), None);
    }
}
