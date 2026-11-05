//! Moments the user asked about during a live run, answered by that run.
//!
//! While a run is identifying it is the only thing that calls Shazam: two callers would double
//! the burst the limit is sensitive to, and two writers would race on the progress file. So a
//! click on the live timeline is queued here, the run takes it ahead of its own next probe
//! (inside its normal pacing), saves the answer, and only then hands it to the asker, who is
//! waiting on another thread (an HTTP request). Python's `sampler.SampleRequests`.

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crate::model::Probe;

/// How long a sample's window is when the user picks the moment.
pub const SAMPLE_WINDOW: f64 = 30.0;

/// Where a window starts so that the 10 seconds Shazam fingerprints, the window's middle, are
/// centred on `t`: the answer describes the moment clicked, not the fifteen seconds after it.
pub fn sample_window_start(t: f64) -> f64 {
    (t - SAMPLE_WINDOW / 2.0).max(0.0)
}

/// One moment asked about, and its answer once there is one.
pub struct Question {
    t: f64,
    answer: Mutex<Option<Result<Probe, String>>>,
    answered: Condvar,
}

impl Question {
    /// The moment asked about, in seconds.
    pub fn t(&self) -> f64 {
        self.t
    }

    /// The probe the run recorded for it, or why there is none. The first answer stands.
    pub fn answer(&self, result: Result<Probe, String>) {
        let mut slot = self.answer.lock().unwrap_or_else(|e| e.into_inner());
        if slot.is_none() {
            *slot = Some(result);
            self.answered.notify_all();
        }
    }

    /// Waits up to `timeout` for the answer.
    pub fn wait(&self, timeout: Duration) -> Result<Probe, String> {
        let slot = self.answer.lock().unwrap_or_else(|e| e.into_inner());
        let (slot, _) = self
            .answered
            .wait_timeout_while(slot, timeout, |a| a.is_none())
            .unwrap_or_else(|e| e.into_inner());
        slot.clone().unwrap_or_else(|| {
            Err(format!(
                "no answer for {:.0}s within {}s",
                self.t,
                timeout.as_secs()
            ))
        })
    }

    fn answered(&self) -> bool {
        self.answer
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_some()
    }
}

#[derive(Default)]
struct State {
    waiting: VecDeque<Arc<Question>>,
    /// Taken by the run and not answered yet, so closing can fail them too.
    taken: Vec<Arc<Question>>,
    closed: Option<String>,
}

/// The queue between the askers and the run.
#[derive(Default)]
pub struct Questions {
    state: Mutex<State>,
}

impl Questions {
    pub fn new() -> Arc<Questions> {
        Arc::new(Questions::default())
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Queues `t` without waiting. Once the queue is closed, the question is failed at once.
    pub fn submit(&self, t: f64) -> Arc<Question> {
        let question = Arc::new(Question {
            t,
            answer: Mutex::new(None),
            answered: Condvar::new(),
        });
        let mut state = self.state();
        match &state.closed {
            Some(why) => question.answer(Err(why.clone())),
            None => state.waiting.push_back(question.clone()),
        }
        question
    }

    /// Queues `t` and waits up to `timeout` for the run to answer it. A question not taken in
    /// time leaves the queue, so the run never spends a call on an asker who has gone.
    pub fn ask(&self, t: f64, timeout: Duration) -> Result<Probe, String> {
        let question = self.submit(t);
        let result = question.wait(timeout);
        if result.is_err() {
            self.state().waiting.retain(|q| !Arc::ptr_eq(q, &question));
        }
        result
    }

    /// The oldest waiting question, for the run.
    pub fn pop(&self) -> Option<Arc<Question>> {
        let mut state = self.state();
        state.taken.retain(|q| !q.answered());
        let question = state.waiting.pop_front()?;
        state.taken.push(question.clone());
        Some(question)
    }

    pub fn pending(&self) -> bool {
        !self.state().waiting.is_empty()
    }

    /// Refuses new questions and fails every unanswered one: nothing will serve them.
    pub fn close(&self, reason: &str) {
        let mut guard = self.state();
        let state = &mut *guard;
        state.closed.get_or_insert_with(|| reason.to_string());
        for question in state.waiting.drain(..).chain(state.taken.drain(..)) {
            question.answer(Err(reason.to_string()));
        }
    }
}

/// Closes the queue when the run leaves, however it leaves.
pub(super) struct CloseOnDrop(pub Option<Arc<Questions>>);

impl Drop for CloseOnDrop {
    fn drop(&mut self) {
        if let Some(questions) = &self.0 {
            questions.close("identification has finished");
        }
    }
}
