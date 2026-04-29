"""
Desktop notifications for track changes.
Uses org.freedesktop.Notifications via D-Bus when possible,
falling back to QSystemTrayIcon balloon messages.
"""

import os
import tempfile
import threading
import requests
from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtWidgets import QSystemTrayIcon

from modules.player_state import PlayerBus, NowPlaying


class NotificationService(QObject):
    def __init__(self, tray: QSystemTrayIcon = None, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self._tray = tray
        self._art_cache_dir = tempfile.mkdtemp(prefix="jellyplayer-art-")
        self.bus.notify_track.connect(self._on_track)

    def set_tray(self, tray: QSystemTrayIcon):
        self._tray = tray

    @pyqtSlot(object)
    def _on_track(self, np: NowPlaying):
        threading.Thread(target=self._send, args=(np,), daemon=True).start()

    def _send(self, np: NowPlaying):
        title = np.title
        body_lines = []
        if np.subtitle:
            body_lines.append(np.subtitle)
        if np.album:
            body_lines.append(f"from {np.album}")
        body = "\n".join(body_lines)

        # Cache album art locally for the notifier
        icon_path = self._cache_artwork(np)

        # Try notify-send (most reliable)
        if self._notify_via_command(title, body, icon_path):
            return
        # Fall back to tray balloon
        if self._tray:
            self._tray.showMessage(
                title, body, QSystemTrayIcon.MessageIcon.Information, 4000
            )

    def _notify_via_command(self, title: str, body: str, icon: str) -> bool:
        try:
            import subprocess
            args = ["notify-send", "-a", "JellyPlayer", "-t", "4000"]
            if icon:
                args += ["-i", icon]
            args += [title, body]
            subprocess.run(args, check=False, timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def _cache_artwork(self, np: NowPlaying) -> str:
        if not np.thumb_url:
            return ""
        cache_path = os.path.join(self._art_cache_dir, f"{np.item_id}.jpg")
        if os.path.exists(cache_path):
            return cache_path
        try:
            r = requests.get(np.thumb_url, timeout=5)
            with open(cache_path, "wb") as f:
                f.write(r.content)
            return cache_path
        except Exception:
            return ""
