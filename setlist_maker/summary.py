"""Playlist summary generation via the Claude CLI (`claude -p`)."""

import shutil
import subprocess
import tempfile

CLAUDE_TIMEOUT_SECONDS = 120

PROMPT_TEMPLATE = """\
Write a single succinct paragraph (3-4 sentences) describing the playlist below.
Focus on the overall genres, sound, and mood across the set. Avoid clichés and
goofy phrasing -- write plainly and accurately, as a knowledgeable listener would.
Do not list individual tracks or use bullet points. Output only the paragraph,
with no preamble, heading, or quotation marks.

Tracklist:
{tracks}
"""


def generate_summary(track_lines: list[str]) -> str | None:
    """
    Generate a one-paragraph playlist description by shelling out to `claude -p`.

    Returns the paragraph text, or None if it could not be generated (the CLI is
    missing, errored, timed out, or returned nothing). On failure a warning is
    printed and the caller continues without a summary.
    """
    if not track_lines:
        return None

    if shutil.which("claude") is None:
        print("  Warning: 'claude' CLI not found; skipping playlist summary.")
        return None

    prompt = PROMPT_TEMPLATE.format(tracks="\n".join(track_lines))

    # Run from a throwaway directory with --strict-mcp-config so the nested
    # Claude session doesn't inherit this project's CLAUDE.md, hooks, or MCP
    # servers, which would otherwise contaminate the one-shot prompt.
    try:
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                ["claude", "-p", "--strict-mcp-config", prompt],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_SECONDS,
                cwd=cwd,
            )
    except subprocess.TimeoutExpired:
        print("  Warning: Claude summary timed out; skipping playlist summary.")
        return None
    except Exception as e:
        print(f"  Warning: Could not run Claude for summary ({e}); skipping.")
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        print(f"  Warning: Claude summary failed; skipping. {stderr}".rstrip())
        return None

    summary = result.stdout.strip()
    if not summary:
        print("  Warning: Claude returned an empty summary; skipping.")
        return None

    return summary
