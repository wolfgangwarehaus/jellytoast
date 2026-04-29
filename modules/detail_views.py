"""
Detail views: Album (track list with play buttons), Artist (their albums),
Queue panel, Now Playing detail (large artwork + lyrics).
"""

import threading
from typing import List, Dict, Optional
from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QPixmap, QFont, QImage, QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QListWidget, QListWidgetItem, QMenu, QSizePolicy,
)

from modules.jellyfin_api import get_api
from modules.player_state import PlayerBus, get_now_playing, NowPlaying
from modules.ui_helpers import (
    load_image_async, fmt_duration_ticks, fmt_time,
    ACCENT, TEXT, TEXT_DIM, TEXT_FAINT, BORDER, BG_PANEL,
)


# ── Album detail ─────────────────────────────────────────────────────────────

class AlbumDetailView(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self.bus = PlayerBus.get()
        self._album: Dict = {}
        self._tracks: List[Dict] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        self._inner_layout = QVBoxLayout(inner)
        self._inner_layout.setContentsMargins(28, 24, 28, 24)
        self._inner_layout.setSpacing(20)

        # Header
        self._header = QWidget()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(24)

        self.cover = QLabel()
        self.cover.setFixedSize(220, 220)
        self.cover.setStyleSheet("border-radius: 12px; background: rgba(255,255,255,0.04);")
        h.addWidget(self.cover)

        info = QVBoxLayout()
        info.setSpacing(8)

        self.kind_label = QLabel("ALBUM")
        self.kind_label.setStyleSheet(f"color: {ACCENT}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;")

        self.title_label = QLabel("")
        self.title_label.setStyleSheet(f"color: {TEXT}; font-size: 32px; font-weight: 800;")
        self.title_label.setWordWrap(True)

        self.artist_label = QLabel("")
        self.artist_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 15px;")

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 12px;")

        # Action buttons
        actions = QHBoxLayout()
        actions.setSpacing(10)

        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("accent")
        self.play_btn.setMinimumWidth(120)
        self.play_btn.clicked.connect(self._play_album)

        self.shuffle_btn = QPushButton("⤮  Shuffle")
        self.shuffle_btn.clicked.connect(self._shuffle_play)

        self.queue_btn = QPushButton("＋  Queue")
        self.queue_btn.clicked.connect(self._add_to_queue)

        actions.addWidget(self.play_btn)
        actions.addWidget(self.shuffle_btn)
        actions.addWidget(self.queue_btn)
        actions.addStretch()

        info.addWidget(self.kind_label)
        info.addWidget(self.title_label)
        info.addWidget(self.artist_label)
        info.addWidget(self.meta_label)
        info.addStretch()
        info.addLayout(actions)

        h.addLayout(info, 1)

        self._inner_layout.addWidget(self._header)

        # Track list
        self.track_list = QListWidget()
        self.track_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{
                padding: 0; margin: 0; border-bottom: 1px solid {BORDER};
            }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.04); }}
            QListWidget::item:selected {{ background: rgba(167,139,250,0.15); }}
        """)
        self.track_list.itemDoubleClicked.connect(self._on_track_double)
        self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_list.customContextMenuRequested.connect(self._track_context_menu)
        self._inner_layout.addWidget(self.track_list)

        self.scroll.setWidget(inner)
        v.addWidget(self.scroll)

    def load_album(self, album: Dict):
        self._album = album
        self.title_label.setText(album.get("Name", "Unknown"))
        self.artist_label.setText(album.get("AlbumArtist", ""))

        # Cover
        cover_url = self.api.get_image_url(album.get("Id", ""), "Primary", 440)
        load_image_async(f"{album.get('Id')}|albumcover", cover_url, 220, 220,
                          self.cover.setPixmap, rounded_radius=12)

        # Tracks
        try:
            self._tracks = self.api.get_album_tracks(album.get("Id", ""))
        except Exception as e:
            print(f"Album tracks: {e}")
            self._tracks = []

        # Meta
        total_ticks = sum(t.get("RunTimeTicks", 0) for t in self._tracks)
        year = album.get("ProductionYear", "")
        meta = f"{len(self._tracks)} tracks · {fmt_duration_ticks(total_ticks)}"
        if year:
            meta = f"{year} · " + meta
        self.meta_label.setText(meta)

        # Populate list
        self.track_list.clear()
        for i, t in enumerate(self._tracks, 1):
            item = QListWidgetItem()
            row = _TrackRow(i, t, self._play_from_index)
            item.setSizeHint(QSize(0, 44))
            item.setData(Qt.ItemDataRole.UserRole, t)
            self.track_list.addItem(item)
            self.track_list.setItemWidget(item, row)

    # ── Actions ─────────────────────────────────────────────────────────────

    def _play_album(self):
        if self._tracks:
            self.bus.queue_play_now.emit(self._tracks, 0)

    def _shuffle_play(self):
        import random
        if not self._tracks:
            return
        shuffled = list(self._tracks)
        random.shuffle(shuffled)
        self.bus.shuffle_changed.emit(True)
        self.bus.queue_play_now.emit(shuffled, 0)

    def _add_to_queue(self):
        if self._tracks:
            self.bus.queue_add_end.emit(self._tracks)

    def _play_from_index(self, idx: int):
        if 0 <= idx < len(self._tracks):
            self.bus.queue_play_now.emit(self._tracks, idx)

    def _on_track_double(self, lwi: QListWidgetItem):
        idx = self.track_list.row(lwi)
        self._play_from_index(idx)

    def _track_context_menu(self, pos):
        lwi = self.track_list.itemAt(pos)
        if not lwi:
            return
        track = lwi.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Play",
                       lambda: self._play_from_index(self.track_list.row(lwi)))
        menu.addAction("Play next",
                       lambda: self.bus.queue_add_next.emit([track]))
        menu.addAction("Add to queue",
                       lambda: self.bus.queue_add_end.emit([track]))
        menu.exec(self.track_list.mapToGlobal(pos))


class _TrackRow(QWidget):
    """A single track row inside an album view."""
    def __init__(self, index: int, track: Dict, play_callback, parent=None):
        super().__init__(parent)
        self.track = track
        self._idx = index - 1
        self._play_callback = play_callback

        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 16, 8)
        h.setSpacing(14)

        self.num_label = QLabel(f"{index}")
        self.num_label.setFixedWidth(28)
        self.num_label.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 13px;")
        self.num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(28, 28)
        self.play_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT};
                           border: none; font-size: 12px; }}
            QPushButton:hover {{ color: {ACCENT}; }}
        """)
        self.play_btn.hide()
        self.play_btn.clicked.connect(lambda: self._play_callback(self._idx))

        # Stack num/play
        num_container = QWidget()
        num_container.setFixedWidth(28)
        nc = QHBoxLayout(num_container)
        nc.setContentsMargins(0, 0, 0, 0)
        nc.addWidget(self.num_label)
        nc.addWidget(self.play_btn)
        self.play_btn.setParent(num_container)
        self.play_btn.move(0, 0)
        self.play_btn.setFixedSize(28, 28)
        self.num_container = num_container

        title = QLabel(track.get("Name", "Unknown"))
        title.setStyleSheet(f"color: {TEXT}; font-size: 13px;")

        artists_text = ", ".join(track.get("Artists", [])) or track.get("AlbumArtist", "")
        artist = QLabel(artists_text)
        artist.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")

        duration = QLabel(fmt_duration_ticks(track.get("RunTimeTicks", 0)))
        duration.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 12px;")

        h.addWidget(num_container)
        h.addWidget(title, 2)
        h.addWidget(artist, 2)
        h.addWidget(duration)

    def enterEvent(self, e):
        self.num_label.hide()
        self.play_btn.show()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.num_label.show()
        self.play_btn.hide()
        super().leaveEvent(e)


# ── Artist detail ────────────────────────────────────────────────────────────

class ArtistDetailView(QWidget):
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self.bus = PlayerBus.get()
        self._artist: Dict = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(28, 24, 28, 24)
        self._layout.setSpacing(20)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Header
        header = QHBoxLayout()
        self.image = QLabel()
        self.image.setFixedSize(180, 180)
        self.image.setStyleSheet("border-radius: 90px; background: rgba(255,255,255,0.04);")
        header.addWidget(self.image)

        info = QVBoxLayout()
        self.kind_label = QLabel("ARTIST")
        self.kind_label.setStyleSheet(f"color: {ACCENT}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;")
        self.name_label = QLabel("")
        self.name_label.setStyleSheet(f"color: {TEXT}; font-size: 32px; font-weight: 800;")
        info.addWidget(self.kind_label)
        info.addWidget(self.name_label)
        info.addStretch()

        header.addSpacing(24)
        header.addLayout(info, 1)
        self._layout.addLayout(header)

        # Albums section
        self.albums_label = QLabel("Albums")
        self.albums_label.setStyleSheet(f"color: {TEXT}; font-size: 18px; font-weight: 700; margin-top: 12px;")
        self._layout.addWidget(self.albums_label)

        self._albums_container = QWidget()
        self._albums_grid = QHBoxLayout(self._albums_container)
        self._albums_grid.setSpacing(16)
        self._albums_grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._layout.addWidget(self._albums_container)

        self.scroll.setWidget(inner)
        v.addWidget(self.scroll)

    def load_artist(self, artist: Dict):
        self._artist = artist
        self.name_label.setText(artist.get("Name", ""))
        url = self.api.get_image_url(artist.get("Id", ""), "Primary", 360)
        load_image_async(f"{artist.get('Id')}|artist", url, 180, 180,
                          self.image.setPixmap, rounded_radius=90)

        # Clear albums
        while self._albums_grid.count():
            it = self._albums_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        try:
            albums = self.api.get_artist_albums(artist.get("Id", ""))
            from modules.library_views import MediaCard
            for album in albums:
                card = MediaCard(album, art_size=160)
                card.clicked.connect(self.item_clicked)
                card.play_requested.connect(self.item_play_requested)
                self._albums_grid.addWidget(card)
        except Exception as e:
            print(f"Artist albums load: {e}")


# ── Queue panel ──────────────────────────────────────────────────────────────

class QueuePanel(QWidget):
    """Right-hand side queue list with current track highlighted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_api()
        self.setFixedWidth(320)
        self.setStyleSheet(f"""
            QWidget {{ background: rgba(255,255,255,0.02); border-left: 1px solid {BORDER}; }}
        """)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 12, 16)
        v.setSpacing(12)

        # Header
        header_row = QHBoxLayout()
        title = QLabel("Queue")
        title.setStyleSheet(f"color: {TEXT}; font-size: 16px; font-weight: 700;")
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("ghost")
        clear_btn.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; padding: 4px 8px;")
        clear_btn.clicked.connect(lambda: self.bus.queue_clear.emit())
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(clear_btn)
        v.addLayout(header_row)

        self.list = QListWidget()
        self.list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{
                padding: 10px 8px; border-radius: 6px; margin: 2px 0;
            }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.04); }}
            QListWidget::item:selected {{ background: rgba(167,139,250,0.16); }}
        """)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        v.addWidget(self.list)

        self.bus.queue_changed.connect(self._on_queue_changed)

    @pyqtSlot(list, int)
    def _on_queue_changed(self, queue: list, current: int):
        self.list.clear()
        for i, item in enumerate(queue):
            title = item.get("Name", "Unknown")
            artist = ", ".join(item.get("Artists", [])) or item.get("AlbumArtist", "")
            prefix = "♪  " if i == current else "    "
            text = f"{prefix}{title}"
            sub = artist
            lwi = QListWidgetItem(f"{text}\n      {sub}")
            lwi.setData(Qt.ItemDataRole.UserRole, i)
            if i == current:
                font = lwi.font()
                font.setBold(True)
                lwi.setFont(font)
                lwi.setForeground(QColor(ACCENT))
            self.list.addItem(lwi)
        if 0 <= current < self.list.count():
            self.list.scrollToItem(self.list.item(current))

    def _on_double_clicked(self, lwi: QListWidgetItem):
        idx = lwi.data(Qt.ItemDataRole.UserRole)
        if idx is not None:
            self.bus.track_jumped.emit(idx)

    def _context_menu(self, pos):
        lwi = self.list.itemAt(pos)
        if not lwi:
            return
        idx = lwi.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Play now", lambda: self.bus.track_jumped.emit(idx))
        menu.exec(self.list.mapToGlobal(pos))


# ── Now Playing detail (large artwork + lyrics) ──────────────────────────────

class NowPlayingView(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self.bus = PlayerBus.get()
        self._lyrics_lines: List[Dict] = []  # [{"Start": ticks, "Text": str}]

        self.setStyleSheet(f"background: {BG_PANEL};")
        h = QHBoxLayout(self)
        h.setContentsMargins(48, 36, 48, 36)
        h.setSpacing(40)

        # Left: artwork + meta
        left = QVBoxLayout()
        left.setSpacing(16)

        self.cover = QLabel()
        self.cover.setFixedSize(360, 360)
        self.cover.setStyleSheet("border-radius: 16px; background: rgba(255,255,255,0.04);")
        left.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)

        self.title = QLabel("")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 24px; font-weight: 800;")
        self.title.setWordWrap(True)
        self.title.setMaximumWidth(360)

        self.artist = QLabel("")
        self.artist.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px;")

        left.addWidget(self.title)
        left.addWidget(self.artist)
        left.addStretch()
        h.addLayout(left)

        # Right: lyrics
        right = QVBoxLayout()
        lyrics_label = QLabel("Lyrics")
        lyrics_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: 700; letter-spacing: 1.5px;")
        right.addWidget(lyrics_label)

        self.lyrics_scroll = QScrollArea()
        self.lyrics_scroll.setWidgetResizable(True)
        self.lyrics_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._lyrics_inner = QWidget()
        self._lyrics_layout = QVBoxLayout(self._lyrics_inner)
        self._lyrics_layout.setSpacing(8)
        self._lyrics_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.lyrics_scroll.setWidget(self._lyrics_inner)
        right.addWidget(self.lyrics_scroll, 1)
        h.addLayout(right, 1)

        self.bus.playback_started.connect(self._on_started)
        self.bus.position_updated.connect(self._on_position)

    @pyqtSlot(object)
    def _on_started(self, np: NowPlaying):
        self.title.setText(np.title)
        self.artist.setText(np.subtitle)
        if np.thumb_url:
            load_image_async(f"{np.item_id}|np", np.thumb_url, 360, 360,
                              self.cover.setPixmap, rounded_radius=16)
        # Load lyrics in background
        if np.is_audio:
            threading.Thread(target=self._fetch_lyrics, args=(np.item_id,), daemon=True).start()

    def _fetch_lyrics(self, item_id: str):
        lyrics = self.api.get_lyrics(item_id)
        self._lyrics_lines = lyrics.get("Lyrics", []) if lyrics else []
        # Marshal back to UI thread
        QTimer.singleShot(0, self._render_lyrics)

    def _render_lyrics(self):
        # Clear
        while self._lyrics_layout.count():
            it = self._lyrics_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self._lyrics_lines:
            empty = QLabel("No lyrics available")
            empty.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 13px; padding: 40px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._lyrics_layout.addWidget(empty)
            return
        for line in self._lyrics_lines:
            text = line.get("Text", "")
            label = QLabel(text or "♪")
            label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 15px; padding: 4px 0;")
            label.setWordWrap(True)
            self._lyrics_layout.addWidget(label)

    @pyqtSlot(int)
    def _on_position(self, ms: int):
        # Highlight current synced lyric line if available
        if not self._lyrics_lines or "Start" not in self._lyrics_lines[0]:
            return
        ticks = ms * 10_000
        active_idx = -1
        for i, line in enumerate(self._lyrics_lines):
            if line.get("Start", 0) <= ticks:
                active_idx = i
            else:
                break
        for i in range(self._lyrics_layout.count()):
            w = self._lyrics_layout.itemAt(i).widget()
            if isinstance(w, QLabel):
                if i == active_idx:
                    w.setStyleSheet(f"color: {TEXT}; font-size: 16px; font-weight: 700; padding: 4px 0;")
                else:
                    w.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 15px; padding: 4px 0;")
