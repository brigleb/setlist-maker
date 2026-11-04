//! What Shazam hears and what it answers: the signature of a window's centred 10 seconds,
//! and the request that sends it.

pub mod client;
mod hanning;
pub mod signature;

pub use client::{Client, Heard, NoAnswer};
pub use signature::{EXCERPT_SECONDS, Fingerprint, SAMPLE_RATE, fingerprint};
