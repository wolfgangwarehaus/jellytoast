#!/bin/bash
# Register jellytoast in your application menu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="$HOME/.local/share/applications/jellytoast.desktop"
ICON_BASE="$HOME/.local/share/icons/hicolor"

mkdir -p "$HOME/.local/share/applications"
for sz in 16 24 32 48 64 128 256 512; do
    mkdir -p "$ICON_BASE/${sz}x${sz}/apps"
done

# Generate the icon at every standard hicolor size — the launcher, panel,
# alt-tab switcher, and KDE's tray pick whichever resolution fits best.
# Stale icons at unused sizes will out-rank the freshly written one.
python3 - << EOF
import os
import sys
sys.path.insert(0, "$SCRIPT_DIR")
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
app = QApplication([])
from modules.ui_helpers import make_app_icon
for sz in (16, 24, 32, 48, 64, 128, 256, 512):
    make_app_icon(sz).save("$ICON_BASE/{0}x{0}/apps/jellytoast.png".format(sz))
EOF

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=jellytoast
GenericName=Jellyfin Media Player
Comment=Audio-first Jellyfin desktop client with mini player and casting
Exec=$SCRIPT_DIR/run.sh
Path=$SCRIPT_DIR
Icon=jellytoast
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Video;Player;
Keywords=jellyfin;music;media;player;chromecast;airplay;
StartupNotify=true
# StartupWMClass intentionally omitted: KDE uses it to auto-associate a
# running window with the bounce sequence, which stops the bounce the
# moment our (initially invisible, opacity-0) loading window maps —
# well before the home destination is ready. We rely on Qt's
# setDesktopFileName("jellytoast") setting _KDE_NET_WM_DESKTOP_FILE
# instead, and call _send_startup_notification_remove() (in
# jellytoast.py) once the first content is shown so the bounce
# lasts the entire load.
MimeType=audio/mpeg;audio/flac;audio/ogg;audio/x-vorbis+ogg;
EOF

echo "✅ Desktop entry: $DESKTOP_FILE"
echo "✅ Icons: $ICON_BASE/{16,24,32,48,64,128,256,512}x*/apps/jellytoast.png"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "jellytoast should now appear in your app launcher."
