# Web editor: preview the real chapter artwork before embedding

**Issue:** #20 (partial — preview only; curation deferred)
**Date:** 2026-08-12

## Problem

Chapter images are generated at embed time and first seen in the podcast app.
Artwork mistakes — wrong release art, a composite whose overlay text is broken —
are discovered after publishing, or never.

The web editor already shows a 42px thumbnail per row (`web_editor.html:134`),
but it renders `coverart_url`: the raw Shazam cover, **not** the
`create_chapter_image()` composite that actually gets embedded. When Shazam
supplied no URL the thumb is a bare gradient, which is precisely the case where
the artwork waterfall does its most guessing.

## Goal

The row thumbnail shows the real composite, clickable to full size, and the bytes
you approved are the bytes that get embedded.

## Decisions (settled during brainstorming)

- **Scope: preview only.** No alternates picker, no paste-a-URL, no explicit
  episode-cover choice. Those stay open on #20 (see *Deferred*).
- **Authoritative disk cache.** `embed_chapters_for_tracklist()` reuses the
  cached composite rather than refetching, so what was approved is what ships.
  This also makes `--chapters` near-instant after an editing session.
- **Placement: swap the existing row thumb**, click for a full-size overlay. No
  new tab or gallery view.
- **Cache location: `~/.cache/setlist-maker/artwork/`**, keyed by a content hash.
  Nothing clutters the user's music folder, and a track appearing in two sets
  composites once.

### A premise from the issue that does not hold

The issue says alternates are cheap because "iTunes / Deezer / MusicBrainz all
get queried anyway." They are not. `fetch_artwork()` (`artwork.py:263`) is a
short-circuiting waterfall that returns on the first source yielding bytes; when
Shazam supplied a `coverart_url`, the other three are never called. Offering
alternates means *new* fan-out (~3 extra queries per track), not salvage of work
already done. This is why curation was descoped rather than bundled in.

## Architecture

One function, two callers, cache in the middle.

Today `embed_chapters_for_tracklist()` (`cli.py:292-308`) inlines *fetch →
composite*. That inline pair becomes a single cached function called by both the
editor endpoint and the chapters path, so only one code path can produce a
chapter image. That is what makes the preview authoritative rather than merely
similar.

```
web page  ──GET /api/artwork?index=N──►  web_editor._handle_artwork
                                                  │
cli.embed_chapters_for_tracklist ─────────────────┤
                                                  ▼
                                    artwork_cache.chapter_image()
                                         │hit          │miss
                                    read cache    fetch_artwork()
                                                  create_chapter_image()
                                                  atomic write
```

## New module: `setlist_maker/artwork_cache.py`

A separate module rather than more surface on `artwork.py`, which is already 450
lines and owns two distinct jobs (network fetching, image drawing). Caching is a
third.

```python
def cache_dir() -> Path
    # $XDG_CACHE_HOME/setlist-maker/artwork, else ~/.cache/setlist-maker/artwork

def cache_key(artist: str, title: str, coverart_url: str | None, size: int) -> str
    # sha256 hex of a canonical joined string of all four inputs

def chapter_image(artist, title, coverart_url=None, size=CHAPTER_IMAGE_SIZE) -> bytes
    # hit  -> read the cached JPEG
    # miss -> fetch_artwork() + create_chapter_image() -> atomic write -> return
```

**Invalidation is structural, not logic.** Because the key is a content hash of
everything that affects the output, editing an artist or title yields a different
key and regenerates automatically. There is no invalidation code path, and
therefore no stale-cache bug class. `size` is in the key so a future
`CHAPTER_IMAGE_SIZE` change invalidates rather than serving mis-sized art.

**Concurrency.** A per-key lock held across the generate-and-write section, so
two simultaneous requests for the same track don't both hit the network. Writes
go to a temp file in the cache dir and are `os.replace`d into position, so a
crash or a concurrent reader never sees a half-written JPEG.

## Endpoint: `GET /api/artwork?index=N`

Serves `image/jpeg` from `ctx.tracklist.tracks[N]`. Guarded by a module-level
semaphore capping concurrent generation at 4, so a fast scroll through 60 rows
cannot open 60 threads each doing up to 6 network calls. Cache hits are served
without taking the semaphore — only generation is capped.

**Index-based, deliberately — not artist/title passed from the page.** Since the
cache is authoritative, the preview must reflect *saved server state*, because
that is what `chapters` will embed. Letting the page send unsaved edits would
render something that isn't what ships, defeating the feature's purpose.

Responses:

| Case | Response |
|---|---|
| Identified track | `200` `image/jpeg` |
| Index out of range / unparseable | `404` |
| Unidentified track (`is_unidentified`) | `404` — chapters skips these too |

Rejected tracks are still served: rejection is one click away from being undone,
and generating is cheap once cached.

## Front-end: `web_editor.html`

- The thumb's background becomes `/api/artwork?index=N`, requested lazily via
  `IntersectionObserver` so visible rows generate first (as the issue asks).
  Until it lands, the existing gradient placeholder stays — no layout shift.
- A small client-side concurrency cap (4 in flight) so scrolling doesn't queue
  dozens of requests ahead of what the user is actually looking at.
- Click the thumb → a full-size overlay of the same image. Escape or a click
  outside dismisses it. The overlay is where a broken composite is actually
  visible; 42px can only tell you whether the album is right.
- A 404 leaves the placeholder in place (unidentified rows keep their `?`).
- After a successful save, thumbs re-request with a cache-busting query param so
  edited rows re-render against the new server state.

Consistent with the page's existing no-`innerHTML` posture: image URLs are set
via `style.backgroundImage` / `img.src` with an integer index, never by
interpolating track text into markup.

## Chapters path: `setlist_maker/cli.py`

`embed_chapters_for_tracklist()` replaces its inline fetch-and-composite with a
call to `artwork_cache.chapter_image()`. The episode cover is generated the same
way — it passes different text (source filename, `"Tracklist"`), which is simply
a different cache key, so it needs no special handling.

Net effect: running `--chapters` after an editing session is near-instant and
byte-identical to what was previewed.

## Error handling

- **No artwork found anywhere** is not an error. `create_chapter_image(None, …)`
  already returns the gradient fallback, which is exactly what ships, so it is
  cached and served like any other image.
- **Corrupt or truncated cache file** → treated as a miss and regenerated.
- **Cache directory unwritable** (read-only home, full disk) → generate in
  memory, serve normally, skip the write. Preview must not fail because a cache
  can't be persisted.
- **Network failure** is already absorbed by `fetch_artwork()`, which returns
  `None` and falls through to the gradient composite.

## Testing

`tests/test_artwork_cache.py` (new)
- key changes when any of artist / title / coverart_url / size changes
- key is stable across calls for identical inputs
- a hit does not touch the network (patch `fetch_artwork` to raise)
- a miss writes a file whose bytes equal the returned bytes
- a corrupt cached file is regenerated rather than served
- an unwritable cache dir still returns valid bytes

`tests/test_web_editor.py` (extend)
- `/api/artwork?index=0` returns `200` with `Content-Type: image/jpeg`
- out-of-range and non-integer index return `404`
- an unidentified track returns `404`
- two concurrent requests for the same index generate once

`tests/test_cli.py` (extend)
- `embed_chapters_for_tracklist` with a pre-seeded cache never calls
  `fetch_artwork`

All 238 existing tests must stay green.

## Known limitation

`fetch_artwork()` is synchronous `urllib` with 15-second timeouts across up to
six strategies, so a track with no art anywhere can block ~90 seconds before
returning the fallback composite. Cached, this is a one-time cost per track;
uncached, that row shows its placeholder for a while. Shipping as-is; a shorter
per-request budget for the preview path is the fix if it proves annoying in
practice.

## Deferred (keeps #20 open)

- Alternates picker across iTunes / Deezer / MusicBrainz, and paste-a-URL
- Explicit choice of which track's art becomes the episode cover (today: silently
  the first track with artwork). This needs a tracklist-level field, and
  `Tracklist.to_json()` currently returns a bare list with nowhere to put one.
