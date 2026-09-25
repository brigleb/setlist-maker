"""Uploaded track artwork and episode covers (uploads.py and where they are read)."""

import io
import json
import urllib.error
import urllib.request

import pytest
from PIL import Image

from setlist_maker import artwork_cache
from setlist_maker.uploads import (
    UploadError,
    artwork_dir_for,
    cover_path_for,
    is_upload_ref,
    read_upload,
    set_episode_cover,
    store_upload,
    upload_path,
)
from tests.test_web_editor import _ctx, running_server


def image_bytes(size=(800, 600), color=(200, 20, 21), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def decoded(data):
    return Image.open(io.BytesIO(data))


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Isolate the artwork cache; any network lookup is a test failure."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def no_network(*a, **k):
        raise AssertionError("an uploaded image must never trigger a lookup")

    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", no_network)


# --- the module ------------------------------------------------------------


def test_paths_sit_beside_the_tracklist(tmp_path):
    md = tmp_path / "2026-09-23-Keys-Lounge_tracklist.md"
    assert artwork_dir_for(md) == tmp_path / "2026-09-23-Keys-Lounge_artwork"
    assert cover_path_for(md) == tmp_path / "2026-09-23-Keys-Lounge_cover.jpg"


def test_store_normalizes_to_a_square_jpeg_named_by_its_content(tmp_path):
    ref = store_upload(tmp_path, image_bytes((800, 600)))
    assert is_upload_ref(ref)
    stored = upload_path(tmp_path, ref).read_bytes()
    img = decoded(stored)
    # Square, because create_chapter_image resizes rather than crops: a
    # landscape photo would otherwise be squashed into the chapter card.
    assert img.format == "JPEG" and img.size == (600, 600)
    # The same picture uploaded again is the same file, not a second copy.
    assert store_upload(tmp_path, image_bytes((800, 600))) == ref
    assert len(list(tmp_path.iterdir())) == 1


def test_store_caps_the_edge(tmp_path):
    ref = store_upload(tmp_path, image_bytes((3000, 3000), fmt="JPEG"))
    assert decoded(read_upload(tmp_path, ref)).size == (1400, 1400)


@pytest.mark.parametrize("junk", [b"not an image", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64])
def test_store_refuses_what_it_cannot_decode(tmp_path, junk):
    with pytest.raises(UploadError, match="HEIC"):
        store_upload(tmp_path, junk)
    assert not tmp_path.exists() or not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "ref",
    [
        "upload:../../etc/passwd",
        "upload:0123456789abcdef01234567.png",
        "upload:0123456789ABCDEF01234567.jpg",
        "https://example.test/a.jpg",
        "",
        None,
    ],
)
def test_malformed_references_resolve_to_nothing(tmp_path, ref):
    """The sidecar is hand-editable: a name is validated before it is a path."""
    assert not is_upload_ref(ref)
    assert upload_path(tmp_path, ref) is None
    assert read_upload(tmp_path, ref) is None


def test_episode_cover_is_set_and_removed(tmp_path):
    md = tmp_path / "set_tracklist.md"
    ref = store_upload(artwork_dir_for(md), image_bytes())
    set_episode_cover(md, ref)
    assert cover_path_for(md).read_bytes() == read_upload(artwork_dir_for(md), ref)
    set_episode_cover(md, None)
    assert not cover_path_for(md).exists()
    set_episode_cover(md, None)  # removing nothing is fine


def test_episode_cover_refuses_a_missing_upload(tmp_path):
    md = tmp_path / "set_tracklist.md"
    with pytest.raises(UploadError):
        set_episode_cover(md, "upload:" + "0" * 24 + ".jpg")


# --- the artwork cache -----------------------------------------------------


def test_an_upload_is_read_locally_and_composited(tmp_path, offline):
    ref = store_upload(tmp_path / "art", image_bytes(color=(10, 200, 30)))
    src = artwork_cache.source_artwork("A", "T", ref, uploads_dir=tmp_path / "art")
    assert src == read_upload(tmp_path / "art", ref)
    card = decoded(artwork_cache.chapter_image("A", "T", ref, uploads_dir=tmp_path / "art"))
    r, g, b = card.convert("RGB").getpixel((20, 20))
    assert g > 150 and r < 80  # the uploaded green, not the gradient fallback


def test_a_missing_upload_is_not_remembered_as_artless(tmp_path, offline):
    """An iCloud folder that has not synced yet is a delay, not an answer: no
    .fallback marker, and no cached gradient under the upload's key."""
    art = tmp_path / "art"
    stored = image_bytes(color=(10, 200, 30))
    ref = store_upload(tmp_path / "elsewhere", stored)

    assert artwork_cache.source_artwork("A", "T", ref, uploads_dir=art) is None
    artwork_cache.chapter_image("A", "T", ref, uploads_dir=art)
    cache = artwork_cache.cache_dir()
    assert not list(cache.glob("*.fallback")) if cache.exists() else True
    assert not list(cache.glob("*.jpg")) if cache.exists() else True

    # The file arrives: the very next render uses it.
    art.mkdir()
    upload_path(art, ref).write_bytes(read_upload(tmp_path / "elsewhere", ref))
    card = decoded(artwork_cache.chapter_image("A", "T", ref, uploads_dir=art))
    assert card.convert("RGB").getpixel((20, 20))[1] > 150


# --- the server ------------------------------------------------------------


def post(base, path, body, ctype):
    req = urllib.request.Request(base + path, method="POST", data=body)
    req.add_header("Content-Type", ctype)
    return urllib.request.urlopen(req)


def test_upload_endpoint_stores_and_serves(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    with running_server(ctx) as base:
        with post(base, "/api/upload", image_bytes(), "image/png") as r:
            ref = json.loads(r.read())["ref"]
        assert upload_path(artwork_dir_for(ctx.output_path), ref).exists()
        with urllib.request.urlopen(f"{base}/api/upload/{ref[len('upload:') :]}") as r:
            assert r.headers["Content-Type"] == "image/jpeg"
            assert "immutable" in r.headers["Cache-Control"]
            assert decoded(r.read()).size == (600, 600)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/upload/..%2f..%2fset_tracklist.md")
        assert exc.value.code == 404


@pytest.mark.parametrize(
    "ctype", ["multipart/form-data; boundary=x", "text/plain", "application/octet-stream"]
)
def test_upload_endpoint_refuses_types_a_cross_site_form_can_send(
    sample_tracklist, tmp_path, ctype
):
    """multipart/form-data and text/plain need no preflight; image/* does."""
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/upload", image_bytes(), ctype)
    assert exc.value.code == 415
    assert not artwork_dir_for(tmp_path / "set_tracklist.md").exists()


def test_upload_endpoint_refuses_junk_and_oversize(sample_tracklist, tmp_path, monkeypatch):
    monkeypatch.setattr("setlist_maker.web_editor.MAX_UPLOAD_BYTES", 1000)
    with running_server(_ctx(sample_tracklist, tmp_path)) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/upload", b"GIF89a but not really", "image/gif")
        assert exc.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/upload", image_bytes((900, 900), fmt="BMP"), "image/bmp")
        assert exc.value.code == 413


def save(base, payload):
    return post(base, "/api/save", json.dumps(payload).encode(), "application/json")


def _edits(tracklist, **by_index):
    return [
        {
            "index": i,
            "artist": t.artist,
            "title": t.title,
            "rejected": t.rejected,
            **by_index.get(str(i), {}),
        }
        for i, t in enumerate(tracklist.tracks)
    ]


def test_save_pins_an_uploaded_cover_for_a_track(sample_tracklist, tmp_path, offline):
    ctx = _ctx(sample_tracklist, tmp_path)
    ref = store_upload(artwork_dir_for(ctx.output_path), image_bytes(color=(10, 200, 30)))
    with running_server(ctx) as base:
        with save(base, {"tracks": _edits(sample_tracklist, **{"0": {"coverart_url": ref}})}):
            pass
        track = ctx.tracklist.tracks[0]
        assert track.coverart_url == ref and track.artwork_pinned
        sidecar = json.loads(ctx.output_path.with_suffix(".json").read_text())
        assert sidecar[0]["coverart_url"] == ref
        # ...and the chapter preview is built from it, with no lookup.
        with urllib.request.urlopen(f"{base}/api/artwork?index=0") as r:
            assert decoded(r.read()).convert("RGB").getpixel((20, 20))[1] > 150


def test_save_sets_and_removes_the_episode_cover(sample_tracklist, tmp_path):
    ctx = _ctx(sample_tracklist, tmp_path)
    ctx.tracklist.tracks[1].is_episode_cover = True
    ref = store_upload(artwork_dir_for(ctx.output_path), image_bytes())
    cover = cover_path_for(ctx.output_path)
    with running_server(ctx) as base:
        with urllib.request.urlopen(f"{base}/api/tracklist") as r:
            assert json.loads(r.read())["cover"] is None

        with save(base, {"tracks": [], "cover": ref}):
            pass
        assert cover.exists()
        # One episode cover: the upload replaces the starred track.
        assert not any(t.is_episode_cover for t in ctx.tracklist.tracks)
        with urllib.request.urlopen(f"{base}/api/tracklist") as r:
            assert json.loads(r.read())["cover"] is not None
        with urllib.request.urlopen(f"{base}/api/cover") as r:
            assert r.headers["Cache-Control"] == "no-store"

        with save(base, {"tracks": []}):  # absent: unchanged
            pass
        assert cover.exists()
        with save(base, {"tracks": [], "cover": None}):
            pass
        assert not cover.exists()


@pytest.mark.parametrize("cover", ["https://example.test/c.jpg", "upload:" + "0" * 24 + ".jpg"])
def test_save_with_a_bad_cover_saves_nothing(sample_tracklist, tmp_path, cover):
    ctx = _ctx(sample_tracklist, tmp_path)
    with running_server(ctx) as base:
        with pytest.raises(urllib.error.HTTPError):
            save(
                base,
                {
                    "tracks": _edits(sample_tracklist, **{"0": {"artist": "Changed"}}),
                    "cover": cover,
                },
            )
    assert ctx.tracklist.tracks[0].artist != "Changed"
    assert not ctx.output_path.exists()


# --- embedding -------------------------------------------------------------


def test_chapters_endpoint_embeds_the_saved_tracklist(sample_tracklist, tmp_path, monkeypatch):
    audio = tmp_path / "set.mp3"
    audio.write_bytes(b"\xff\xfb" + b"\x00" * 64)
    calls = []

    def fake_embed(tracklist, audio_path, fetch_art=True, cover_image=None, tracklist_path=None):
        calls.append((audio_path, tracklist_path))
        return 4, 3, True

    monkeypatch.setattr("setlist_maker.cli.embed_chapters_for_tracklist", fake_embed)
    ctx = _ctx(sample_tracklist, tmp_path, audio_path=audio)
    with running_server(ctx) as base:
        with urllib.request.urlopen(f"{base}/api/tracklist") as r:
            assert json.loads(r.read())["can_embed"] is True
        with post(base, "/api/chapters", b"{}", "application/json") as r:
            assert json.loads(r.read()) == {"ok": True, "chapters": 4, "images": 3, "cover": True}
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/chapters", b"{}", "text/plain")
        assert exc.value.code == 415
    # tracklist_path is what lets the embed find uploads and the saved cover.
    assert calls == [(audio, ctx.output_path)]


def test_chapters_endpoint_needs_an_mp3_and_no_live_run(sample_tracklist, tmp_path):
    from setlist_maker.web_editor import LiveRun

    wav = tmp_path / "set.wav"
    wav.write_bytes(b"RIFF")
    with running_server(_ctx(sample_tracklist, tmp_path, audio_path=wav)) as base:
        with urllib.request.urlopen(f"{base}/api/tracklist") as r:
            assert json.loads(r.read())["can_embed"] is False
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/chapters", b"{}", "application/json")
        assert exc.value.code == 400

    ctx = _ctx(sample_tracklist, tmp_path, audio_path=tmp_path / "set.mp3")
    ctx.live = LiveRun(source_file="set.mp3")
    with running_server(ctx) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/api/chapters", b"{}", "application/json")
        assert exc.value.code == 409


def _capture_embed(monkeypatch):
    embedded = {}
    monkeypatch.setattr(
        "setlist_maker.cli.embed_chapters", lambda **kw: embedded.update(kw) or kw["audio_path"]
    )
    return embedded


def test_embed_uses_the_saved_cover_and_uploaded_art(
    sample_tracklist, tmp_path, monkeypatch, offline
):
    from setlist_maker.cli import embed_chapters_for_tracklist

    md = tmp_path / "set_tracklist.md"
    ref = store_upload(artwork_dir_for(md), image_bytes(color=(10, 200, 30)))
    sample_tracklist.tracks[0].coverart_url = ref
    set_episode_cover(md, store_upload(artwork_dir_for(md), image_bytes(color=(20, 20, 220))))
    monkeypatch.setattr("setlist_maker.artwork_cache.fetch_artwork", lambda *a, **k: None)
    embedded = _capture_embed(monkeypatch)

    result = embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "set.mp3", fetch_art=True, tracklist_path=md
    )

    assert result == (4, 3, True)
    first = decoded(embedded["chapter_images"][0]).convert("RGB").getpixel((20, 20))
    assert first[1] > 150  # the uploaded green
    cover = decoded(embedded["episode_image"]).convert("RGB").getpixel((20, 20))
    assert cover[2] > 150 and cover[0] < 80  # the saved blue cover, no overlay


def test_embed_cover_precedence_and_no_artwork(sample_tracklist, tmp_path, monkeypatch, offline):
    """--cover outranks the saved cover; the saved cover, like --cover, is
    embedded even with --no-artwork."""
    from setlist_maker.cli import embed_chapters_for_tracklist

    md = tmp_path / "set_tracklist.md"
    set_episode_cover(md, store_upload(artwork_dir_for(md), image_bytes(color=(20, 20, 220))))
    embedded = _capture_embed(monkeypatch)

    embed_chapters_for_tracklist(
        sample_tracklist,
        tmp_path / "set.mp3",
        fetch_art=False,
        cover_image=b"CLI",
        tracklist_path=md,
    )
    assert embedded["episode_image"] == b"CLI"

    embed_chapters_for_tracklist(
        sample_tracklist, tmp_path / "set.mp3", fetch_art=False, tracklist_path=md
    )
    assert decoded(embedded["episode_image"]).convert("RGB").getpixel((20, 20))[2] > 150
