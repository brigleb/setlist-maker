"""
Interactive TUI editor for reviewing and correcting tracklists.

Provides a spreadsheet-like interface for:
- Browsing tracks with arrow keys
- Rejecting tracks with spacebar
- Editing artist/title with Enter
- Playing 30-second audio samples with waveform visualization
- Saving corrections that improve future identifications
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from pydub import AudioSegment

# sounddevice is optional - playback won't work without it but editor still functions
try:
    import sounddevice as sd

    SOUNDDEVICE_AVAILABLE = True
except OSError:
    sd = None  # type: ignore[assignment]
    SOUNDDEVICE_AVAILABLE = False
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from setlist_maker.waveform import colorize, extract_peaks, supports_unicode

# Audio extensions to look for (same as cli.py)
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".aiff"}


@dataclass
class Track:
    """Represents a single track in the tracklist."""

    timestamp: int  # seconds from start
    artist: str
    title: str
    rejected: bool = False
    shazam_url: str | None = None
    album: str | None = None
    original_artist: str | None = None  # For tracking corrections
    original_title: str | None = None

    @property
    def time_str(self) -> str:
        """Format timestamp as HH:MM:SS or MM:SS."""
        hours = self.timestamp // 3600
        minutes = (self.timestamp % 3600) // 60
        secs = self.timestamp % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @property
    def is_unidentified(self) -> bool:
        """Check if this track was not identified by Shazam."""
        return not self.artist and not self.title

    @property
    def was_corrected(self) -> bool:
        """Check if this track was manually corrected."""
        if self.original_artist is None and self.original_title is None:
            return False
        return self.artist != self.original_artist or self.title != self.original_title


@dataclass
class Tracklist:
    """A complete tracklist for an audio file."""

    source_file: str
    tracks: list[Track] = field(default_factory=list)
    generated_on: str | None = None

    def to_markdown(self) -> str:
        """Generate markdown output from the tracklist."""
        lines = [
            f"# Tracklist: {self.source_file}",
            "",
            f"*Generated on {self.generated_on or datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]

        track_num = 1
        for track in self.tracks:
            if track.rejected:
                continue
            time_str = track.time_str
            if track.is_unidentified:
                lines.append(f"{track_num}. *Unidentified* ({time_str})")
            else:
                lines.append(f"{track_num}. **{track.artist}** - {track.title} ({time_str})")
            track_num += 1

        lines.append("")
        return "\n".join(lines)

    def to_json(self) -> list[dict]:
        """Export tracklist as JSON-serializable list."""
        return [
            {
                "timestamp": t.timestamp,
                "time": t.time_str,
                "artist": t.artist,
                "title": t.title,
                "rejected": t.rejected,
                "shazam_url": t.shazam_url,
                "album": t.album,
            }
            for t in self.tracks
            if not t.rejected
        ]


def parse_markdown_tracklist(content: str) -> Tracklist:
    """Parse a markdown tracklist file into a Tracklist object."""
    lines = content.strip().split("\n")
    tracklist = Tracklist(source_file="")

    # Parse header: # Tracklist: filename.mp3
    for line in lines:
        if line.startswith("# Tracklist:"):
            tracklist.source_file = line.replace("# Tracklist:", "").strip()
            break

    # Parse generation date: *Generated on YYYY-MM-DD HH:MM*
    for line in lines:
        if line.startswith("*Generated on"):
            match = re.search(r"\*Generated on (.+)\*", line)
            if match:
                tracklist.generated_on = match.group(1)
            break

    # Parse tracks: "1. **Artist** - Title (MM:SS)" or "1. *Unidentified* (MM:SS)"
    track_pattern = re.compile(
        r"^\d+\.\s+"
        r"(?:"
        r"\*\*(.+?)\*\*\s*-\s*(.+?)"  # **Artist** - Title
        r"|"
        r"\*Unidentified\*"  # *Unidentified*
        r")\s*"
        r"\((\d+:\d+(?::\d+)?)\)"  # (MM:SS) or (H:MM:SS)
    )

    for line in lines:
        match = track_pattern.match(line.strip())
        if match:
            artist = match.group(1) or ""
            title = match.group(2) or ""
            time_str = match.group(3)

            # Parse timestamp
            parts = time_str.split(":")
            if len(parts) == 3:
                timestamp = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                timestamp = int(parts[0]) * 60 + int(parts[1])

            track = Track(
                timestamp=timestamp,
                artist=artist.strip(),
                title=title.strip(),
                original_artist=artist.strip() if artist else None,
                original_title=title.strip() if title else None,
            )
            tracklist.tracks.append(track)

    return tracklist


class PlaybackEngine:
    """
    Audio playback engine for playing segments from an audio file.

    Uses pydub to extract segments and sounddevice for non-blocking playback.
    """

    def __init__(self, audio_path: Path):
        """
        Initialize the playback engine.

        Args:
            audio_path: Path to the audio file

        Raises:
            RuntimeError: If sounddevice/PortAudio is not available
        """
        if not SOUNDDEVICE_AVAILABLE:
            raise RuntimeError(
                "Audio playback requires PortAudio. "
                "Install it with: apt install libportaudio2"
            )
        self.audio_path = audio_path
        self._audio: AudioSegment | None = None
        self._playing = False
        self._stream: sd.OutputStream | None = None  # type: ignore[name-defined]
        self._playback_position = 0
        self._total_frames = 0
        self._lock = threading.Lock()
        self._waveform_peaks: list[float] = []

    def _load_audio(self) -> None:
        """Load the audio file if not already loaded."""
        if self._audio is None:
            self._audio = AudioSegment.from_file(str(self.audio_path))

    def play_segment(
        self, start_seconds: int, duration: int = 30
    ) -> tuple[list[float], int]:
        """
        Start playing a segment of the audio file.

        Args:
            start_seconds: Start position in seconds
            duration: Duration to play in seconds (default 30)

        Returns:
            Tuple of (waveform peaks for visualization, sample rate)
        """
        self.stop()

        self._load_audio()
        assert self._audio is not None

        # Extract the segment
        start_ms = start_seconds * 1000
        end_ms = start_ms + (duration * 1000)
        segment = self._audio[start_ms:end_ms]

        # Convert to numpy array for sounddevice
        samples = np.array(segment.get_array_of_samples(), dtype=np.float32)

        # Normalize to -1.0 to 1.0 range
        max_val = float(2 ** (segment.sample_width * 8 - 1))
        samples = samples / max_val

        # Handle stereo
        if segment.channels == 2:
            samples = samples.reshape((-1, 2))

        # Extract waveform peaks for visualization (enough for ~80 char width)
        raw_samples = segment.get_array_of_samples()
        self._waveform_peaks = extract_peaks(raw_samples, 160, segment.channels)

        self._total_frames = len(samples)
        self._playback_position = 0

        # Create callback for position tracking
        def callback(outdata, frames, time_info, status):
            with self._lock:
                start = self._playback_position
                end = start + frames

                if end > len(samples):
                    # End of audio
                    outdata[: len(samples) - start] = samples[start:]
                    outdata[len(samples) - start :] = 0
                    self._playing = False
                    raise sd.CallbackStop()
                else:
                    outdata[:] = samples[start:end]
                    self._playback_position = end

        # Start playback
        self._stream = sd.OutputStream(
            samplerate=segment.frame_rate,
            channels=segment.channels,
            callback=callback,
            finished_callback=self._on_finished,
        )
        self._playing = True
        self._stream.start()

        return self._waveform_peaks, segment.frame_rate

    def _on_finished(self) -> None:
        """Called when playback finishes."""
        with self._lock:
            self._playing = False

    def stop(self) -> None:
        """Stop playback."""
        with self._lock:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            self._playing = False
            self._playback_position = 0

    def is_playing(self) -> bool:
        """Check if audio is currently playing."""
        with self._lock:
            return self._playing

    def get_position(self) -> float:
        """
        Get current playback position as a fraction (0.0 to 1.0).

        Returns:
            Playback progress from 0.0 (start) to 1.0 (end)
        """
        with self._lock:
            if self._total_frames == 0:
                return 0.0
            return min(1.0, self._playback_position / self._total_frames)

    def get_waveform_peaks(self) -> list[float]:
        """Get the waveform peaks for visualization."""
        return self._waveform_peaks


def find_audio_file(markdown_path: Path) -> Path | None:
    """
    Find the audio file matching a markdown tracklist.

    Looks for audio files with the same base name as the markdown file
    in the same directory.

    Args:
        markdown_path: Path to the markdown tracklist file

    Returns:
        Path to the matching audio file, or None if not found
    """
    base_name = markdown_path.stem
    parent_dir = markdown_path.parent

    # First, try exact base name match
    for ext in AUDIO_EXTENSIONS:
        candidate = parent_dir / f"{base_name}{ext}"
        if candidate.exists():
            return candidate

    # Also try removing _tracklist suffix if present
    if base_name.endswith("_tracklist"):
        audio_name = base_name[: -len("_tracklist")]
        for ext in AUDIO_EXTENSIONS:
            candidate = parent_dir / f"{audio_name}{ext}"
            if candidate.exists():
                return candidate

    return None


class EditTrackScreen(ModalScreen[tuple[str, str] | None]):
    """Modal screen for editing a track's artist and title."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    EditTrackScreen {
        align: center middle;
    }

    #edit-dialog {
        width: 60;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #edit-dialog Label {
        margin-bottom: 1;
    }

    #edit-dialog Input {
        margin-bottom: 1;
    }

    #button-row {
        margin-top: 1;
        align: center middle;
    }

    #button-row Button {
        margin: 0 1;
    }
    """

    def __init__(self, artist: str, title: str) -> None:
        super().__init__()
        self.initial_artist = artist
        self.initial_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label("Edit Track", id="edit-title")
            yield Label("Artist:")
            yield Input(value=self.initial_artist, id="artist-input", placeholder="Artist name")
            yield Label("Title:")
            yield Input(value=self.initial_title, id="title-input", placeholder="Track title")
            with Horizontal(id="button-row"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#artist-input", Input).focus()

    @on(Button.Pressed, "#save-btn")
    def save_changes(self) -> None:
        artist = self.query_one("#artist-input", Input).value.strip()
        title = self.query_one("#title-input", Input).value.strip()
        self.dismiss((artist, title))

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "artist-input":
            self.query_one("#title-input", Input).focus()
        else:
            self.save_changes()


class TracklistEditor(App[None]):
    """Interactive TUI for editing tracklists."""

    TITLE = "Setlist Maker - Tracklist Editor"

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 100%;
    }

    #info-bar {
        height: 3;
        background: $primary-background;
        padding: 0 1;
    }

    #info-bar Label {
        margin-right: 2;
    }

    #waveform-bar {
        height: 3;
        background: $primary-background;
        padding: 0 1;
        display: none;
    }

    #waveform-bar.playing {
        display: block;
    }

    #info-bar.hidden {
        display: none;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--cursor {
        background: $accent;
    }

    #help-bar {
        height: 1;
        background: $primary-background;
        padding: 0 1;
        color: $text-muted;
    }

    .rejected {
        color: $text-disabled;
        text-style: strike;
    }

    .corrected {
        color: $success;
    }

    .unidentified {
        color: $warning;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "save", "Save"),
        Binding("space", "toggle_reject", "Reject/Accept"),
        Binding("enter", "edit_track", "Edit"),
        Binding("p", "play", "Play"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(
        self,
        tracklist: Tracklist,
        output_path: Path,
        corrections_db: "CorrectionsDB | None" = None,
    ) -> None:
        super().__init__()
        self.tracklist = tracklist
        self.output_path = output_path
        self.corrections_db = corrections_db
        self.unsaved_changes = False

        # Audio playback
        self.audio_file = find_audio_file(output_path)
        self.playback_engine: PlaybackEngine | None = None
        self._playback_timer: object | None = None
        self._waveform_peaks: list[float] = []
        self._playback_start_timestamp: int = 0
        self._loading_audio: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Horizontal(id="info-bar"):
                yield Label(f"File: {self.tracklist.source_file}")
                yield Label(f"Tracks: {len(self.tracklist.tracks)}")
                yield Label(id="status-label")
            yield Static("", id="waveform-bar")
            yield DataTable(id="track-table")
        yield Static(
            "[Space] Reject/Accept  [Enter] Edit  [P] Play  [S] Save  [Q] Quit  [?] Help",
            id="help-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#track-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        # Add columns
        table.add_column("#", width=4)
        table.add_column("Time", width=10)
        table.add_column("Artist", width=30)
        table.add_column("Title", width=40)
        table.add_column("Status", width=12)

        # Populate rows
        self._refresh_table()

    def _refresh_table(self) -> None:
        """Refresh the table contents from the tracklist."""
        table = self.query_one("#track-table", DataTable)
        table.clear()

        for i, track in enumerate(self.tracklist.tracks):
            status = ""
            if track.rejected:
                status = "[red]REJECTED[/]"
            elif track.was_corrected:
                status = "[green]EDITED[/]"
            elif track.is_unidentified:
                status = "[yellow]UNKNOWN[/]"

            artist_display = track.artist if track.artist else "[dim italic]Unknown[/]"
            title_display = track.title if track.title else "[dim italic]Unknown[/]"

            if track.rejected:
                artist_display = f"[strike dim]{track.artist}[/]"
                title_display = f"[strike dim]{track.title}[/]"

            table.add_row(
                str(i + 1),
                track.time_str,
                artist_display,
                title_display,
                status,
                key=str(i),
            )

        self._update_status()

    def _update_status(self) -> None:
        """Update the status label."""
        status = self.query_one("#status-label", Label)
        rejected_count = sum(1 for t in self.tracklist.tracks if t.rejected)
        edited_count = sum(1 for t in self.tracklist.tracks if t.was_corrected)

        parts = []
        if rejected_count:
            parts.append(f"Rejected: {rejected_count}")
        if edited_count:
            parts.append(f"Edited: {edited_count}")
        if self.unsaved_changes:
            parts.append("[bold red]UNSAVED[/]")

        status.update(" | ".join(parts) if parts else "")

    def _get_current_track(self) -> tuple[int, Track] | None:
        """Get the currently selected track."""
        table = self.query_one("#track-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.tracklist.tracks):
            return table.cursor_row, self.tracklist.tracks[table.cursor_row]
        return None

    def action_toggle_reject(self) -> None:
        """Toggle rejected status of current track."""
        result = self._get_current_track()
        if result:
            idx, track = result
            track.rejected = not track.rejected
            self.unsaved_changes = True
            self._refresh_table()
            # Keep cursor on same row
            table = self.query_one("#track-table", DataTable)
            table.move_cursor(row=idx)

    def action_edit_track(self) -> None:
        """Open edit dialog for current track."""
        result = self._get_current_track()
        if result:
            idx, track = result
            self.push_screen(
                EditTrackScreen(track.artist, track.title),
                callback=lambda r: self._on_edit_complete(idx, r),
            )

    def _on_edit_complete(self, idx: int, result: tuple[str, str] | None) -> None:
        """Handle edit dialog completion."""
        if result is not None:
            artist, title = result
            track = self.tracklist.tracks[idx]

            # Store original values for correction learning
            if track.original_artist is None:
                track.original_artist = track.artist
            if track.original_title is None:
                track.original_title = track.title

            track.artist = artist
            track.title = title
            self.unsaved_changes = True

            # Record correction for learning
            if self.corrections_db and track.was_corrected:
                self.corrections_db.add_correction(
                    original_artist=track.original_artist or "",
                    original_title=track.original_title or "",
                    corrected_artist=artist,
                    corrected_title=title,
                )

            self._refresh_table()
            table = self.query_one("#track-table", DataTable)
            table.move_cursor(row=idx)

    def action_save(self) -> None:
        """Save the tracklist to file."""
        # Save markdown
        markdown = self.tracklist.to_markdown()
        with open(self.output_path, "w") as f:
            f.write(markdown)

        # Also save JSON version
        json_path = self.output_path.with_suffix(".json")
        with open(json_path, "w") as f:
            json.dump(self.tracklist.to_json(), f, indent=2)

        # Save corrections database
        if self.corrections_db:
            self.corrections_db.save()

        self.unsaved_changes = False
        self._update_status()
        self.notify(f"Saved to {self.output_path}", title="Saved")

    def action_quit(self) -> None:
        """Quit the editor."""
        self._stop_playback()
        if self.unsaved_changes:
            self.notify(
                "You have unsaved changes! Press S to save or Q again to quit.",
                title="Unsaved Changes",
                severity="warning",
            )
            self.unsaved_changes = False  # Allow quit on second press
        else:
            self.exit()

    def action_cursor_down(self) -> None:
        """Move cursor down."""
        self._stop_playback()
        table = self.query_one("#track-table", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move cursor up."""
        self._stop_playback()
        table = self.query_one("#track-table", DataTable)
        table.action_cursor_up()

    def action_show_help(self) -> None:
        """Show help information."""
        self.notify(
            "↑↓/jk: Navigate | Space: Reject | Enter: Edit | P: Play | S: Save | Q: Quit",
            title="Keyboard Shortcuts",
        )

    def action_play(self) -> None:
        """Play/stop the 30-second sample at the current track's timestamp."""
        # If already playing, stop
        if self.playback_engine and self.playback_engine.is_playing():
            self._stop_playback()
            return

        # If currently loading, ignore
        if hasattr(self, "_loading_audio") and self._loading_audio:
            return

        # Check if audio file exists
        if self.audio_file is None:
            self.notify(
                "No audio file found. Expected MP3 in same directory as tracklist.",
                title="Audio Not Found",
                severity="error",
            )
            return

        # Get current track
        result = self._get_current_track()
        if not result:
            return

        _, track = result
        self._playback_start_timestamp = track.timestamp

        # Show loading indicator
        self._show_loading_bar()

        # Start loading in background worker
        self._start_playback_worker(track.timestamp)

    @work(thread=True)
    def _start_playback_worker(self, timestamp: int) -> None:
        """Load and start audio playback in a background thread."""
        self._loading_audio = True
        try:
            # Initialize playback engine if needed (loads audio file)
            if self.playback_engine is None:
                self.playback_engine = PlaybackEngine(self.audio_file)

            # Start playback and get waveform data
            self._waveform_peaks, _ = self.playback_engine.play_segment(timestamp)

            # Schedule UI update on main thread
            self.call_from_thread(self._on_playback_started)

        except Exception as e:
            self.call_from_thread(self._on_playback_error, str(e))
        finally:
            self._loading_audio = False

    def _on_playback_started(self) -> None:
        """Called on main thread when playback has started."""
        # Show waveform bar, hide info bar
        self._show_waveform_bar(True)

        # Start timer to update waveform display
        self._playback_timer = self.set_interval(0.1, self._update_waveform_display)

    def _on_playback_error(self, error_message: str) -> None:
        """Called on main thread when playback fails."""
        self.notify(
            f"Playback failed: {error_message}",
            title="Playback Error",
            severity="error",
        )
        self._stop_playback()

    def _show_loading_bar(self) -> None:
        """Show a loading indicator while audio loads."""
        waveform_bar = self.query_one("#waveform-bar", Static)
        waveform_bar.update("⏳ Loading audio...")
        info_bar = self.query_one("#info-bar", Horizontal)
        info_bar.add_class("hidden")
        waveform_bar.add_class("playing")

    def _stop_playback(self) -> None:
        """Stop any current playback and restore UI."""
        self._loading_audio = False

        if self.playback_engine:
            self.playback_engine.stop()

        if self._playback_timer:
            self._playback_timer.stop()
            self._playback_timer = None

        self._show_waveform_bar(False)

    def _show_waveform_bar(self, show: bool) -> None:
        """Toggle visibility of waveform bar vs info bar."""
        info_bar = self.query_one("#info-bar", Horizontal)
        waveform_bar = self.query_one("#waveform-bar", Static)

        if show:
            info_bar.add_class("hidden")
            waveform_bar.add_class("playing")
        else:
            info_bar.remove_class("hidden")
            waveform_bar.remove_class("playing")
            waveform_bar.update("")

    def _update_waveform_display(self) -> None:
        """Update the waveform display during playback."""
        if not self.playback_engine or not self.playback_engine.is_playing():
            self._stop_playback()
            return

        # Get playback position
        position = self.playback_engine.get_position()
        elapsed_seconds = int(position * 30)
        total_seconds = 30

        # Format timestamp
        timestamp = self._playback_start_timestamp
        hours = timestamp // 3600
        minutes = (timestamp % 3600) // 60
        secs = timestamp % 60
        if hours > 0:
            time_str = f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            time_str = f"{minutes}:{secs:02d}"

        # Build waveform visualization
        waveform_str = self._render_waveform_progress(position)

        # Build display string
        play_icon = "▶"
        display = f"{play_icon} {time_str}  {waveform_str}  [{elapsed_seconds}s/{total_seconds}s]"

        waveform_bar = self.query_one("#waveform-bar", Static)
        waveform_bar.update(display)

    def _render_waveform_progress(self, position: float) -> str:
        """
        Render waveform with playback progress indicator.

        Args:
            position: Playback progress from 0.0 to 1.0

        Returns:
            Colored waveform string with progress indicator
        """
        if not self._waveform_peaks:
            return ""

        # Target ~60 characters for the waveform
        target_width = 60
        peaks = self._waveform_peaks

        # Resample peaks to target width * 2 (for braille pairs)
        num_chars = min(target_width, len(peaks) // 2)
        step = len(peaks) / (num_chars * 2)

        use_unicode = supports_unicode()
        chars = []

        for i in range(num_chars):
            idx = int(i * 2 * step)
            idx2 = int((i * 2 + 1) * step)
            amp_left = peaks[idx] if idx < len(peaks) else 0.0
            amp_right = peaks[idx2] if idx2 < len(peaks) else 0.0
            avg_amp = (amp_left + amp_right) / 2

            # Determine if this position has been played
            char_position = i / num_chars
            played = char_position < position

            if use_unicode:
                # Use braille for waveform
                from setlist_maker.waveform import amplitude_to_braille

                char = amplitude_to_braille(amp_left, amp_right)

                if played:
                    # Bright cyan for played portion
                    char = f"\033[96m{char}\033[0m"
                else:
                    # Dim for unplayed portion
                    char = colorize(char, avg_amp * 0.5)
            else:
                # ASCII fallback
                from setlist_maker.waveform import amplitude_to_ascii

                char = amplitude_to_ascii(amp_left, amp_right)
                if not played:
                    char = f"\033[90m{char}\033[0m"

            chars.append(char)

        return "".join(chars)


class CorrectionsDB:
    """
    Database for storing and applying user corrections.

    Corrections are stored as mappings from (original_artist, original_title)
    to (corrected_artist, corrected_title). This allows the system to learn
    from user corrections and apply them automatically in future runs.
    """

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            # Default to ~/.config/setlist-maker/corrections.json
            config_dir = Path.home() / ".config" / "setlist-maker"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = config_dir / "corrections.json"

        self.db_path = db_path
        self.corrections: dict[str, dict[str, str]] = {}
        self._load()

    def _make_key(self, artist: str, title: str) -> str:
        """Create a normalized key for lookup."""
        return f"{artist.lower().strip()}|||{title.lower().strip()}"

    def _load(self) -> None:
        """Load corrections from disk."""
        if self.db_path.exists():
            try:
                with open(self.db_path) as f:
                    data = json.load(f)
                    self.corrections = data.get("corrections", {})
            except (json.JSONDecodeError, IOError):
                self.corrections = {}

    def save(self) -> None:
        """Save corrections to disk."""
        with open(self.db_path, "w") as f:
            json.dump({"corrections": self.corrections}, f, indent=2)

    def add_correction(
        self,
        original_artist: str,
        original_title: str,
        corrected_artist: str,
        corrected_title: str,
    ) -> None:
        """Record a correction."""
        key = self._make_key(original_artist, original_title)
        self.corrections[key] = {
            "artist": corrected_artist,
            "title": corrected_title,
            "original_artist": original_artist,
            "original_title": original_title,
            "corrected_at": datetime.now().isoformat(),
        }

    def get_correction(self, artist: str, title: str) -> tuple[str, str] | None:
        """Look up a correction for a given artist/title."""
        key = self._make_key(artist, title)
        if key in self.corrections:
            corr = self.corrections[key]
            return corr["artist"], corr["title"]
        return None

    def apply_corrections(self, tracklist: Tracklist) -> int:
        """Apply known corrections to a tracklist. Returns count of corrections applied."""
        applied = 0
        for track in tracklist.tracks:
            # Skip unidentified tracks - no point looking up corrections for empty strings
            if track.is_unidentified:
                continue
            correction = self.get_correction(track.artist, track.title)
            if correction:
                track.original_artist = track.artist
                track.original_title = track.title
                track.artist, track.title = correction
                applied += 1
        return applied


def run_editor(
    tracklist: Tracklist,
    output_path: Path,
    use_corrections: bool = True,
) -> None:
    """Run the interactive tracklist editor."""
    corrections_db = CorrectionsDB() if use_corrections else None

    # Apply any known corrections
    if corrections_db:
        applied = corrections_db.apply_corrections(tracklist)
        if applied > 0:
            print(f"Applied {applied} learned correction(s) from previous sessions.")

    app = TracklistEditor(tracklist, output_path, corrections_db)
    app.run()
