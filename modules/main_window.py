"""
Main application window.

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │ [Sidebar] │            [Top bar: search]                    │
  │           ├─────────────────────────────────────────────────┤
  │  Home     │                                                 │
  │  Music    │            [Page stack]                  [Queue]│
  │  Movies   │            • Home                               │
  │  TV       │            • Music                              │
  │  Search   │            • Movies / TV                        │
  │           │            • Album / Artist detail              │
  │  Cast     │            • Now Playing detail                 │
  │  Mini     │            • Video player (mpv embed)           │
  ├─────────────────────────────────────────────────────────────┤
  │  [ Now Playing bar — transport, progress, volume, cast ]    │
  └─────────────────────────────────────────────────────────────┘
"""

from typing import Optional
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QStackedWidget, QSplitter, QMessageBox, QDialog, QMenu,
    QApplication,
)

from modules.jellyfin_api import get_api
from modules.cast_manager import CastManager
from modules.player_state import PlayerBus, get_now_playing
from modules.player_backend import MpvVideoWidget, MpvController
from modules.queue_manager import QueueManager
from modules.settings import get_settings
from modules.ui_helpers import (
    GLOBAL_STYLE, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM, TEXT_FAINT, BORDER,
    make_app_icon,
)
from modules.library_views import HomeView, MusicView, TypedGridView
from modules.detail_views import (
    AlbumDetailView, ArtistDetailView, QueuePanel, NowPlayingView,
)
from modules.now_playing_bar import NowPlayingBar, CastDialog
from modules.login_dialog import LoginDialog


PAGE_HOME = 0
PAGE_MUSIC = 1
PAGE_MOVIES = 2
PAGE_TV = 3
PAGE_SEARCH = 4
PAGE_ALBUM = 5
PAGE_ARTIST = 6
PAGE_NOW_PLAYING = 7
PAGE_VIDEO = 8


class MainWindow(QMainWindow):
    def __init__(self, mpv_controller: MpvController):
        super().__init__()
        self.api = get_api()
        self.bus = PlayerBus.get()
        self.settings = get_settings()
        self.cast_manager = CastManager()
        self.mpv = mpv_controller
        self.queue_mgr = QueueManager(self)

        self.setWindowTitle("JellyPlayer")
        self.setWindowIcon(QIcon(make_app_icon(64)))
        self.setMinimumSize(1180, 760)
        self.setStyleSheet(GLOBAL_STYLE)

        self._build_ui()
        self._connect_signals()
        self._setup_shortcuts()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Top section: sidebar | content | queue
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        body_layout.addWidget(self._build_sidebar())
        body_layout.addWidget(self._build_content_area(), 1)

        # Queue panel (initially hidden)
        self.queue_panel = QueuePanel()
        self.queue_panel.hide()
        body_layout.addWidget(self.queue_panel)

        root.addWidget(body, 1)

        # Bottom: now playing bar
        self.np_bar = NowPlayingBar()
        self.np_bar.show_now_playing_requested.connect(self._show_now_playing)
        self.np_bar.show_queue_requested.connect(self._toggle_queue)
        self.np_bar.cast_requested.connect(self._open_cast_dialog)
        root.addWidget(self.np_bar)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"""
            QWidget {{
                background: rgba(255,255,255,0.02);
                border-right: 1px solid {BORDER};
            }}
        """)
        v = QVBoxLayout(sidebar)
        v.setContentsMargins(14, 22, 14, 16)
        v.setSpacing(2)

        # Logo
        logo = QLabel("◆ JellyPlayer")
        logo.setStyleSheet(f"color: {ACCENT}; font-size: 17px; font-weight: 800; padding: 4px 8px 16px 8px;")
        v.addWidget(logo)

        # Nav items
        nav = [
            ("⌂", "Home", PAGE_HOME),
            ("♪", "Music", PAGE_MUSIC),
            ("🎬", "Movies", PAGE_MOVIES),
            ("📺", "TV Shows", PAGE_TV),
            ("⌕", "Search", PAGE_SEARCH),
        ]
        self._nav_buttons = {}
        for icon, label, page_id in nav:
            btn = self._make_nav_button(icon, label)
            btn.clicked.connect(lambda _, p=page_id, l=label: self._goto_page(p, l))
            v.addWidget(btn)
            self._nav_buttons[page_id] = btn

        v.addStretch()

        # Cast / mini player buttons
        self.cast_button = self._make_nav_button("📡", "Cast")
        self.cast_button.clicked.connect(self._open_cast_dialog)
        v.addWidget(self.cast_button)

        self.mini_button = self._make_nav_button("🪟", "Mini Player")
        self.mini_button.clicked.connect(lambda: self.bus.show_mini_player.emit())
        v.addWidget(self.mini_button)

        # Server info / disconnect
        v.addSpacing(8)

        self.server_label = QLabel("")
        self.server_label.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 10px; padding: 0 8px;")
        self.server_label.setWordWrap(True)
        v.addWidget(self.server_label)

        logout_btn = QPushButton("Sign out")
        logout_btn.setObjectName("ghost")
        logout_btn.setStyleSheet(f"""
            QPushButton {{ color: {TEXT_FAINT}; font-size: 11px; padding: 6px 8px;
                            text-align: left; background: transparent; border: none; }}
            QPushButton:hover {{ color: #f87171; }}
        """)
        logout_btn.clicked.connect(self._logout)
        v.addWidget(logout_btn)

        return sidebar

    def _make_nav_button(self, icon: str, label: str) -> QPushButton:
        btn = QPushButton(f"  {icon}    {label}")
        btn.setCheckable(True)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM}; border: none;
                border-radius: 8px; padding: 9px 12px; text-align: left;
                font-size: 13px; font-weight: 500;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.05); color: {TEXT}; }}
            QPushButton:checked {{
                background: rgba(167,139,250,0.18); color: {ACCENT};
            }}
        """)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _build_content_area(self) -> QWidget:
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Top bar
        topbar = QWidget()
        topbar.setFixedHeight(64)
        topbar.setStyleSheet(f"background: transparent; border-bottom: 1px solid {BORDER};")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(28, 0, 28, 0)
        tb.setSpacing(16)

        self.page_title = QLabel("Home")
        self.page_title.setStyleSheet(f"color: {TEXT}; font-size: 22px; font-weight: 800;")
        tb.addWidget(self.page_title)
        tb.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("⌕  Search artists, albums, songs, movies…")
        self.search_input.setFixedWidth(340)
        self.search_input.setMinimumHeight(36)
        self.search_input.returnPressed.connect(self._do_search)
        tb.addWidget(self.search_input)

        content_layout.addWidget(topbar)

        # Page stack
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")

        # Build pages
        self.home_view = HomeView()
        self.music_view = MusicView()
        self.movies_view = TypedGridView("Movie")
        self.tv_view = TypedGridView("Series")
        self.search_view = TypedGridView("")  # search uses its own loader
        self.album_view = AlbumDetailView()
        self.artist_view = ArtistDetailView()
        self.now_playing_view = NowPlayingView()

        # Video page (mpv embedded)
        self.video_widget = MpvVideoWidget(self.mpv)

        # Wire all view signals
        for v in [self.home_view, self.music_view, self.movies_view, self.tv_view,
                  self.search_view, self.artist_view]:
            if hasattr(v, "item_clicked"):
                v.item_clicked.connect(self._on_item_clicked)
            if hasattr(v, "item_play_requested"):
                v.item_play_requested.connect(self._on_item_play)
            if hasattr(v, "item_context_requested"):
                v.item_context_requested.connect(self._on_item_context_menu)

        # Add to stack with explicit indices
        self.stack.addWidget(self.home_view)         # 0
        self.stack.addWidget(self.music_view)        # 1
        self.stack.addWidget(self.movies_view)       # 2
        self.stack.addWidget(self.tv_view)           # 3
        self.stack.addWidget(self.search_view)       # 4
        self.stack.addWidget(self.album_view)        # 5
        self.stack.addWidget(self.artist_view)       # 6
        self.stack.addWidget(self.now_playing_view)  # 7
        self.stack.addWidget(self.video_widget)      # 8

        # Padded container
        padded = QWidget()
        pad_layout = QVBoxLayout(padded)
        pad_layout.setContentsMargins(28, 18, 28, 4)
        pad_layout.addWidget(self.stack)

        content_layout.addWidget(padded, 1)

        return content

    # ── Signals ─────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.bus.open_main_window.connect(self._show_self)
        self.bus.playback_started.connect(self._on_playback_started)
        self.bus.playback_stopped.connect(self._on_playback_stopped)
        self.bus.navigate_to_item.connect(self._on_item_clicked)
        self.bus.show_now_playing.connect(self._show_now_playing)

    def _setup_shortcuts(self):
        # Spacebar = play/pause (when not in a text field)
        self._sc_space = QShortcut(QKeySequence("Space"), self)
        self._sc_space.activated.connect(self._space_pressed)

        QShortcut(QKeySequence("Ctrl+Right"), self,
                   activated=lambda: self.bus.next_track.emit())
        QShortcut(QKeySequence("Ctrl+Left"), self,
                   activated=lambda: self.bus.prev_track.emit())
        QShortcut(QKeySequence("Right"), self,
                   activated=lambda: self.bus.seek_relative.emit(10_000))
        QShortcut(QKeySequence("Left"), self,
                   activated=lambda: self.bus.seek_relative.emit(-10_000))
        QShortcut(QKeySequence("Ctrl+F"), self,
                   activated=lambda: self.search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+Q"), self,
                   activated=QApplication.instance().quit)

    def _space_pressed(self):
        # Don't interfere with text fields
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return
        self.bus.pause_toggled.emit()

    # ── Authentication & startup ────────────────────────────────────────────

    def show_login_if_needed(self) -> bool:
        """Show login dialog if not authenticated. Returns True if authenticated."""
        if self.api.is_authenticated and self.api.verify_session():
            self._on_authenticated()
            return True

        dlg = LoginDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._on_authenticated()
            return True
        return False

    def _on_authenticated(self):
        # Update server label
        url = self.api.server_url
        username = self.settings.username
        self.server_label.setText(f"Signed in as {username}\n{url}")

        # Default to Home
        self._goto_page(PAGE_HOME, "Home")

        # Discover cast devices in background
        self.cast_manager.discover_all()

    def _logout(self):
        ret = QMessageBox.question(
            self, "Sign out",
            "Are you sure you want to sign out? The current queue will be cleared.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.bus.stop_requested.emit()
        self.bus.queue_clear.emit()
        self.api.logout()
        if self.show_login_if_needed():
            self._goto_page(PAGE_HOME, "Home")

    # ── Navigation ──────────────────────────────────────────────────────────

    def _goto_page(self, page_id: int, label: str):
        # Update sidebar checked state for top-level pages
        for pid, btn in self._nav_buttons.items():
            btn.setChecked(pid == page_id)

        self.stack.setCurrentIndex(page_id)
        self.page_title.setText(label)

        # Lazy-load
        if page_id == PAGE_HOME:
            self.home_view.load()
        elif page_id == PAGE_MUSIC:
            self.music_view.load()
        elif page_id == PAGE_MOVIES:
            self.movies_view.load()
        elif page_id == PAGE_TV:
            self.tv_view.load()
        elif page_id == PAGE_SEARCH:
            self.search_input.setFocus()

    def _do_search(self):
        term = self.search_input.text().strip()
        if not term:
            return
        for btn in self._nav_buttons.values():
            btn.setChecked(False)
        self._nav_buttons[PAGE_SEARCH].setChecked(True)
        self.stack.setCurrentIndex(PAGE_SEARCH)
        self.page_title.setText(f'Search: "{term}"')
        try:
            results = self.api.search(term)
            self.search_view.grid.set_items(results)
        except Exception as e:
            print(f"Search error: {e}")

    # ── Item interactions ───────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _on_item_clicked(self, item: dict):
        item_type = item.get("Type", "")
        if item_type == "MusicAlbum":
            self.album_view.load_album(item)
            self.stack.setCurrentIndex(PAGE_ALBUM)
            self.page_title.setText(item.get("Name", "Album"))
        elif item_type == "MusicArtist":
            self.artist_view.load_artist(item)
            self.stack.setCurrentIndex(PAGE_ARTIST)
            self.page_title.setText(item.get("Name", "Artist"))
        elif item_type in ("Movie", "Episode", "Audio"):
            self._on_item_play(item)
        elif item_type == "Series":
            # TODO: Series detail view; for now just play first episode
            try:
                seasons = self.api.get_seasons(item.get("Id", ""))
                if seasons:
                    eps = self.api.get_episodes(item.get("Id", ""), seasons[0].get("Id", ""))
                    if eps:
                        self._on_item_play(eps[0])
            except Exception:
                pass

    @pyqtSlot(dict)
    def _on_item_play(self, item: dict):
        # For an album/artist, build a queue from their tracks
        item_type = item.get("Type", "")
        if item_type == "MusicAlbum":
            try:
                tracks = self.api.get_album_tracks(item.get("Id", ""))
                if tracks:
                    self.bus.queue_play_now.emit(tracks, 0)
            except Exception:
                pass
        elif item_type == "MusicArtist":
            try:
                albums = self.api.get_artist_albums(item.get("Id", ""))
                tracks = []
                for alb in albums:
                    tracks.extend(self.api.get_album_tracks(alb.get("Id", "")))
                if tracks:
                    self.bus.queue_play_now.emit(tracks, 0)
            except Exception:
                pass
        else:
            self.bus.queue_play_now.emit([item], 0)

    @pyqtSlot(dict, "QPoint")
    def _on_item_context_menu(self, item: dict, global_pos):
        menu = QMenu(self)
        item_type = item.get("Type", "")

        if item_type == "MusicAlbum":
            menu.addAction("Play album", lambda: self._on_item_play(item))
            menu.addAction("Play next",
                           lambda: self._add_album_to_queue(item, "next"))
            menu.addAction("Add to queue",
                           lambda: self._add_album_to_queue(item, "end"))
            menu.addSeparator()
            menu.addAction("Open album", lambda: self._on_item_clicked(item))
        elif item_type == "MusicArtist":
            menu.addAction("Play all", lambda: self._on_item_play(item))
            menu.addAction("Open artist", lambda: self._on_item_clicked(item))
        elif item_type in ("Movie", "Episode", "Audio"):
            menu.addAction("Play", lambda: self._on_item_play(item))
            menu.addAction("Play next",
                           lambda: self.bus.queue_add_next.emit([item]))
            menu.addAction("Add to queue",
                           lambda: self.bus.queue_add_end.emit([item]))

        menu.exec(global_pos)

    def _add_album_to_queue(self, album: dict, where: str):
        try:
            tracks = self.api.get_album_tracks(album.get("Id", ""))
            if not tracks:
                return
            if where == "next":
                self.bus.queue_add_next.emit(tracks)
            else:
                self.bus.queue_add_end.emit(tracks)
        except Exception:
            pass

    # ── Playback events ─────────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_playback_started(self, np):
        # If video, switch to video page
        if np.is_video:
            self.stack.setCurrentIndex(PAGE_VIDEO)
            self.page_title.setText(np.title)
        # Notification
        self.bus.notify_track.emit(np)

    @pyqtSlot()
    def _on_playback_stopped(self):
        # If we're on the video page, go back home
        if self.stack.currentIndex() == PAGE_VIDEO:
            self._goto_page(PAGE_HOME, "Home")

    # ── Queue / Now Playing / Cast ─────────────────────────────────────────

    def _toggle_queue(self):
        self.queue_panel.setVisible(not self.queue_panel.isVisible())
        self.np_bar.queue_btn.setChecked(self.queue_panel.isVisible())

    def _show_now_playing(self):
        np = get_now_playing()
        if not np.item_id:
            return
        if np.is_audio:
            self.stack.setCurrentIndex(PAGE_NOW_PLAYING)
            self.page_title.setText("Now Playing")
        else:
            self.stack.setCurrentIndex(PAGE_VIDEO)

    def _open_cast_dialog(self):
        np = get_now_playing()
        if not np.item_id:
            QMessageBox.information(
                self, "Cast",
                "Start playing something first, then choose a device to cast to."
            )
            return

        dlg = CastDialog(self.cast_manager, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_device:
            dev = dlg.selected_device
            success = False
            if dev.device_type == "chromecast":
                success = self.cast_manager.cast_to_chromecast(
                    dev, np.stream_url, np.title, np.thumb_url, is_audio=np.is_audio,
                )
            else:
                success = self.cast_manager.cast_to_airplay(dev, np.stream_url, np.title)

            if success:
                self.bus.cast_started.emit(dev.name)
                self.bus.stop_requested.emit()  # stop local playback
                QMessageBox.information(
                    self, "Casting",
                    f"Now casting to {dev.name}.\n\nLocal playback has stopped.",
                )
            else:
                QMessageBox.warning(
                    self, "Cast failed",
                    f"Could not cast to {dev.name}.\n\n"
                    "Make sure the device is on the same network and reachable."
                )

    # ── Window behaviour ────────────────────────────────────────────────────

    @pyqtSlot()
    def _show_self(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        # Hide to tray instead of quitting
        if self.settings.minimize_to_tray:
            self.hide()
            e.ignore()
        else:
            QApplication.instance().quit()
