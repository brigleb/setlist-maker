"""What each layer is allowed to import (#65).

Run in a fresh interpreter: the pytest process has already imported the TUI
(conftest pulls in the model, and other test modules the editor), so checking
``sys.modules`` here would see everything every test ever loaded.
"""

import subprocess
import sys

import pytest

HEAVY = ("textual", "shazamio", "aiohttp")

# module -> the heavy packages it must not pull in
RULES = {
    "setlist_maker.boundary": HEAVY,  # pure: no network, no TUI
    "setlist_maker.identity": HEAVY,
    "setlist_maker.tracklist": HEAVY,  # the model every front end shares
    "setlist_maker.chapters": HEAVY,
    "setlist_maker.web_editor": ("textual",),  # samples Shazam, but has no TUI
    "setlist_maker.adaptive": ("textual",),
}


@pytest.mark.parametrize("module", sorted(RULES))
def test_module_does_not_import_what_its_layer_forbids(module):
    forbidden = RULES[module]
    code = (
        f"import sys, {module}\n"
        f"loaded = {{name.split('.')[0] for name in sys.modules}}\n"
        f"print(','.join(sorted(loaded & {set(forbidden)!r})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"{module} imports {out}"
