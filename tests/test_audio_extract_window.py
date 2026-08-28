"""extract_window: arbitrary-position slicing for adaptive probes."""

from pydub import AudioSegment

from setlist_maker.audio import extract_window


def test_extracts_requested_window():
    audio = AudioSegment.silent(duration=60_000)  # 60s
    seg = extract_window(audio, 10.0, 12.0)
    assert len(seg) == 12_000


def test_clamps_at_end_of_audio():
    audio = AudioSegment.silent(duration=60_000)
    seg = extract_window(audio, 55.0, 12.0)
    assert len(seg) == 5_000


def test_clamps_negative_start():
    audio = AudioSegment.silent(duration=60_000)
    seg = extract_window(audio, -3.0, 12.0)
    assert len(seg) == 12_000
