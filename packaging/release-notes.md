Hindsight @VERSION@, for macOS 14 (Sonoma) or later, on Apple silicon and Intel.

Download `Hindsight_@VERSION@_universal.dmg`, open it, and drag Hindsight to
Applications. It isn't notarized by Apple, so the first launch takes a detour:

1. Open Hindsight. macOS says it can't verify the app; click **Done** (not Move to Trash).
2. Open **System Settings → Privacy & Security**, scroll to Security, and click
   **Open Anyway** beside "Hindsight was blocked".
3. Confirm with your password or Touch ID, then click **Open**.

After that it opens normally. Control-click → Open no longer skips this on macOS 15 and later.

The dmg's SHA-256 is in `SHA256SUMS`.
