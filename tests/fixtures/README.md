# Boundary-engine fixtures

Verbatim slices of the progress file from one real run: a 4-hour DJ set
(`2026-08-26-Keys-Lounge.mp3`, 4:01:08, 317 probes), captured in the
`save_progress_v2` shape so `adaptive.load_probes()` — the reader the driver
itself uses — reads them. Only `shazam_url` and `coverart_url` are stripped, to
keep the files small; nothing the fold reads is touched.

`audio_duration` is the **whole recording's**, not the slice's, and the probes
keep their original fold order (which is not chronological). Both matter:
`segments()` measures the last run against `duration`, and the engine's state is
an order-dependent fold, so a re-sorted or re-timed fixture would not be the run
that happened.

| file | span | what it captures |
|---|---|---|
| `thrash_zone_probes.json` | 3:41–3:48 | Boz Scaggs *Lowdown* → Grant Green *Sookie Sookie (Live)*, with Shazam alternating Sookie/Us3 *Tukka Yoot's Riddim* across the cut. Us3 sample Blue Note records and Grant Green is Blue Note, so the confusion is semantic, not noise. Unfixed, this folds to four segments spanning 36s. |
| `sliver_zone_probes.json` | 1:05–1:08 | Roy Ayers *Pricilla's Theme* → Herb Alpert *Rise*, with a 16s Notorious B.I.G. *Hypnotize* wedged in the transition — Hypnotize samples *Rise*. Three distinct identities in a row, so it is **not** an alternation; both probes report 0.999 confidence. |

To re-cut them (or a new zone) from a fresh run's progress file, slice on probe
`t` and keep `version` / `audio_duration` as they were.
