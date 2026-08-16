# Artwork curation — design

Closes the second half of issue #20. The **preview** half shipped in `969a69a`: row thumbnails
render the real `create_chapter_image()` composite, disk-cached, so the bytes approved in the
editor are the bytes embedded. What was left open was **curation** — fixing a wrong pick, and
choosing the episode cover.

## What the user gets

Clicking a row's thumbnail already opened an enlarger. It becomes the artwork panel:

- **Choose artwork…** — a grid of covers from iTunes, Deezer and the Cover Art Archive, labelled
  by source, plus a paste-a-URL box and an **Automatic** reset. Click one to use it.
- **☆ Episode cover** — star the track whose art becomes the set's cover. One per set; the
  starred row shows a ★ in the list.

Nothing is written until **Save**.

## Decisions, and what they were weighed against

### The sidecar stays a bare list

The issue's own status note says the episode cover "needs a tracklist-level field, and
`Tracklist.to_json()` returns a bare list with nowhere to put one". We did *not* add an envelope.

Changing the top level from list to object fails **silently** on any un-updated reader.
`_load_tracklist_with_artwork_urls()` iterates the loaded JSON; iterating a dict yields its keys
as strings, and `"timestamp" in "tracks"` is a false substring test rather than a `TypeError` —
so even the loader's existing `except (json.JSONDecodeError, IOError, TypeError)` guard never
fires. The result is not a crash but every track quietly losing its `coverart_url`, re-keying the
artwork cache, re-fetching, and possibly compositing art the user never approved. That is exactly
the preview/embed divergence the shipped half of #20 was built to eliminate.

So the choice is stored as a flag on the one track whose art is used. It rides the existing
per-track, timestamp-joined mechanism that `coverart_url` already uses, and no reader changes
shape. It also sidesteps a second trap: `to_json()` drops rejected tracks, so any pointer stored
as an *index* would drift the moment a row was rejected.

### A pinned cover is not a Shazam URL

`apply_track_edit()` clears `coverart_url` on any real correction (#30) because that URL is
evidence attached to the *original* identification — left attached, the chapter image keeps
showing the misidentified track's cover under the corrected text.

A cover the user picked is the opposite: their answer about *this* track. Storing both in one
field means the #30 clearing silently destroys a deliberate choice the first time someone fixes a
typo. `Track.artwork_pinned` distinguishes them, and `apply_track_edit()` skips the clearing only
when it is set. Within a single save the ordering carries the same weight: the pick is applied
*after* `apply_track_edit()`, so picking art and fixing a title in one save keeps both.

Alternatives rejected: a separate `pinned_url` field (the artwork cache keys on `coverart_url`
and has no invalidation path, so a pick stored anywhere else would leave the old key resolving to
the old composite); and dropping the #30 clearing outright (regresses #30).

### Candidates are enumerated, not salvaged

The issue assumed "iTunes / Deezer / MusicBrainz all get queried anyway". They are not —
`fetch_artwork()` short-circuits on the first source that answers. Alternates are therefore new
fan-out, which is why the grid is behind a button and per-track rather than eager: it only runs
for a track the user opened. `artwork_options()` caches the list on disk, so reopening is free.

Rather than duplicating each source, the single-result helpers became thin wrappers over new
`*_artwork_candidates` siblings called with `limit=1`, so the picker and the waterfall cannot
drift. Only MusicBrainz costs more per candidate: the Cover Art Archive reveals whether a release
has a front cover only by being asked, one request each.

An empty candidate list is deliberately **not** cached, unlike `source_artwork()`'s `.fallback`
marker. "No alternates" far more often means the network was down than that the track has none,
and this is an interactive surface where retrying costs one click.

### Deezer was contributing nothing

Verified live while building the picker: Deezer's advanced `artist:"..." track:"..."` syntax
returns **0 rows** for both *Daft Punk / One More Time* and *Kraftwerk / Autobahn*, while the
plain term query returns 48 and 164. Deezer had effectively been absent from the waterfall for a
large share of real tracks, not just from the picker. A plain-term retry runs when the advanced
query comes back empty.

This does change the unattended waterfall, so: already-cached composites are untouched (the cache
key does not change, so they are hits), and only fresh lookups can newly resolve to Deezer.
Tracks already carrying a sticky `.fallback` marker keep it until the cache is cleared.

### Tiles load from the source; the composite does not

Candidate tiles are `<img src>` straight to the source URL — the same thing the player bar's
thumbnail already does. Compositing each candidate server-side would show the identical
lower-third on every tile and add a download per candidate, buying nothing for the comparison the
grid exists to support. The WYSIWYG guarantee is kept where it matters: after saving, the row
thumbnail and the enlarger show the real composite, and it is byte-identical to what `chapters`
embeds (verified end-to-end: same SHA-256).

A **pasted** URL is loaded into an `Image` before it is accepted. `fetch_artwork()` falls
*through* its waterfall on a URL that will not download, so a dead link would not raise — it
would quietly composite some other source's art under this track's name.

### A typed URL is a fetch primitive

`download_image()` handed any string to `urlopen`, whose default opener serves `file:`, `ftp:`
and `data:`. Cover URLs used to come only from Shazam and the search APIs; now a user can type
one into a page, and it is persisted to the sidecar and re-fetched by this process on every later
run. `is_fetchable_url()` restricts it to `http(s)` with a host, enforced at two layers: in
`download_image()` (covers every caller, including a hand-edited sidecar) and in `apply_edits()`
before anything is mutated (so the user gets an error instead of a mystery gradient, and a bad
row cannot half-apply a save).

### The panel became interactive

Consequences, all of which are real bugs if missed:

- Only a click whose `e.target` *is* the backdrop dismisses it.
- The global keydown handler returns early while it is open. Its tiles are `BUTTON`s and the
  existing focus guard exempts only inputs, so `←/→` would seek the audio while browsing covers.
- `#toast` needs `z-index:60`; the overlay is `50` and the toast had none, so "Saved" would have
  rendered behind the backdrop.
- `[hidden] { display:none !important; }` — the panel's author `display:flex` beats the UA
  stylesheet's `[hidden]` rule.
- The panel lives outside `#list` (which `render()` rebuilds wholesale) and holds the open track
  as an **object reference**, for the same reason `playingTrack` exists (#37).

## Test network guard

This change adds network fan-out, and nothing structurally stopped a test from reaching iTunes
for real — one that forgot to patch simply passed, slowly and at a rate limiter's mercy. An
autouse fixture now fails any test resolving a non-loopback host. It raises a **`BaseException`**
subclass on purpose: every source helper wraps its request in `except Exception`, so an ordinary
error would be swallowed and the leak would stay silent. All 316 pre-existing tests pass under it
unchanged.

## Verification

- 374 tests (58 new), `ruff check` / `ruff format` clean.
- Driven in Chrome, light and dark: grid renders 8 distinct covers across three sources; the
  in-use tile is marked; a pick updates the panel, the row thumb and the dirty state; the star
  toggles and persists; all five player shortcuts are suppressed while the panel is open and fire
  again once closed; the three paste-URL paths (bad scheme, non-image, real image) behave.
- End to end: a browser save round-tripped through the sidecar into a separate `chapters`
  process, which embedded the chosen Deezer cover for track 1 (byte-identical to the editor's
  preview) and the starred track's art as the episode cover.
