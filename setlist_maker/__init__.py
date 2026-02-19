"""Setlist Maker - Generate tracklists from DJ sets using Shazam."""

from importlib.metadata import version

__version__ = version("setlist-maker")

# Supported audio extensions (shared across modules)
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".aiff"}
