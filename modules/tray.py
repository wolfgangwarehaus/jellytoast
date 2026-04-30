"""
System tray icon with media controls.
"""

from PyQt6.QtCore import Qt, QObject, pyqtSlot
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication

from modules.player_state import PlayerBus, get_now_playing, NowPlaying
from modules.ui_helpers import make_app_icon, ACCENT


class TrayController(QObject):
    def __init__(self, app: QApplication, mini_player, main_window):
        super().__init__(app)
        self.app = app
        self.mini = mini_player
        self.main = main_window
        self.bus = PlayerBus.get()

        self.tray = QSystemTrayIcon(QIcon(make_app_icon(64)), app)
        self.tray.setToolTip("JellyToast")
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(lambda: self.play_action.setText("▶  Play"))
        self.bus.playback_resumed.connect(lambda: self.play_action.setText("⏸  Pause"))

    def _build_menu(self):
        self.menu = QMenu()
        self.menu.setStyleSheet("""
            QMenu {
                background: #12121f; color: #e2e8f0;
                border: 1px solid rgba(167,139,250,0.4); border-radius: 8px;
                padding: 4px;
            }
            QMenu::item { padding: 7px 22px 7px 14px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(167,139,250,0.25); }
            QMenu::item:disabled { color: rgba(255,255,255,0.4); }
            QMenu::separator { height: 1px; background: rgba(255,255,255,0.08); margin: 4px 8px; }
        """)

        self.now_playing = QAction("─── Nothing Playing ───")
        self.now_playing.setEnabled(False)
        self.menu.addAction(self.now_playing)

        self.menu.addSeparator()

        self.play_action = QAction("▶  Play")
        self.play_action.triggered.connect(lambda: self.bus.pause_toggled.emit())
        self.menu.addAction(self.play_action)

        prev_action = QAction("⏮  Previous")
        prev_action.triggered.connect(lambda: self.bus.prev_track.emit())
        self.menu.addAction(prev_action)

        next_action = QAction("⏭  Next")
        next_action.triggered.connect(lambda: self.bus.next_track.emit())
        self.menu.addAction(next_action)

        stop_action = QAction("⏹  Stop")
        stop_action.triggered.connect(lambda: self.bus.stop_requested.emit())
        self.menu.addAction(stop_action)

        self.menu.addSeparator()

        self.mini_action = QAction("🪟  Show Mini Player")
        self.mini_action.triggered.connect(self._toggle_mini)
        self.menu.addAction(self.mini_action)

        open_action = QAction("🎬  Open JellyToast")
        open_action.triggered.connect(lambda: self.bus.open_main_window.emit())
        self.menu.addAction(open_action)

        self.menu.addSeparator()

        quit_action = QAction("✕  Quit")
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)

    def _toggle_mini(self):
        if self.mini.isVisible():
            self.mini.hide()
            self.mini_action.setText("🪟  Show Mini Player")
        else:
            self.mini.show()
            self.mini_action.setText("🪟  Hide Mini Player")

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_mini()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.bus.open_main_window.emit()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.bus.pause_toggled.emit()

    @pyqtSlot(object)
    def _on_started(self, np: NowPlaying):
        label = np.title
        if np.subtitle:
            label = f"{np.subtitle} – {np.title}"
        if len(label) > 50:
            label = label[:47] + "…"
        self.now_playing.setText(f"♪  {label}")
        self.tray.setToolTip(f"JellyToast\n{label}")
        self.play_action.setText("⏸  Pause")

    @pyqtSlot()
    def _on_stopped(self):
        self.now_playing.setText("─── Nothing Playing ───")
        self.tray.setToolTip("JellyToast")
        self.play_action.setText("▶  Play")
