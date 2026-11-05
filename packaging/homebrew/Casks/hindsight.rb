# The app. NOT in the tap yet: the plan ships a cask only if a clean-machine test shows the
# postflight below gets past Gatekeeper (Homebrew 6 dropped --no-quarantine). Until then the
# dmg and Open Anyway are the way in. Untested. Once a cask named hindsight sits beside the
# formula of that name, the README's `brew install brigleb/hindsight/hindsight` may need
# `--formula` (unverified).
cask "hindsight" do
  version "@VERSION@"
  sha256 "@DMG_SHA256@"

  url "https://github.com/brigleb/setlist-maker/releases/download/v#{version}/Hindsight_#{version}_universal.dmg"
  name "Hindsight"
  desc "Drop a DJ set's MP3 on it to identify its tracks, then edit the tracklist"
  homepage "https://github.com/brigleb/setlist-maker"

  depends_on macos: :sonoma

  app "Hindsight.app"

  # Ad-hoc signed, not notarized: without this Gatekeeper refuses the first launch.
  postflight_steps do
    run "/usr/bin/xattr", args: ["-dr", "com.apple.quarantine", "{{appdir}}/Hindsight.app"]
  end

  # Not ~/.config/hindsight or ~/.cache/hindsight: the CLI shares them, and the corrections
  # learned there are never to be lost.
  zap trash: [
    "~/Library/Caches/com.brigleb.hindsight",
    "~/Library/WebKit/com.brigleb.hindsight",
  ]
end
