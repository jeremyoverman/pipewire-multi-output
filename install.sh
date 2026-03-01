#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing pipewire-multi-output..."
echo ""

# Check dependencies
missing=()
command -v python3 >/dev/null 2>&1 || missing+=(python3)
command -v pw-cli >/dev/null 2>&1 || missing+=(pipewire)
command -v pw-loopback >/dev/null 2>&1 || missing+=(pipewire-utils)
command -v pactl >/dev/null 2>&1 || missing+=(pulseaudio-utils)
python3 -c "import gi; gi.require_version('Adw', '1')" 2>/dev/null || missing+=(libadwaita/python3-gobject)

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing dependencies: ${missing[*]}"
    echo ""
    echo "On Fedora:  sudo dnf install python3 pipewire pipewire-utils pulseaudio-utils libadwaita python3-gobject"
    echo "On Ubuntu:  sudo apt install python3 pipewire pipewire-pulse pulseaudio-utils libadwaita-1-0 python3-gi gir1.2-adw-1"
    echo "On Arch:    sudo pacman -S python pipewire pipewire-pulse libadwaita python-gobject"
    exit 1
fi

# Install .desktop file
DESKTOP_DIR="${HOME}/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
sed "s|^Exec=.*|Exec=env PYTHONPATH=${SCRIPT_DIR} python3 -m multi_output.gui|" \
    "${SCRIPT_DIR}/multi-output.desktop" > "${DESKTOP_DIR}/multi-output.desktop"
echo "  Installed .desktop file to ${DESKTOP_DIR}/multi-output.desktop"

# Install systemd user service via core.py
python3 -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); from multi_output import core; core.install_service()"
echo "  Installed systemd service to ~/.config/systemd/user/multi-output.service"

echo ""
echo "Done! You can now:"
echo "  1. Launch the app from your application menu ('Multi-Output Audio')"
echo "  2. Or run:  cd ${SCRIPT_DIR} && python3 -m multi_output.gui"
echo "  3. Toggle 'Auto-start on login' in the app to start on boot"
