"""
Interactive TUI editor for reviewing and correcting tracklists.

Provides a spreadsheet-like interface for:
- Browsing tracks with arrow keys
- Rejecting tracks with spacebar
- Editing artist/title with Enter
- Saving corrections that improve future identifications
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from setlist_maker import AUDIO_EXTENSIONS
from setlist_maker.playback import PREVIEW_SECONDS, PlaybackController, playback_available


@dataclass
class Track:
    """Represents a single track in the tracklist."""

    timestamp: int  # seconds from start
    artist: str
    title: str
    rejected: bool = False
    shazam_url: str | None = None
    album: str | None = None
    coverart_url: str | None = None
    confidence: float | None = None  # Shazam match-confidence proxy, if known
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
    summary: str | None = None

    def to_markdown(self) -> str:
        """Generate markdown output from the tracklist."""
        lines = [
            f"# Tracklist: {self.source_file}",
            "",
            f"*Generated on {self.generated_on or datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]

        if self.summary:
            lines.append(self.summary)
            lines.append("")

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
                "coverart_url": t.coverart_url,
                "confidence": t.confidence,
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

    # Parse the optional summary paragraph: contiguous prose lines sitting
    # between the "*Generated on*" line and the first numbered track.
    summary_lines: list[str] = []
    seen_generated = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("*Generated on"):
            seen_generated = True
            continue
        if not seen_generated:
            continue
        if track_pattern.match(stripped):
            break
        if stripped:
            summary_lines.append(stripped)
        elif summary_lines:
            break
    if summary_lines:
        tracklist.summary = " ".join(summary_lines)

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
        Binding("p", "play_pause", "Play/Stop"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(
        self,
        tracklist: Tracklist,
        output_path: Path,
        corrections_db: "CorrectionsDB | None" = None,
        audio_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.tracklist = tracklist
        self.output_path = output_path
        self.corrections_db = corrections_db
        self.audio_path = audio_path
        self.unsaved_changes = False
        self.playback = PlaybackController()
        self.playback_enabled = False  # set in on_mount once capability is known
        self._playing_row: int | None = None
        self._playing_since: float | None = None  # monotonic clock when play started

    def _resolve_audio_path(self) -> Path | None:
        """Locate the source audio for previewing track segments.

        Prefers the path threaded in from the CLI (the fresh-identify case);
        falls back to discovering a sibling of the markdown file (the
        edit-an-existing-.md case). Returns None if neither resolves, so a
        moved/renamed file degrades gracefully rather than erroring.
        """
        if self.audio_path is not None and self.audio_path.exists():
            return self.audio_path
        return find_audio_file(self.output_path)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Horizontal(id="info-bar"):
                yield Label(f"File: {self.tracklist.source_file}")
                yield Label(f"Tracks: {len(self.tracklist.tracks)}")
                yield Label(id="playback-label")
                yield Label(id="status-label")
            yield DataTable(id="track-table")
        yield Static(
            "[Space] Reject/Accept  [Enter] Edit  [P] Play/Stop  [S] Save  [Q] Quit  [?] Help",
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

        # Decide once whether previews can be heard here: ffplay on PATH and a
        # supported platform (macOS). This is a cheap PATH/platform check with
        # no subprocess, so it is safe to run inline on mount. Then poll
        # playback state so the now-playing readout clears itself when a preview
        # ends on its own.
        self.playback_enabled = playback_available()
        self.set_interval(0.5, self._tick_playback)

    # ffplay exits 0 within ~1s on an unreadable file or a seek past the end of
    # the recording, so a near-instant stop means the preview never really
    # played rather than that it finished its window.
    _MIN_AUDIBLE_SECONDS = 1.5

    def _tick_playback(self) -> None:
        """Refresh the now-playing readout; clear it when playback stops."""
        label = self.query_one("#playback-label", Label)
        if self.playback.is_playing() and self._playing_row is not None:
            elapsed = min(int(self.playback.elapsed()), PREVIEW_SECONDS)
            track_num = self._playing_row + 1
            label.update(f"[green]▶ Track {track_num}  {elapsed}s/{PREVIEW_SECONDS}s[/]")
        else:
            # A row was previewing but ffplay has stopped on its own. Manual
            # stops (toggle, navigation, edit, unmount) clear _playing_row
            # themselves, so reaching here with it still set means a natural
            # exit -- flag it if it happened too fast to have been heard.
            if self._playing_row is not None:
                since = self._playing_since
                self._playing_row = None
                self._playing_since = None
                if since is not None and (time.monotonic() - since) < self._MIN_AUDIBLE_SECONDS:
                    self.notify(
                        "Could not preview this segment (unreadable audio or "
                        "a timestamp past the end of the recording).",
                        title="Preview failed",
                        severity="warning",
                    )
            label.update("")

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
            # Stop any preview explicitly; the table refresh below would only
            # stop it as an incidental cursor side effect (and not at all when
            # rejecting row 0), so be deliberate about it.
            self._stop_playback()
            track.rejected = not track.rejected
            self.unsaved_changes = True
            self._refresh_table()
            # Keep cursor on same row
            table = self.query_one("#track-table", DataTable)
            table.move_cursor(row=idx)

    def action_play_pause(self) -> None:
        """Preview the current track's 30s window, or stop if it's playing.

        Lets you listen to an unknown track before editing it. Plays the
        recording from the track's start timestamp via a non-blocking ffplay
        subprocess; pressing again (on the same row) stops it.
        """
        if not self.playback_enabled:
            self.notify(
                "Playback unavailable: ffplay (ffmpeg) not found or no audio output here.",
                title="No playback",
                severity="warning",
            )
            return

        result = self._get_current_track()
        if not result:
            return
        idx, track = result

        # Toggle off if this same row is already previewing.
        if self.playback.is_playing() and self._playing_row == idx:
            self._stop_playback()
            self._tick_playback()
            return

        audio_path = self._resolve_audio_path()
        if audio_path is None:
            self.notify(
                "Audio file not found next to the tracklist; cannot preview.",
                title="No audio",
                severity="warning",
            )
            return

        self.playback.play(audio_path, track.timestamp)
        self._playing_row = idx
        self._playing_since = time.monotonic()
        self._tick_playback()

    def action_edit_track(self) -> None:
        """Open edit dialog for current track."""
        result = self._get_current_track()
        if result:
            idx, track = result
            # Stop any preview so audio doesn't keep playing under the modal.
            self._stop_playback()
            self.push_screen(
                EditTrackScreen(track.artist, track.title),
                callback=lambda r: self._on_edit_complete(idx, r),
            )

    def _stop_playback(self) -> None:
        """Stop any active preview and clear the now-playing state."""
        self.playback.stop()
        self._playing_row = None
        self._playing_since = None

    def on_unmount(self) -> None:
        """Ensure no ffplay process outlives the editor."""
        self._stop_playback()

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
        table = self.query_one("#track-table", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move cursor up."""
        table = self.query_one("#track-table", DataTable)
        table.action_cursor_up()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the edit dialog when a row is activated with Enter.

        The focused DataTable binds Enter to its own ``select_cursor`` action,
        which shadows this app's ``enter`` -> ``edit_track`` binding (a focused
        widget's bindings win over the app's). That action posts RowSelected,
        so we hook it here to reach ``action_edit_track`` -- the app-level
        binding only survives to label the footer hint.
        """
        self.action_edit_track()

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Stop any preview when the cursor leaves the playing row.

        Fires for both arrow-key and j/k navigation (DataTable posts this on
        any cursor move), so a preview never keeps playing for a row you have
        scrolled away from.
        """
        if self._playing_row is not None and event.cursor_row != self._playing_row:
            self._stop_playback()

    def action_show_help(self) -> None:
        """Show help information."""
        self.notify(
            "↑↓/jk: Navigate | Space: Reject | Enter: Edit | P: Play/Stop | S: Save | Q: Quit",
            title="Keyboard Shortcuts",
        )


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
    audio_path: Path | None = None,
) -> None:
    """Run the interactive tracklist editor.

    ``audio_path`` is the source recording, passed through when known (the
    fresh-identify path) so track previews work without relying on filename
    discovery; when omitted the editor falls back to finding a sibling of the
    markdown file.
    """
    corrections_db = CorrectionsDB() if use_corrections else None

    # Apply any known corrections
    if corrections_db:
        applied = corrections_db.apply_corrections(tracklist)
        if applied > 0:
            print(f"Applied {applied} learned correction(s) from previous sessions.")

    app = TracklistEditor(tracklist, output_path, corrections_db, audio_path=audio_path)
    app.run()
