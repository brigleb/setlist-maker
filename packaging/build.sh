#!/usr/bin/env bash
# Builds a release into target/dist: the universal app's dmg, the universal CLI as a tarball,
# their SHA256SUMS, the release notes, and the Homebrew formula and cask with this version's
# hashes filled in.
# Publishes nothing. Needs both macOS Rust targets and the Tauri CLI:
#   rustup target add aarch64-apple-darwin x86_64-apple-darwin
#   cargo install tauri-cli --version '^2' --locked
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
version=$(sed -n 's/^  "version": "\(.*\)",$/\1/p' app/tauri.conf.json)
[ -n "$version" ] || { echo "no version in app/tauri.conf.json" >&2; exit 1; }
dist=target/dist
rm -rf "${dist:?}"
mkdir -p "$dist/tap/Formula" "$dist/tap/Casks"

# The app: Tauri lipos the two builds, bundles, signs ad hoc (bundle.macOS.signingIdentity)
# and makes the dmg.
(cd app && cargo tauri build --target universal-apple-darwin --bundles app,dmg)
dmg="Hindsight_${version}_universal.dmg"
cp "target/universal-apple-darwin/release/bundle/dmg/$dmg" "$dist/"

# The CLI: one binary per architecture, joined. Each slice keeps the linker's ad-hoc
# signature, which arm64 needs to run.
for t in aarch64-apple-darwin x86_64-apple-darwin; do
  cargo build --release --locked -p hindsight-cli --target "$t"
done
stage=$(mktemp -d)
lipo -create -output "$stage/hindsight" \
  target/aarch64-apple-darwin/release/hindsight target/x86_64-apple-darwin/release/hindsight
tarball="hindsight-${version}-macos-universal.tar.gz"
tar -czf "$dist/$tarball" -C "$stage" .
rm -rf "$stage"

(cd "$dist" && shasum -a 256 "$dmg" "$tarball" > SHA256SUMS)
dmg_sha=$(shasum -a 256 "$dist/$dmg" | cut -d' ' -f1)
cli_sha=$(shasum -a 256 "$dist/$tarball" | cut -d' ' -f1)
for kind in Formula Casks; do
  sed -e "s/@VERSION@/$version/g" -e "s/@CLI_SHA256@/$cli_sha/g" -e "s/@DMG_SHA256@/$dmg_sha/g" \
    "packaging/homebrew/$kind/hindsight.rb" > "$dist/tap/$kind/hindsight.rb"
done
sed "s/@VERSION@/$version/g" packaging/release-notes.md > "$dist/release-notes.md"

# The checks a release needs before it goes anywhere.
app=target/universal-apple-darwin/release/bundle/macos/Hindsight.app
lipo -archs "$app/Contents/MacOS/hindsight-app"
codesign --verify --deep --strict "$app"
signature=$(codesign -dv "$app" 2>&1)
[[ $signature == *Signature=adhoc* ]] || { echo "the app isn't signed ad hoc" >&2; exit 1; }
tar -xzOf "$dist/$tarball" ./hindsight > /dev/null
echo "Built $version in $dist:"
cat "$dist/SHA256SUMS"
