#!/bin/bash
# Publish a built DMG as a GitHub release.
#
#   ./app/publish.sh v0.3.1 "Release title" notes.md
#
# Uploads the DMG twice on purpose: once under its versioned name (so old
# releases stay individually addressable) and once as SaveYourShit-macOS-arm64.dmg.
# That second, unversioned name is what the README's download button points at,
# via /releases/latest/download/<name> — a URL that keeps working forever
# because it always resolves to the newest release. Forget it and the button
# silently 404s the next time you ship.
set -euo pipefail

cd "$(dirname "$0")/.."
TAG="${1:?usage: publish.sh <tag> [title] [notes-file]}"
TITLE="${2:-Save Your Shit ${TAG#v}}"
NOTES="${3:-}"
STABLE_NAME="SaveYourShit-macOS-arm64.dmg"

DMG=$(ls -t app/release/*.dmg 2>/dev/null | head -1)
[ -n "$DMG" ] || { echo "no DMG found — run ./app/release.sh first" >&2; exit 1; }

echo "→ verifying the disk image…"
./app/verify-dmg.sh "$DMG"

# Version in the bundle must match the tag being published.
APPVER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  "app/release/mac-arm64/Save Your Shit.app/Contents/Info.plist")
if [ "${TAG#v}" != "$APPVER" ]; then
  echo "error: tag $TAG does not match the built app version $APPVER" >&2
  exit 1
fi

echo "→ building the Python distributables…"
rm -f dist/*.whl dist/*.tar.gz
uv build --out-dir dist >/dev/null

STAGE=$(mktemp -d)
cp "$DMG" "$STAGE/$(basename "$DMG" | tr ' ' '-')"
cp "$DMG" "$STAGE/$STABLE_NAME"

ARGS=(--title "$TITLE" --target master)
[ -n "$NOTES" ] && ARGS+=(--notes-file "$NOTES") || ARGS+=(--generate-notes)

# Braces are required here: the ellipsis is multi-byte, and an unbraced
# "$TAG…" makes bash read those bytes as part of the variable name.
echo "→ publishing ${TAG}…"
gh release create "$TAG" "${ARGS[@]}" \
  "$STAGE/$(basename "$DMG" | tr ' ' '-')" \
  "$STAGE/$STABLE_NAME" \
  dist/*.whl dist/*.tar.gz
rm -rf "$STAGE"

echo "→ checking the download button actually works…"
URL="https://github.com/arhxam/save-your-shit/releases/latest/download/$STABLE_NAME"
CODE=$(curl -sIL -o /dev/null -w "%{http_code}" --max-time 60 "$URL")
[ "$CODE" = "200" ] || { echo "error: download link returned $CODE — $URL" >&2; exit 1; }

echo
echo "✓ Published $TAG"
echo "  Download button: $URL"
