#!/bin/bash
# Build a signed + notarized Save Your Shit DMG for distribution.
#
#   ./app/release.sh
#
# Requires (one-time setup, see docs/BUILDING-APP.md):
#   • a "Developer ID Application" certificate in the login keychain
#   • a notarytool keychain profile:
#       xcrun notarytool store-credentials "syt-notary" \
#         --key ~/.appstoreconnect/private_keys/AuthKey_XXXX.p8 \
#         --key-id XXXX --issuer <issuer-uuid>
#
# Without a Developer ID the plain `npm run dist` still works; it produces an
# ad-hoc signed app that runs locally but needs a quarantine bypass to install
# from a download.
set -euo pipefail

cd "$(dirname "$0")"
PROFILE="${SYT_NOTARY_PROFILE:-syt-notary}"

IDENTITY="${SYT_SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
  | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' | head -1)}"
if [ -z "$IDENTITY" ]; then
  echo "error: no 'Developer ID Application' certificate found." >&2
  exit 1
fi
echo "→ signing identity: $IDENTITY"

export SYT_SIGN_IDENTITY="$IDENTITY"
# electron-builder picks the cert itself and rejects the "Developer ID
# Application:" prefix — it wants just the common name.
export CSC_NAME="${IDENTITY#Developer ID Application: }"

echo "→ building…"
npm run dist

DMG=$(ls -t release/*.dmg | head -1)
echo "→ built: $DMG"

# Sign the disk image itself as well — an unsigned DMG is still flagged by
# Gatekeeper when the user opens it, even though the app inside is notarized.
echo "→ signing the disk image…"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo "→ notarizing (this can take a few minutes)…"
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "→ stapling…"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo "→ verifying Gatekeeper acceptance…"
APP="release/mac-arm64/Save Your Shit.app"
xcrun stapler staple "$APP" || true
spctl --assess --type execute --verbose=2 "$APP"

echo
echo "✓ Signed + notarized: $DMG"
echo "  Users can download, open, and drag to Applications — no warnings."
