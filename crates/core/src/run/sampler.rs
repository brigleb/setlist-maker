//! Click-to-sample outside a run: one Shazam lookup at a moment the user picked, in the
//! editor. Python's `sampler.ShazamSampler`.
//!
//! The page can ask for several at once, but Shazam's limit is burst-sensitive, so lookups
//! queue on one lock and are spaced [`DELAY`](super::DELAY) apart, as a run's are, counting
//! from the last one even when it failed. The answer is a probe like any other but for its
//! purpose, so the progress file keeps it as evidence a later resume replays.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use super::{Listener, MANUAL, SAMPLE_WINDOW, sample_window_start};
use crate::model::Probe;
use crate::shazam::NoAnswer;

pub struct Sampler<L: Listener> {
    state: Mutex<(L, Option<Instant>)>,
    interval: Duration,
}

impl<L: Listener> Sampler<L> {
    /// `last_call`: when Shazam was last asked, if just now (a run that has only finished).
    pub fn new(listener: L, interval: Duration, last_call: Option<Instant>) -> Self {
        Sampler {
            state: Mutex::new((listener, last_call)),
            interval,
        }
    }

    /// What Shazam hears in the window centred on `t`; waits its turn first.
    pub fn sample(&self, t: f64) -> Result<Probe, NoAnswer> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(last) = state.1 {
            let since = last.elapsed();
            if since < self.interval {
                std::thread::sleep(self.interval - since);
            }
        }
        let start = sample_window_start(t);
        let heard = state.0.listen(start, SAMPLE_WINDOW);
        state.1 = Some(Instant::now());
        let heard = heard?;
        Ok(Probe {
            t: start,
            window: SAMPLE_WINDOW,
            purpose: MANUAL.into(),
            result: heard.result,
            offsets: heard.offsets,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shazam::Heard;
    use serde_json::json;
    use std::sync::Arc;

    struct Echo(Vec<(f64, f64)>, bool);

    impl Listener for Echo {
        fn listen(&mut self, t: f64, window: f64) -> Result<Heard, NoAnswer> {
            self.0.push((t, window));
            if self.1 {
                return Err(NoAnswer::Throttled);
            }
            Ok(Heard {
                result: Some(json!({"artist": "A", "title": "T"})),
                offsets: None,
            })
        }
    }

    #[test]
    fn a_sample_is_centred_on_the_moment_and_kept_as_manual() {
        let sampler = Sampler::new(Echo(Vec::new(), false), Duration::ZERO, None);
        let probe = sampler.sample(100.0).unwrap();
        assert_eq!(
            (probe.t, probe.window, probe.purpose.as_str()),
            (85.0, 30.0, MANUAL)
        );
        assert_eq!(probe.result.unwrap()["title"], "T");
        // Near the start the window starts at 0 rather than before the audio.
        assert_eq!(sampler.sample(4.0).unwrap().t, 0.0);
    }

    #[test]
    fn samples_are_spaced_apart_even_after_a_failure() {
        let gap = Duration::from_millis(60);
        let sampler = Arc::new(Sampler::new(Echo(Vec::new(), true), gap, None));
        let started = Instant::now();
        let others: Vec<_> = (0..2)
            .map(|i| {
                let s = sampler.clone();
                std::thread::spawn(move || s.sample(10.0 * f64::from(i)).is_err())
            })
            .collect();
        assert!(sampler.sample(50.0).is_err());
        assert!(others.into_iter().all(|t| t.join().unwrap()));
        // Three lookups, so at least two gaps, though all three failed.
        assert!(started.elapsed() >= gap * 2, "{:?}", started.elapsed());
    }

    #[test]
    fn a_sampler_made_after_a_run_waits_out_the_runs_last_call() {
        let gap = Duration::from_millis(60);
        let sampler = Sampler::new(Echo(Vec::new(), false), gap, Some(Instant::now()));
        let started = Instant::now();
        sampler.sample(1.0).unwrap();
        assert!(started.elapsed() >= gap * 9 / 10);
    }
}
