#!/bin/bash
# Verify a built DMG the way a downloader's Mac will see it.
#
#   ./app/verify-dmg.sh [path/to.dmg]
#
# Checks the things that actually decide whether a stranger can double-click
# the download and have it work: is the disk image signed, is the app inside
# notarized and stapled (so it opens with no Gatekeeper warning and no
# right-click workaround), does Gatekeeper accept it offline, and is the
# bundled engine present and executable.
# NB: no `pipefail` here on purpose. `cmd | grep -q` exits the grep early,
# which SIGPIPEs the producer — with pipefail that reads as a failed check even
# when the string was found. Text checks below capture output first instead.
set -u

cd "$(dirname "$0")/.."
DMG="${1:-$(ls -t app/release/*.dmg 2>/dev/null | head -1)}"
[ -n "$DMG" ] && [ -f "$DMG" ] || { echo "no DMG found (build one with ./app/release.sh)"; exit 1; }

fail=0
ok()   { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
# Match text in a command's combined output without piping into grep.
contains(){ # label, needle, command...
  local label="$1" needle="$2"; shift 2
  local out; out="$("$@" 2>&1)"
  case "$out" in *"$needle"*) ok "$label" ;; *) bad "$label" ;; esac
}

echo
echo "Verifying $(basename "$DMG")  ($(du -h "$DMG" | cut -f1))"
echo

echo "Disk image"
check "signed"                 "codesign --verify --strict '$DMG'"
check "notarization stapled"   "xcrun stapler validate '$DMG'"

MNT=$(mktemp -d)
if ! hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MNT" >/dev/null 2>&1; then
  bad "disk image mounts"
  echo; echo "$fail check(s) failed"; exit 1
fi
ok "disk image mounts"
trap 'hdiutil detach "$MNT" -quiet >/dev/null 2>&1' EXIT

APP="$MNT/Cold Storage.app"
echo
echo "App bundle"
check "present"                     "[ -d '$APP' ]"
contains "signed with a Developer ID" "Developer ID Application" codesign -dv --verbose=2 "$APP"
check "signature valid (deep)"      "codesign --verify --deep --strict '$APP'"
contains "hardened runtime enabled" "(runtime)" codesign -d --verbose=2 "$APP"
# Stapled to the APP, not just the disk image: once it is dragged to
# /Applications the disk image is gone, and without its own ticket the first
# launch needs a network round-trip to Apple to succeed.
check "notarization stapled to the app" "xcrun stapler validate '$APP'"
# The real question: would Gatekeeper let a stranger open this?
contains "Gatekeeper accepts it (no right-click needed)" "accepted" \
  spctl --assess --type execute --verbose=2 "$APP"

echo
echo "Bundled engine"
ENGINE="$APP/Contents/Resources/cold/cold"
check "present and executable"   "[ -x '$ENGINE' ]"
check "signed"                   "codesign --verify --strict '$ENGINE'"
if [ -x "$ENGINE" ]; then
  VER=$("$ENGINE" version 2>/dev/null | tr -d '\n')
  APPVER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null)
  echo "  INFO  engine reports: ${VER:-<no output>}"
  echo "  INFO  app bundle version: ${APPVER:-unknown}"
  case "$VER" in
    *"$APPVER"*) ok "engine and app versions agree" ;;
    *)           bad "engine and app versions agree" ;;
  esac
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "All checks passed — this DMG is ready to publish."
else
  echo "$fail check(s) failed."
fi
exit "$fail"
