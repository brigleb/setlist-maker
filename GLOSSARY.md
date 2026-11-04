# Hindsight

Hindsight (formerly Setlist Maker) turns a long DJ set recording into a tracklist by asking Shazam about short stretches of it,
then lets the DJ correct the result and publish it as a podcast episode with chapters.

## The set and what it produces

**Set**:
One DJ set's audio file together with the files that sit beside it (tracklist, sidecar,
progress, episode details, artwork).
_Avoid_: Recording, mix, session

**Tracklist**:
The ordered list of tracks in a set, each with its start time. It alone decides which tracks
exist.
_Avoid_: Setlist, playlist

**Track**:
One song in a tracklist: a start time plus an artist and a title, either of which may be unknown.

**Sidecar**:
The per-track details kept beside the tracklist (chosen artwork, episode cover flag), matched
to tracks by start time.

**Episode**:
The set as published: its title, artist and cover, carried in the audio file's tags.
_Avoid_: Podcast, show

**Episode cover**:
The one image that represents the whole episode, either uploaded for the set or taken from one
track's artwork.

**Chapter**:
A track's start time and name (and artwork) written into the episode so podcast players can
jump between tracks.
_Avoid_: Marker, cue

**Chosen artwork**:
A cover the DJ picked for a track by hand. A later correction to the track's name keeps it.
_Avoid_: Pinned artwork

## Identifying

**Run**:
One pass of identifying a set from start to finish (or until it is stopped), which can be
resumed later.
_Avoid_: Scan, job

**Live run**:
A run being watched as it happens. The set can't be edited until it finishes.

**Sample**:
One question put to Shazam about one window of the set, and the answer it gave.
_Avoid_: Probe (the engine's internal word), lookup, query

**Window**:
The stretch of audio sent with a sample.

**Excerpt**:
The 10 seconds at the centre of a window: the audio Shazam actually matches.

**Boundary**:
The moment one track gives way to the next.

**Sample marker**:
A sample drawn on the timeline at the moment it heard.
_Avoid_: Pin

**Correction**:
A change the DJ made to a track's artist or title. It is remembered across sets and applied to
later runs.

## Editing

**Merge**:
Folding a track into the one before or after it, so its time belongs to its neighbour.
_Avoid_: Delete, reject

**Split**:
Cutting a track in two at a moment, making a new track from there on.
