#!/bin/sh
# Save Your Shit — one-command installer.
# Installs `uv` if needed, then installs the `syt` command. Local-only; nothing
# about your data ever touches a network here.
#
#   curl -LsSf https://raw.githubusercontent.com/arhxam/save-your-shit/master/install.sh | sh
#
set -eu

REPO="${SYT_REPO:-https://github.com/arhxam/save-your-shit}"

echo "→ Save Your Shit installer"

if ! command -v uv >/dev/null 2>&1; then
  echo "→ installing uv (the Python installer)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # make uv available in this shell
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "→ installing the 'syt' command…"
# PEP 508 direct reference: package + extras "@ " git source. (Do NOT combine
# `--from git+…` with a `pkg[extra]` argument — uv rejects that as conflicting.)
uv tool install --force "saveyourshit[keyring] @ git+${REPO}"

echo ""
echo "✓ Installed. Get started with:"
echo "    syt init"
echo "    syt ingest ~/Downloads/your-export.zip"
echo ""
echo "  If 'syt' isn't found, add uv's tool bin to your PATH:"
echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
