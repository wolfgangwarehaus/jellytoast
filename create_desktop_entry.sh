#!/bin/bash
# Register JellyToast in your application menu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="$HOME/.local/share/applications/jellytoast.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

mkdir -p "$HOME/.local/share/applications" "$ICON_DIR"

# Generate icon (run app once, save its rendered logo)
python3 - << EOF
import os
import sys
sys.path.insert(0, "$SCRIPT_DIR")
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from modules.ui_helpers import make_app_icon
icon = make_app_icon(256)
icon.save("$ICON_DIR/jellytoast.png")
EOF

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=JellyToast
GenericName=Jellyfin Media Player
Comment=Audio-first Jellyfin desktop client with mini player and casting
Exec=python3 "$SCRIPT_DIR/jellytoast.py"
Path=$SCRIPT_DIR
Icon=jellytoast
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Video;Player;
Keywords=jellyfin;music;media;player;chromecast;airplay;
StartupNotify=true
StartupWMClass=JellyToast
MimeType=audio/mpeg;audio/flac;audio/ogg;audio/x-vorbis+ogg;
EOF

echo "✅ Desktop entry: $DESKTOP_FILE"
echo "✅ Icon: $ICON_DIR/jellytoast.png"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "JellyToast should now appear in your app launcher."
