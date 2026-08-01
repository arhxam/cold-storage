#!/bin/bash
# Build a signed + notarized Cold Storage DMG for distribution.
#
#   ./app/release.sh
#
# Requires (one-time setup, see docs/BUILDING-APP.md):
#   • a "Developer ID Application" certificate in the login keychain
#   • a notarytool keychain profile:
#       xcrun notarytool store-credentials "cold-notary" \
#         --key ~/.appstoreconnect/private_keys/AuthKey_XXXX.p8 \
#         --key-id XXXX --issuer <issuer-uuid>
#
# Without a Developer ID the plain `npm run dist` still works; it produces an
# ad-hoc signed app that runs locally but needs a quarantine bypass to install
# from a download.
set -euo pipefail

cd "$(dirname "$0")"
PROFILE="${COLD_NOTARY_PROFILE:-cold-notary}"

IDENTITY="${COLD_SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
  | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' | head -1)}"
if [ -z "$IDENTITY" ]; then
  echo "error: no 'Developer ID Application' certificate found." >&2
  exit 1
fi
echo "→ signing identity: $IDENTITY"

export COLD_SIGN_IDENTITY="$IDENTITY"
# electron-builder picks the cert itself and rejects the "Developer ID
# Application:" prefix — it wants just the common name.
export CSC_NAME="${IDENTITY#Developer ID Application: }"

APP="release/mac-arm64/Cold Storage.app"

# Two passes on purpose.
#
# The app has to be notarized and STAPLED before the disk image is built, so
# that the copy the user drags to /Applications carries its own ticket. Staple
# only the DMG and that ticket is left behind with the disk image: the app's
# first launch then needs a round-trip to Apple, and on a machine that is
# offline (or behind a captive portal) it is refused.
echo "→ building the app…"
npm run dist -- --dir

echo "→ notarizing the app (this can take a few minutes)…"
APPZIP="release/app-for-notarization.zip"
rm -f "$APPZIP"
/usr/bin/ditto -c -k --keepParent "$APP" "$APPZIP"
xcrun notarytool submit "$APPZIP" --keychain-profile "$PROFILE" --wait
rm -f "$APPZIP"

echo "→ stapling the app…"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

# Now package the already-stapled app into a disk image.
echo "→ building the disk image…"
npm run dist -- --prepackaged "$APP"

DMG=$(ls -t release/*.dmg | head -1)
echo "→ built: $DMG"

# Sign the disk image itself as well — an unsigned DMG is still flagged by
# Gatekeeper when the user opens it, even though the app inside is notarized.
echo "→ signing the disk image…"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo "→ notarizing the disk image…"
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "→ stapling the disk image…"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo "→ verifying Gatekeeper acceptance…"
spctl --assess --type execute --verbose=2 "$APP"

echo
echo "✓ Signed + notarized: $DMG"
echo "  Users can download, open, and drag to Applications — no warnings."
