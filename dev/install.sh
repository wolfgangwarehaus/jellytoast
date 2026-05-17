#!/bin/bash
# jellytoast — DEV install helper (Arch Linux).
#
# This is the manual git-clone install path. For end-user installs
# prefer the AUR package (`jellytoast`) or the Flathub Flatpak — both
# handle their own deps without --break-system-packages.
#
# This script installs runtime deps system-wide via pacman where
# available, then falls back to `pip --break-system-packages` for the
# few bindings that aren't in the Arch repos.
set -e

echo "jellytoast — dev install"
echo "========================="

if ! command -v pacman &> /dev/null; then
    echo "This dev installer targets Arch Linux. On other distros install"
    echo "Python 3.10+, Qt6, mpv, libmpv, libnotify, ffmpeg via your"
    echo "package manager, then: pip install -e . (from the repo root)."
    exit 1
fi

echo ""
echo "Installing system packages..."
sudo pacman -S --needed --noconfirm \
    python python-pip \
    qt6-base qt6-svg \
    mpv \
    libnotify \
    ffmpeg \
    pyside6 \
    python-requests \
    python-keyring \
    python-cryptography

echo ""
echo "Installing Python packages (mpv/cast/MPRIS bindings not in repos)..."
pip install --user --break-system-packages \
    python-mpv \
    pychromecast \
    zeroconf \
    dbus-next \
    pyatv

echo ""
echo "Done. Run with:"
echo "  python3 jellytoast.py             (from repo root)"
echo "  bash dev/run.sh                   (with libmpv env vars set)"
echo "  bash dev/create_desktop_entry.sh  (register in app menu)"
