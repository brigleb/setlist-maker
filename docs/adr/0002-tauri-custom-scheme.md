# Tauri, serving the existing page through a custom URI scheme

The app is Tauri 2, and its window loads the existing web editor through a `hindsight://`
scheme handler that answers the same paths Python's server did. The front end is reused
unchanged, there is no npm or build step, and there is no listening socket, so the DNS-rebinding
host guard isn't needed in the app. The same router also runs behind a loopback server
(`hindsight serve`, guarded) only because Playwright can't drive a Tauri webview.

## Considered Options

- **Electron around `hindsight serve`:** Chrome, which the page was built in, but a much larger
  bundle and a socket again. Rejected after spike 0 (2026-11-03): under Playwright WebKit only
  two real issues appeared, both small front-end fixes (one was a bug in Chromium too).
- **A native UI:** would mean rewriting about 5.2k lines of working front end.
