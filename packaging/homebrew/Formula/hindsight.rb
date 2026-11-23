# The CLI, installed from the prebuilt universal binary on the tap's own release (the app's
# release page offers only the dmg). Rendered into the brigleb/homebrew-hindsight tap by
# packaging/build.sh, which fills in the version and hash.
class Hindsight < Formula
  desc "Identify the tracks in a DJ set and embed chapter markers"
  homepage "https://github.com/brigleb/setlist-maker"
  url "https://github.com/brigleb/homebrew-hindsight/releases/download/v@VERSION@/hindsight-@VERSION@-macos-universal.tar.gz"
  sha256 "@CLI_SHA256@"
  license "GPL-3.0-or-later"

  depends_on :macos

  def install
    bin.install "hindsight"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/hindsight --version")
  end
end
