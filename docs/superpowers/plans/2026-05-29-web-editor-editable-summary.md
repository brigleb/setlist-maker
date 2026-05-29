# Editable Set Description (Web Editor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web editor's set description prominent, column-aligned, and editable inline, persisting on Save and surviving a reopen.

**Architecture:** Front end swaps the read-only muted `.summary` div for an always-on, auto-growing `<textarea>` constrained to the 760px track-list column; the save payload gains a top-level `summary`. Back end threads that value through the existing pure `apply_edits()` helper (sentinel default + whitespace normalization) so all edits stay in one tested place; `save_tracklist()` and the markdown round-trip are unchanged.

**Tech Stack:** Python 3.10+, stdlib `http.server`, `pytest`; vanilla HTML/CSS/JS single-page asset.

**Spec:** `docs/superpowers/specs/2026-05-29-web-editor-editable-summary-design.md`

---

### Task 1: Back end — `apply_edits()` summary support (pure helper)

**Files:**
- Modify: `setlist_maker/web_editor.py` (imports; module-level sentinel + helper; `apply_edits` signature/body, currently lines 66-119)
- Test: `tests/test_web_editor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_editor.py` (after `test_apply_edits_new_track_missing_timestamp_defaults_to_zero`):

```python
def test_apply_edits_sets_summary(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="A deep house journey.")
    assert sample_tracklist.summary == "A deep house journey."


def test_apply_edits_clears_summary_when_blank(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.summary = "Original."
    apply_edits(sample_tracklist, [], None, summary="   ")
    assert sample_tracklist.summary is None


def test_apply_edits_leaves_summary_unchanged_when_omitted(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    sample_tracklist.summary = "Original summary."
    apply_edits(
        sample_tracklist,
        [{"index": 0, "artist": "Daft Punk", "title": "Around the World"}],
        None,
    )
    assert sample_tracklist.summary == "Original summary."


def test_apply_edits_normalizes_summary_whitespace(sample_tracklist):
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="Line one.\n\nLine two.   Extra")
    assert sample_tracklist.summary == "Line one. Line two. Extra"


def test_summary_round_trips_through_markdown(sample_tracklist):
    from setlist_maker.editor import parse_markdown_tracklist
    from setlist_maker.web_editor import apply_edits

    apply_edits(sample_tracklist, [], None, summary="A sweaty warehouse set.")
    reparsed = parse_markdown_tracklist(sample_tracklist.to_markdown())
    assert reparsed.summary == "A sweaty warehouse set."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_web_editor.py -k summary -v`
Expected: FAIL — `apply_edits()` takes no `summary` keyword (`TypeError: apply_edits() got an unexpected keyword argument 'summary'`).

- [ ] **Step 3: Implement the minimal change**

In `setlist_maker/web_editor.py`, add `import re` to the import block (alongside `import json`):

```python
import json
import re
```

Add a module-level sentinel + helper just above `def apply_edits` (after `tracklist_to_api`):

```python
_UNSET = object()  # "summary not provided" — distinct from an empty/cleared summary


def _normalize_summary(value: str | None) -> str | None:
    """Collapse whitespace runs to single spaces; empty -> None.

    Keeps the markdown round-trip lossless: parse_markdown_tracklist() joins
    contiguous prose lines with spaces, so a single-paragraph summary reloads
    byte-for-byte.
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    return text or None
```

Change the `apply_edits` signature to accept `summary` (defaulting to the sentinel):

```python
def apply_edits(
    tracklist: Tracklist,
    edits: list[dict],
    corrections_db: CorrectionsDB | None,
    summary: object = _UNSET,
) -> None:
```

At the very end of `apply_edits` (after the `if inserted:` block), set the summary only when provided:

```python
    if summary is not _UNSET:
        tracklist.summary = _normalize_summary(summary)
```

Update the docstring's first line to mention summary, e.g. append a sentence:
`"An optional ``summary`` (when not the _UNSET sentinel) replaces tracklist.summary, normalized to a single paragraph; blank clears it."`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_web_editor.py -k summary -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/web_editor.py tests/test_web_editor.py
git commit -m "feat(web-editor): apply_edits accepts and normalizes summary"
```

---

### Task 2: Back end — wire `summary` through `_handle_save()`

**Files:**
- Modify: `setlist_maker/web_editor.py` (`_handle_save`, currently lines 170-183)
- Test: `tests/test_web_editor.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_editor.py` (after `test_post_save_writes_files_and_records_correction`):

```python
def test_post_save_writes_summary_to_markdown(sample_tracklist, tmp_path):
    from setlist_maker.web_editor import EditorContext

    ctx = EditorContext(
        tracklist=sample_tracklist,
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=None,
    )
    payload = json.dumps(
        {
            "tracks": [
                {"index": i, "artist": t.artist, "title": t.title, "rejected": False}
                for i, t in enumerate(sample_tracklist.tracks)
            ],
            "summary": "A sweaty warehouse set.",
        }
    ).encode()

    with running_server(ctx) as base:
        req = urllib.request.Request(
            base + "/api/save",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            res = json.loads(r.read())

    assert res["ok"] is True
    md = (tmp_path / "set_tracklist.md").read_text()
    assert "A sweaty warehouse set." in md
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_editor.py::test_post_save_writes_summary_to_markdown -v`
Expected: FAIL — the save path ignores `summary`, so the assertion `"A sweaty warehouse set." in md` fails (`AssertionError`).

- [ ] **Step 3: Implement the minimal change**

In `setlist_maker/web_editor.py`, change the body of `_handle_save` so it reads `summary` from the payload and passes it to `apply_edits`. Replace:

```python
        try:
            edits = json.loads(raw).get("tracks", [])
            apply_edits(ctx.tracklist, edits, ctx.corrections_db)
            save_tracklist(ctx.tracklist, ctx.output_path, ctx.corrections_db)
```

with:

```python
        try:
            data = json.loads(raw)
            edits = data.get("tracks", [])
            apply_edits(
                ctx.tracklist,
                edits,
                ctx.corrections_db,
                summary=data.get("summary", _UNSET),
            )
            save_tracklist(ctx.tracklist, ctx.output_path, ctx.corrections_db)
```

(`data.get("summary", _UNSET)` yields the sentinel when the key is absent, so an
old client that omits `summary` leaves it untouched; an explicit `null`/`""`
clears it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_web_editor.py -v`
Expected: PASS (all web-editor tests, including the new one and the unchanged `test_post_save_writes_files_and_records_correction`).

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/web_editor.py tests/test_web_editor.py
git commit -m "feat(web-editor): persist set description from save payload"
```

---

### Task 3: Front end — always-on editable description textarea

**Files:**
- Modify: `setlist_maker/web_editor.html` (`.summary` CSS at line 18; the `<div class="summary" ...>` body element at line 61; `load()` at lines 96-102; top-level consts near line 85; save payload at lines 254-256)
- Test: `tests/test_web_editor.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_editor.py` (after `test_page_has_insert_affordance_and_time_field`):

```python
def test_page_has_editable_summary():
    """The description is an always-on textarea, has the empty-state placeholder,
    and is included in the save payload."""
    html = (files("setlist_maker") / "web_editor.html").read_text(encoding="utf-8")
    assert '<textarea id="summary"' in html
    assert "Add a description for this set" in html  # empty-state placeholder
    assert "summary:" in html  # included in the POST /api/save body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_editor.py::test_page_has_editable_summary -v`
Expected: FAIL — today's page uses `<div class="summary" id="summary" hidden>`, has no placeholder, and the save payload has no `summary:` key (`AssertionError`).

- [ ] **Step 3: Implement the page changes**

**(a) CSS** — replace the `.summary` rule (line 18) with:

```css
  .summary { display:block; width:100%; max-width:760px; margin:0 auto; padding:14px 20px; font:inherit; font-size:16px; line-height:1.5; color:#3a3a3c; background:var(--card); border:none; border-bottom:1px solid #f1f1f1; resize:none; overflow:hidden; }
  .summary:focus { outline:none; background:#eef6ff; box-shadow:inset 0 0 0 2px var(--accent); }
  .summary::placeholder { color:var(--muted); font-style:italic; }
```

**(b) Body element** — replace line 61:

```html
  <div class="summary" id="summary" hidden></div>
```

with:

```html
  <textarea id="summary" class="summary" rows="2" placeholder="Add a description for this set…"></textarea>
```

**(c) Top-level setup** — after `const audio = document.getElementById("audio");` (line 85), add:

```javascript
  const summaryEl = document.getElementById("summary");
  function autosizeSummary() { summaryEl.style.height = "auto"; summaryEl.style.height = summaryEl.scrollHeight + "px"; }
  summaryEl.addEventListener("input", () => { setDirty(true); autosizeSummary(); });
```

**(d) `load()`** — replace the summary line inside `load()` (line 100):

```javascript
    if (data.summary) { const s = document.getElementById("summary"); s.textContent = data.summary; s.hidden = false; }
```

with:

```javascript
    summaryEl.value = data.summary || "";
    autosizeSummary();
```

**(e) Save payload** — in the `save` click handler, change the payload object (line 254) from:

```javascript
    const payload = { tracks: tracks.map(t => t._new
```

to:

```javascript
    const payload = { summary: summaryEl.value, tracks: tracks.map(t => t._new
```

(The closing `) };` of the `.map(...)` stays as-is.)

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `pytest tests/test_web_editor.py -v && pytest -q`
Expected: PASS — the new `test_page_has_editable_summary` passes and no existing test regresses.

- [ ] **Step 5: Commit**

```bash
git add setlist_maker/web_editor.html tests/test_web_editor.py
git commit -m "feat(web-editor): prominent, column-aligned, editable description"
```

---

### Task 4: Lint + manual verification

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `ruff check . && ruff format --check .`
Expected: no errors. (If `ruff format --check` reports changes, run `ruff format .`, re-run tests, and amend the relevant commit.)

- [ ] **Step 2: Manual smoke test (real editor)**

Run the editor against a tracklist with a summary, e.g.:

```bash
setlist-maker identify <some_audio> --web-edit
```

or open it on an existing markdown via the normal `--web-edit` flow. Verify:
- The description shows larger/darker (not muted italic) and aligns to the same
  left edge as the track rows (760px column).
- Editing the text enables **Save**; after Save + browser refresh the new text is
  still there (round-trip), and it's present in the written `*_tracklist.md`.
- A set with **no** summary shows the empty textarea with the
  "Add a description for this set…" placeholder; typing + Save persists it.

- [ ] **Step 3: No commit** (verification only). If lint forced a reformat, ensure it was committed in Step 1.

---

## Acceptance criteria mapping (issue #14)

- *Description visibly larger / more prominent* → Task 3(a): 16px, normal dark color, not italic.
- *Aligns to the track-list column* → Task 3(a): `max-width:760px; margin:0 auto`, 20px padding matching rows.
- *Editable inline; Save writes to markdown and survives reopening* → Tasks 1–3: textarea → payload `summary` → `apply_edits` → `save_tracklist`; round-trip test (Task 1) + HTTP test (Task 2) + manual (Task 4).
- *Empty-state editable field when no summary* → Task 3(b): always-rendered textarea with placeholder.
