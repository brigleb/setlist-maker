# XDG folders on macOS, not ~/Library

Hindsight keeps its config in `~/.config/hindsight` and its cache in `~/.cache/hindsight`
(or `$XDG_CONFIG_HOME` / `$XDG_CACHE_HOME`), on macOS too. Don't "fix" this with the `dirs`
crate, which would pick `~/Library/Application Support` and `~/Library/Caches`. The config
holds `corrections.json`, every correction ever learned, and the Python version kept it in
`~/.config/setlist-maker`. Hindsight copies it from there on first launch, and a different
convention would orphan it with no gain, since the app isn't sandboxed.
