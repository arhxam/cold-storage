#!/usr/bin/env bash
# Launch Cold Storage in DEMO mode for recording a video.
#
#   * Data lives in a SEPARATE archive (~/ColdStorageDemo) — your real
#     ~/ColdStorage is never touched.
#   * The archive is encrypted (green "Encrypted" badge) and unlocks from an env
#     passphrase, so there is no keychain prompt and no unlock screen.
#   * Accounts are faked by app/demo-preload.js: clicking "Connect" runs a short,
#     believable backup animation and never signs in to anything real.
#   * The conversations, contacts and photos are real data, seeded by
#     tools/seed_demo.py into the demo archive and served by the engine.
#
# Re-runnable: it only re-seeds the first time.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export COLD_HOME="${COLD_HOME:-$HOME/ColdStorageDemo}"
export COLD_PASSPHRASE="${COLD_PASSPHRASE:-cold-storage-demo}"
export COLD_NO_KEYRING=1
export COLD_DEMO=1

COLD="$REPO/dist/cold/cold"
PY="$REPO/.venv/bin/python"
USER_DATA="$HOME/Library/Application Support/ColdStorageDemo"

echo "Demo archive:  $COLD_HOME"

if [ ! -f "$COLD_HOME/config.toml" ]; then
  echo "Initialising encrypted demo archive…"
  "$COLD" init --passphrase "$COLD_PASSPHRASE" >/dev/null
fi

if [ ! -f "$COLD_HOME/.demo-seeded" ]; then
  echo "Seeding template data (this takes a few seconds)…"
  "$PY" "$REPO/tools/seed_demo.py"
  touch "$COLD_HOME/.demo-seeded"
else
  echo "Template data already seeded."
fi

echo "Launching Cold Storage (demo mode)…"
cd "$REPO/app"
exec npx electron . --user-data-dir="$USER_DATA"
