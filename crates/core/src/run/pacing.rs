//! Waiting between calls, and being told to stop.

use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// The run's clock and its stop button, so a test can run an hour of pacing in no time.
pub trait Pacing {
    /// Waits `d`, or less if asked to stop meanwhile; true when the run should stop.
    fn wait(&mut self, d: Duration) -> bool;
    fn stopped(&self) -> bool;
    /// Time since the run started, for the budget.
    fn elapsed(&self) -> Duration;
}

/// Shared between the run and whatever asks it to stop (Ctrl-C, Finish now).
#[derive(Default)]
pub struct StopSignal {
    stop: Mutex<bool>,
    changed: Condvar,
}

impl StopSignal {
    pub fn new() -> Arc<StopSignal> {
        Arc::new(StopSignal::default())
    }

    /// Finish after the sample in flight; a wait in progress ends now.
    pub fn request(&self) {
        *self.stop.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.changed.notify_all();
    }

    pub fn requested(&self) -> bool {
        *self.stop.lock().unwrap_or_else(|e| e.into_inner())
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
        let guard = self.stop.stop.lock().unwrap_or_else(|e| e.into_inner());
        let (guard, _) = self
            .stop
            .changed
            .wait_timeout_while(guard, d, |stop| !*stop)
            .unwrap_or_else(|e| e.into_inner());
        *guard
    }

    fn stopped(&self) -> bool {
        self.stop.requested()
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
}
