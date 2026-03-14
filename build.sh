#!/bin/bash
set -e

echo "=== BSB Control — AppImage Builder ==="
echo ""

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

# Create venv if not exists
if [ ! -d ".venv" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "[2/4] Installing dependencies..."
pip install -q PyQt6 python-osc pyinstaller

echo "[3/4] Building with PyInstaller..."
pyinstaller \
    --onefile \
    --name bsb-control \
    --windowed \
    --hidden-import=PyQt6.sip \
    --add-data "requirements.txt:." \
    bsb_control.py

echo "[4/4] Done!"
echo ""
echo "Binary: dist/bsb-control"
echo ""
echo "To create a desktop entry:"
echo ""
cat << 'DESKTOP'
# Save as ~/.local/share/applications/bsb-control.desktop
[Desktop Entry]
Type=Application
Name=BSB Control
Comment=Bigscreen Beyond brightness control with VRChat OSC
Exec=/path/to/bsb-control
Icon=preferences-desktop-display
Categories=Utility;
StartupNotify=false
DESKTOP
echo ""
echo "To auto-start, symlink to ~/.config/autostart/"
