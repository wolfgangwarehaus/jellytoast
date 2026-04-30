#!/bin/bash
# JellyToast — Arch Linux installer
set -e

echo "🎬 JellyToast Installer"
echo "========================"

# Detect Arch
if ! command -v pacman &> /dev/null; then
    echo "⚠ This installer is for Arch Linux. On other distros, install:"
    echo "   - Python 3.10+, Qt6, mpv, libmpv, libnotify, ffmpeg"
    echo "   - Then run: pip install -r requirements.txt"
    exit 1
fi

echo ""
echo "📦 Installing system packages..."
sudo pacman -S --needed --noconfirm \
    python python-pip \
    qt6-base qt6-svg \
    mpv \
    libnotify \
    ffmpeg \
    python-pyqt6 \
    python-requests \
    python-dbus

echo ""
echo "🐍 Installing Python packages..."
pip install --user --break-system-packages \
    PyQt6 PyQt6-Qt6 PyQt6-sip \
    python-mpv \
    pychromecast \
    zeroconf \
    dbus-next \
    requests

echo ""
echo "✅ Installation complete!"
echo ""
echo "Run with:    python3 jellytoast.py"
echo "Add to menu: bash create_desktop_entry.sh"
