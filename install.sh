#!/bin/sh
# Cold Storage — one-command installer.
# Installs `uv` if needed, then installs the `cold` command. Local-only; nothing
# about your data ever touches a network here.
#
#   curl -LsSf https://raw.githubusercontent.com/arhxam/cold-storage/master/install.sh | sh
#
set -eu

REPO="${COLD_REPO:-https://github.com/arhxam/cold-storage}"

echo "→ Cold Storage installer"

if ! command -v uv >/dev/null 2>&1; then
  echo "→ installing uv (the Python installer)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # make uv available in this shell
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "→ installing the 'cold' command…"
# PEP 508 direct reference: package + extras "@ " git source. (Do NOT combine
# `--from git+…` with a `pkg[extra]` argument — uv rejects that as conflicting.)
uv tool install --force "coldstorage[keyring] @ git+${REPO}"

echo ""
echo "✓ Installed. Get started with:"
echo "    cold init"
echo "    cold ingest ~/Downloads/your-export.zip"
echo ""
echo "  If 'cold' isn't found, add uv's tool bin to your PATH:"
echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
