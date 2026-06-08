"""
System tray icon with media controls.
"""

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from modules.player_state import NowPlaying, PlayerBus
from modules.ui_helpers import make_app_icon, opaque_menu


class TrayController(QObject):
    def __init__(self, app: QApplication, mini_player, main_window):
        super().__init__(app)
        self.app = app
        self.mini = mini_player
        self.main = main_window
        self.bus = PlayerBus.get()

        self.tray = QSystemTrayIcon(QIcon(make_app_icon(64)), app)
        self.tray.setToolTip("jellytoast")
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(lambda: self.play_action.setText("▶  Play"))
        self.bus.playback_resumed.connect(lambda: self.play_action.setText("⏸  Pause"))

    def _build_menu(self):
        self.menu = opaque_menu()
        self.menu.setStyleSheet(self._menu_qss())
        self._build_menu_actions()
        # Live-apply: re-stamp the menu on theme_changed so the color
        # editor's edits to TEXT / TEXT_FAINT / BORDER_ACCENT / accent
        # tint flow through to the system-tray context menu without a
        # restart.
        try:
            self.bus.theme_changed.connect(self._reapply_menu_styling)
        except Exception:
            pass

    def _build_menu_actions(self):
        # Every QAction must have a parent passed to its constructor; if it's
        # only held by a local variable, the Python wrapper gets garbage-
        # collected and the menu silently loses the item — even though
        # menu.addAction() supposedly reparents it. Pass self.menu as parent.
        self.now_playing = QAction("─── Nothing Playing ───", self.menu)
        self.now_playing.setEnabled(False)
        self.menu.addAction(self.now_playing)

        self.menu.addSeparator()

        self.play_action = QAction("▶  Play", self.menu)
        self.play_action.triggered.connect(lambda: self.bus.pause_toggled.emit())
        self.menu.addAction(self.play_action)

        self.prev_action = QAction("⏮  Previous", self.menu)
        self.prev_action.triggered.connect(lambda: self.bus.prev_track.emit())
        self.menu.addAction(self.prev_action)

        self.next_action = QAction("⏭  Next", self.menu)
        self.next_action.triggered.connect(lambda: self.bus.next_track.emit())
        self.menu.addAction(self.next_action)

        self.stop_action = QAction("⏹  Stop", self.menu)
        self.stop_action.triggered.connect(lambda: self.bus.stop_requested.emit())
        self.menu.addAction(self.stop_action)

        self.menu.addSeparator()

        self.mini_action = QAction("🪟  Show Mini Player", self.menu)
        self.mini_action.triggered.connect(self._toggle_mini)
        self.menu.addAction(self.mini_action)
        # Sync the Show/Hide label to the mini player's ACTUAL visibility each
        # time the menu opens — it can be hidden via its own close button,
        # which _toggle_mini's optimistic label update wouldn't catch.
        self.menu.aboutToShow.connect(self._refresh_mini_label)

        self.open_action = QAction("🎬  Open jellytoast", self.menu)
        self.open_action.triggered.connect(lambda: self.bus.open_main_window.emit())
        self.menu.addAction(self.open_action)

        self.menu.addSeparator()

        self.quit_action = QAction("✕  Quit jellytoast", self.menu)
        self.quit_action.triggered.connect(self._quit)
        self.menu.addAction(self.quit_action)

    @staticmethod
    def _menu_qss() -> str:
        """Tray context-menu QSS. Reads tokens + accent rgba live so
        the color editor / accent picker update the menu in place.
        Built per-call so theme_changed re-stamps see new values."""
        from modules import ui_helpers as _u
        from modules.theme import _hex_to_rgb

        ar, ag, ab = _hex_to_rgb(_u.ACCENT)
        return f"""
            QMenu {{
                background: {_u.POPUP_OPAQUE_FILL}; color: {_u.TEXT};
                border: 1px solid rgba({ar},{ag},{ab},0.4);
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 4px; }}
            QMenu::item:selected {{ background: rgba({ar},{ag},{ab},0.25); }}
            QMenu::item:disabled {{ color: {_u.TEXT_FAINT}; }}
            QMenu::separator {{ height: 1px; background: {_u.BORDER}; margin: 4px 8px; }}
        """

    def _reapply_menu_styling(self):
        self.menu.setStyleSheet(self._menu_qss())

    def _quit(self):
        # Tray "Quit" must close *everything* — main window, mini
        # player, and audio. Plain app.quit() exits the event loop but
        # leaves the mini player surface up on Wayland and lets mpv
        # keep playing if aboutToQuit's shutdown hook misfires. So we
        # tear things down explicitly:
        #   1. Stop audio via the bus (mpv.stop() listens here).
        #   2. Mark the main window as intentionally quitting so its
        #      minimize-to-tray closeEvent yields instead of vetoing.
        #   3. Hide the mini player so its top-level surface goes away.
        #   4. Quit — aboutToQuit still runs mpv.shutdown() as a hard
        #      backstop in case stop_requested didn't reach mpv.
        # Persist the in-flight eligible scrobble synchronously BEFORE the
        # stop. The stop's scrobble submit goes via run_async, which can't
        # finish during shutdown; flush_current_on_quit writes it straight
        # to the queue, and the subsequent stop then sees it already
        # handled (scrobbled=True) so it won't fire a doomed async submit.
        try:
            from modules.scrobble import get_scrobble_manager

            get_scrobble_manager().flush_current_on_quit()
        except Exception:
            pass
        try:
            self.bus.stop_requested.emit()
        except Exception:
            pass
        try:
            self.main._quitting = True
        except Exception:
            pass
        try:
            self.mini.hide()
        except Exception:
            pass
        self.app.quit()

    def _refresh_mini_label(self):
        self.mini_action.setText(
            "🪟  Hide Mini Player" if self.mini.isVisible() else "🪟  Show Mini Player"
        )

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
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            # KDE Plasma 6's StatusNotifierItem can intercept the standard
            # context menu and substitute a stripped-down media widget that
            # drops most of our items. Popup the QMenu manually at the
            # cursor so Qt renders it directly.
            self.menu.popup(QCursor.pos())

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        label = np.title
        if np.subtitle:
            label = f"{np.subtitle} – {np.title}"
        if len(label) > 50:
            label = label[:47] + "…"
        self.now_playing.setText(f"♪  {label}")
        self.tray.setToolTip(f"jellytoast\n{label}")
        self.play_action.setText("⏸  Pause")

    @Slot()
    def _on_stopped(self):
        self.now_playing.setText("─── Nothing Playing ───")
        self.tray.setToolTip("jellytoast")
        self.play_action.setText("▶  Play")
