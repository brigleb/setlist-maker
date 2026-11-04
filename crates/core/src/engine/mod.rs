//! The adaptive engine (M2): pure, with no I/O, no clock and no async. It folds completed
//! probes into a model of the recording, says where to probe next and what the recording
//! contains. Everything here is a port of Python's `identity.py` and `boundary.py` that must
//! fold every run to byte-for-byte the same answer, which is why `py` exists.

pub mod boundary;
mod difflib;
pub mod identity;
#[cfg(test)]
pub(crate) mod oracle;
#[cfg(test)]
mod properties;
pub mod py;
#[cfg(test)]
mod pyrandom;
