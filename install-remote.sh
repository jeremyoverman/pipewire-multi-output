#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/jeremyoverman/pipewire-multi-output.git"
INSTALL_DIR="${HOME}/.local/share/pipewire-multi-output"

echo "Installing pipewire-multi-output..."
echo ""

if ! command -v git >/dev/null 2>&1; then
    echo "Error: git is required. Install it first."
    exit 1
fi

if [[ -d "$INSTALL_DIR" ]]; then
    echo "Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi

exec "$INSTALL_DIR/install.sh"
