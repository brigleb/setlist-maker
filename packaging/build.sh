#!/usr/bin/env bash
# Builds a release into target/dist: the universal app's dmg, the universal CLI as a tarball,
# their SHA256SUMS, the release notes, and the tap's formula with this version's hash filled
# in. `target/dist/tap` is the tap repo's contents. The cask, also filled in, goes to
# `target/dist/cask-draft`: it stays out of the tap until a clean-machine test.
# Publishes nothing. Needs both macOS Rust targets and the Tauri CLI:
#   rustup target add aarch64-apple-darwin x86_64-apple-darwin
#   cargo install tauri-cli --version '^2' --locked
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
fail() { echo "build.sh: $*" >&2; exit 1; }

# One version everywhere: the bundle's, the app crate's and the CLI's (which `--version`
# prints, and the formula's test checks).
version=$(plutil -extract version raw app/tauri.conf.json)
for manifest in app/Cargo.toml crates/cli/Cargo.toml; do
  v=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$manifest")
  [ "$v" = "$version" ] || fail "$manifest says $v, app/tauri.conf.json says $version"
done

dist=target/dist
rm -rf "${dist:?}"
mkdir -p "$dist/tap/Formula" "$dist/cask-draft"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

# The app: Tauri lipos the two builds, bundles, signs ad hoc (bundle.macOS.signingIdentity)
# and makes the dmg (bundle.targets).
(cd app && cargo tauri build --target universal-apple-darwin)
dmg="Hindsight_${version}_universal.dmg"
cp "target/universal-apple-darwin/release/bundle/dmg/$dmg" "$dist/"

# The CLI: one binary per architecture, joined. Each slice keeps the linker's ad-hoc
# signature, which arm64 needs to run.
for t in aarch64-apple-darwin x86_64-apple-darwin; do
  cargo build --release --locked -p hindsight-cli --target "$t"
done
lipo -create -output "$stage/hindsight" \
  target/aarch64-apple-darwin/release/hindsight target/x86_64-apple-darwin/release/hindsight
tarball="hindsight-${version}-macos-universal.tar.gz"
tar -czf "$dist/$tarball" -C "$stage" hindsight

(cd "$dist" && shasum -a 256 "$dmg" "$tarball" > SHA256SUMS)
dmg_sha=$(grep " $dmg\$" "$dist/SHA256SUMS" | cut -d' ' -f1)
cli_sha=$(grep " $tarball\$" "$dist/SHA256SUMS" | cut -d' ' -f1)
fill() { sed -e "s/@VERSION@/$version/g" -e "s/@CLI_SHA256@/$cli_sha/g" -e "s/@DMG_SHA256@/$dmg_sha/g" "$1"; }
fill packaging/homebrew/Formula/hindsight.rb > "$dist/tap/Formula/hindsight.rb"
fill packaging/homebrew/Casks/hindsight.rb > "$dist/cask-draft/hindsight.rb"
fill packaging/release-notes.md > "$dist/release-notes.md"

# The checks a release needs before it goes anywhere.
both_archs() { [ "$(lipo -archs "$1")" = "x86_64 arm64" ] || fail "$1 isn't universal"; }
app=target/universal-apple-darwin/release/bundle/macos/Hindsight.app
both_archs "$app/Contents/MacOS/hindsight-app"
codesign --verify --deep --strict "$app"
signature=$(codesign -dv "$app" 2>&1)
[[ $signature == *Signature=adhoc* ]] || fail "the app isn't signed ad hoc"
rm "$stage/hindsight"
tar -xzf "$dist/$tarball" -C "$stage"
both_archs "$stage/hindsight"
for a in arm64 x86_64; do
  [ "$(arch -"$a" "$stage/hindsight" --version)" = "hindsight $version" ] ||
    fail "the $a CLI doesn't say it's $version"
done
echo "Built $version in $dist:"
cat "$dist/SHA256SUMS"
