//! Waiting between calls, and being told to stop.

use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// The run's clock and its stop button, so a test can run an hour of pacing in no time.
pub trait Pacing {
    /// Waits `d`, or less if asked to stop meanwhile; true when the run should stop.
    fn wait(&mut self, d: Duration) -> bool;
    fn stopped(&self) -> bool;
    /// Waits until told to resume (Shazam stopped throttling, as far as the user can tell) or
    /// to stop; true when the run should stop.
    fn pause(&mut self) -> bool;
    /// Time since the run started, for the budget.
    fn elapsed(&self) -> Duration;
}

#[derive(Default)]
struct Flags {
    stop: bool,
    resume: bool,
}

/// Shared between the run and whatever asks it to stop (Ctrl-C, Finish now) or to carry on
/// after a pause (Resume).
#[derive(Default)]
pub struct StopSignal {
    flags: Mutex<Flags>,
    changed: Condvar,
}

impl StopSignal {
    pub fn new() -> Arc<StopSignal> {
        Arc::new(StopSignal::default())
    }

    fn flags(&self) -> std::sync::MutexGuard<'_, Flags> {
        self.flags.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Finish after the sample in flight; a wait or a pause in progress ends now.
    pub fn request(&self) {
        self.flags().stop = true;
        self.changed.notify_all();
    }

    pub fn requested(&self) -> bool {
        self.flags().stop
    }

    /// Ends a pause.
    pub fn resume(&self) {
        self.flags().resume = true;
        self.changed.notify_all();
    }

    /// Waits `d`, or until a stop is requested; whether one was.
    pub fn wait_for(&self, d: Duration) -> bool {
        let (flags, _) = self
            .changed
            .wait_timeout_while(self.flags(), d, |f| !f.stop)
            .unwrap_or_else(|e| e.into_inner());
        flags.stop
    }

    /// Waits for a resume or a stop; whether it was a stop.
    pub fn wait_for_resume(&self) -> bool {
        let mut flags = self
            .changed
            .wait_while(self.flags(), |f| !f.stop && !f.resume)
            .unwrap_or_else(|e| e.into_inner());
        flags.resume = false;
        flags.stop
    }
}

/// Real time.
pub struct Clock {
    started: Instant,
    stop: Arc<StopSignal>,
}

impl Clock {
    pub fn new(stop: Arc<StopSignal>) -> Clock {
        Clock {
            started: Instant::now(),
            stop,
        }
    }
}

impl Pacing for Clock {
    fn wait(&mut self, d: Duration) -> bool {
        self.stop.wait_for(d)
    }

    fn stopped(&self) -> bool {
        self.stop.requested()
    }

    fn pause(&mut self) -> bool {
        self.stop.wait_for_resume()
    }

    fn elapsed(&self) -> Duration {
        self.started.elapsed()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_stop_ends_a_wait_early() {
        let stop = StopSignal::new();
        let mut clock = Clock::new(stop.clone());
        assert!(!clock.wait(Duration::from_millis(5)));
        let other = stop.clone();
        let t = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            other.request();
        });
        let before = Instant::now();
        assert!(clock.wait(Duration::from_secs(30)));
        assert!(before.elapsed() < Duration::from_secs(5));
        assert!(clock.stopped());
        t.join().unwrap();
    }

    #[test]
    fn a_pause_lasts_until_resumed_or_stopped() {
        let stop = StopSignal::new();
        let mut clock = Clock::new(stop.clone());
        let other = stop.clone();
        let t = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            other.resume();
        });
        assert!(!clock.pause(), "resumed, not stopped");
        t.join().unwrap();
        stop.request();
        assert!(clock.pause());
    }
}
