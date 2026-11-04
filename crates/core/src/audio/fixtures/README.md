# Audio fixtures

Made once with ffmpeg 8.1, so `cargo test` needs no encoder:

- `stereo48_cbr.mp3`: 12s, 48 kHz stereo, CBR 96k, **no Xing frame**, like Ray's Logic
  exports. The channels differ, so taking the first one is tested.
- `mono44_info.mp3`: 10s, 44.1 kHz mono, 64k, with ffmpeg's default Info frame (a stated
  length).
- `tone8k.flac`: 10s of a 330 Hz sine, 8 kHz mono 16-bit.

```sh
ffmpeg -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=12" \
  -f lavfi -i "anoisesrc=color=pink:sample_rate=48000:duration=12:amplitude=0.3:seed=7" \
  -filter_complex "[0][1]amerge=inputs=2,pan=stereo|c0=c0+0.5*c1|c1=0.4*c0-c1,volume=0.8" \
  -c:a libmp3lame -b:a 96k -write_xing 0 -id3v2_version 3 stereo48_cbr.mp3
ffmpeg -f lavfi -i "anoisesrc=color=brown:sample_rate=44100:duration=10:amplitude=0.5:seed=3" \
  -ac 1 -c:a libmp3lame -b:a 64k mono44_info.mp3
ffmpeg -f lavfi -i "sine=frequency=330:sample_rate=8000:duration=10" \
  -ac 1 -sample_fmt s16 -c:a flac tone8k.flac
```

`../rodio_*_to_16000.i16` are rodio 0.20.1's resampling of the test's input, from
`spikes/shazam/tests/dump_rodio.rs`.
