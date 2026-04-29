"""
JellyPlayer — Jellyfin desktop client for Arch Linux.

Audio-first design with full video support.
- Bit-perfect audio via mpv
- MPRIS2 D-Bus integration (media keys, KDE/GNOME widgets)
- Floating mini player + system tray
- Chromecast + AirPlay v1 casting
- Persistent queue, shuffle/repeat
- Desktop notifications on track changes
"""

import os
import sys
import signal
from pathlib import Path

# Make this script importable from anywhere (handles desktop launches,
# arbitrary CWDs, and paths with spaces).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# Sanity check: modules/ must exist next to main.py
if not (_SCRIPT_DIR / "modules" / "__init__.py").exists():
    sys.stderr.write(
        f"❌ Cannot find 'modules/' next to main.py\n"
        f"   Expected: {_SCRIPT_DIR / 'modules'}\n"
        f"   Make sure all files were extracted into the project directory.\n"
    )
    sys.exit(1)

# Set Qt platform-specific environment before any Qt import
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox

from modules.player_state import PlayerBus
from modules.player_backend import MpvController, MPV_AVAILABLE
from modules.main_window import MainWindow
from modules.mini_player import FloatingMiniPlayer
from modules.tray import TrayController
from modules.notifications import NotificationService
from modules.mpris import MprisService
from modules.settings import get_settings
from modules.ui_helpers import make_app_icon


def main():
    # Allow Ctrl-C to terminate
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # High-DPI handling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("JellyPlayer")
    app.setApplicationDisplayName("JellyPlayer")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("JellyPlayer")
    app.setDesktopFileName("jellyplayer")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)  # keep alive in tray

    # Verify mpv
    if not MPV_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyPlayer requires libmpv to play audio and video.\n\n"
            "Install with:\n    sudo pacman -S mpv\n\n"
            "Then restart JellyPlayer."
        )
        sys.exit(1)

    # System tray check
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None, "No system tray",
            "Your desktop doesn't appear to have a system tray.\n"
            "JellyPlayer will run, but tray features will be unavailable."
        )

    # Singletons
    bus = PlayerBus.get()
    settings = get_settings()

    # mpv backend (created BEFORE main window since main window's video widget
    # needs the controller reference)
    mpv_ctrl = MpvController()
    # Apply saved volume on startup
    bus.volume_changed.emit(settings.volume)

    # Main window
    window = MainWindow(mpv_ctrl)

    # Mini player
    mini = FloatingMiniPlayer()
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)

    # Tray
    tray = TrayController(app, mini, window)

    # Desktop notifications
    notifier = NotificationService(tray.tray)

    # MPRIS2 D-Bus (runs on separate thread)
    mpris = MprisService()
    mpris.start()

    # Try to authenticate from saved credentials
    if not window.show_login_if_needed():
        sys.exit(0)

    window.show()

    if settings.show_mini_on_start:
        mini.show()

    # Hint: tray balloon on first launch (some systems hide tray icons)
    QTimer.singleShot(500, lambda: tray.tray.showMessage(
        "JellyPlayer is running",
        "Find me in your system tray. Click the icon for quick controls.",
        QSystemTrayIcon.MessageIcon.Information, 4000,
    ))

    # Cleanup on quit
    def _cleanup():
        try:
            mpv_ctrl.shutdown()
        except Exception:
            pass
        try:
            mpris.stop()
        except Exception:
            pass
        try:
            window.cast_manager.cleanup()
        except Exception:
            pass

    app.aboutToQuit.connect(_cleanup)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
