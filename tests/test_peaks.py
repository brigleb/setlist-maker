"""Waveform peaks for the web editor (#57): setlist_maker/peaks.py and /api/peaks."""

import os
import threading
import urllib.error
import urllib.request

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

from setlist_maker import peaks


@pytest.fixture
def clip(tmp_path):
    """Ten seconds: five of silence, then five of a loud tone."""
    audio = AudioSegment.silent(duration=5000) + Sine(440).to_audio_segment(
        duration=5000, volume=-3
    )
    path = tmp_path / "set.wav"
    audio.export(path, format="wav")
    return path


@pytest.fixture(autouse=True)
def cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def decode(data: bytes):
    magic, version, rate, count, duration = peaks.HEADER.unpack_from(data)
    body = data[peaks.HEADER.size :]
    signed = [b - 256 if b > 127 else b for b in body]
    pairs = list(zip(signed[0::2], signed[1::2]))
    return magic, rate, count, duration, pairs


def test_peaks_cover_the_clip_at_the_peak_rate(clip):
    magic, rate, count, duration, pairs = decode(peaks.compute_peaks(clip, 10.0, segments=3))
    assert (magic, rate, count, duration) == (b"SMPK", peaks.PEAK_RATE, 500, 10.0)
    assert len(pairs) == 500
    assert all(lo == hi == 0 for lo, hi in pairs[10:240])  # the silence
    assert all(85 <= hi <= 95 and -95 <= lo <= -85 for lo, hi in pairs[260:500])  # -3 dBFS


def test_parallel_stretches_line_up_with_a_single_pass(clip):
    *_, split = decode(peaks.compute_peaks(clip, 10.0, segments=4))
    *_, whole = decode(peaks.compute_peaks(clip, 10.0, segments=1))
    assert len(split) == len(whole)
    off = [
        i
        for i, (a, b) in enumerate(zip(split, whole))
        if abs(a[0] - b[0]) > 2 or abs(a[1] - b[1]) > 2
    ]
    # Only the bins right at a stretch's seam may differ, and only slightly.
    assert len(off) <= 4


def test_peaks_are_cached_until_the_file_changes(clip, monkeypatch):
    calls = []
    real = peaks._decode
    monkeypatch.setattr(peaks, "_decode", lambda *a: calls.append(a) or real(*a))
    first = peaks.peaks_for(clip, 10.0)
    n = len(calls)
    assert n and peaks.peaks_for(clip, 10.0) == first
    assert len(calls) == n  # served from the cache
    st = clip.stat()
    os.utime(clip, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    peaks.peaks_for(clip, 10.0)
    assert len(calls) > n  # an edited file misses once


def _serve(tmp_path, audio_path):
    from setlist_maker.editor import Track, Tracklist
    from setlist_maker.web_editor import EditorContext, create_server

    ctx = EditorContext(
        tracklist=Tracklist(
            source_file="set.wav", tracks=[Track(timestamp=0, artist="A", title="a")]
        ),
        output_path=tmp_path / "set_tracklist.md",
        corrections_db=None,
        audio_path=audio_path,
    )
    return create_server(ctx)


@pytest.mark.parametrize("with_audio", [True, False])
def test_the_peaks_endpoint(clip, tmp_path, with_audio):
    httpd = _serve(tmp_path, clip if with_audio else None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/peaks"
        if not with_audio:
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url)
            assert err.value.code == 404
            return
        with urllib.request.urlopen(url) as r:
            assert r.headers["Content-Type"] == "application/octet-stream"
            magic, rate, count, _duration, _pairs = decode(r.read())
        assert (magic, rate) == (b"SMPK", peaks.PEAK_RATE) and count >= 499
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_page_has_the_zoomable_timeline_markup():
    from setlist_maker.web_editor import _load_page

    page = _load_page()
    for needle in (
        'id="tl-mini"',
        'id="tl-view"',
        'id="tl-wave"',
        'id="tl-blocks"',
        'id="seek-wave"',
    ):
        assert needle in page
