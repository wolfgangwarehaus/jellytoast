"""
Library browsing views: home, music (artists/albums/songs), movies, TV, search.
"""

from typing import Optional, Callable
from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QPixmap, QFont, QCursor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGridLayout,
    QScrollArea, QListWidget, QListWidgetItem, QTabWidget, QFrame,
    QStackedWidget, QSizePolicy, QMenu, QInputDialog,
)

from modules.jellyfin_api import get_api
from modules.player_state import PlayerBus
from modules.ui_helpers import (
    load_image_async, fmt_duration_ticks, ACCENT, TEXT, TEXT_DIM, TEXT_FAINT,
    BORDER, BG_CARD,
)


# ── Card components ─────────────────────────────────────────────────────────

class _ArtworkLabel(QLabel):
    """Square artwork label that loads async."""
    def __init__(self, item_id: str, size: int, image_type: str = "Primary",
                 rounded: int = 8, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setStyleSheet(f"background: rgba(255,255,255,0.04); border-radius: {rounded}px;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        api = get_api()
        if item_id:
            url = api.get_image_url(item_id, image_type, size * 2)
            load_image_async(f"{item_id}|{image_type}", url, size, size,
                             self._set_pix, rounded)

    def _set_pix(self, pix: QPixmap):
        self.setPixmap(pix)


class MediaCard(QWidget):
    """Standard media card: artwork + title + subtitle."""
    clicked = pyqtSignal(dict)
    play_requested = pyqtSignal(dict)
    context_menu_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, item: dict, art_size: int = 160, parent=None):
        super().__init__(parent)
        self.item = item
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(art_size)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self.context_menu_requested.emit(self.item, self.mapToGlobal(p))
        )
        self._build(art_size)

    def _build(self, art_size: int):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # Artwork with hover overlay
        art_container = QWidget()
        art_container.setFixedSize(art_size, art_size)
        art_layout = QVBoxLayout(art_container)
        art_layout.setContentsMargins(0, 0, 0, 0)

        item_id = self.item.get("Id", "")
        # For audio items, prefer album art via parent album
        if self.item.get("Type") == "Audio" and self.item.get("AlbumId"):
            item_id = self.item["AlbumId"]

        self.art = _ArtworkLabel(item_id, art_size, rounded=8)
        art_layout.addWidget(self.art)

        # Play overlay (shown on hover)
        self.play_btn = QPushButton("▶", self.art)
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT}; color: white; border: none;
                border-radius: 20px; font-size: 16px; padding-left: 3px;
            }}
            QPushButton:hover {{ background: white; color: {ACCENT}; }}
        """)
        self.play_btn.move(art_size - 50, art_size - 50)
        self.play_btn.hide()
        self.play_btn.clicked.connect(lambda: self.play_requested.emit(self.item))

        # Title
        name = self.item.get("Name", "Unknown")
        self.title = QLabel(name)
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 13px; font-weight: 600;")
        self.title.setWordWrap(False)

        # Subtitle (year, artist, etc.)
        sub_text = self._subtitle_text()
        self.subtitle = QLabel(sub_text)
        self.subtitle.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 11px;")

        v.addWidget(art_container)
        v.addWidget(self.title)
        v.addWidget(self.subtitle)

    def _subtitle_text(self) -> str:
        t = self.item.get("Type", "")
        if t == "Audio":
            artists = self.item.get("Artists", [])
            return ", ".join(artists) if artists else self.item.get("AlbumArtist", "")
        if t in ("MusicAlbum",):
            year = self.item.get("ProductionYear", "")
            artist = self.item.get("AlbumArtist", "")
            if artist and year:
                return f"{artist} • {year}"
            return artist or str(year) if year else ""
        if t == "MusicArtist":
            return "Artist"
        year = self.item.get("ProductionYear", "")
        return str(year) if year else ""

    def enterEvent(self, e):
        self.play_btn.show()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.play_btn.hide()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.item)


# ── Section ─────────────────────────────────────────────────────────────────

class Section(QWidget):
    """A horizontal-scrolling section with title and a row of cards."""
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)
    item_context_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        header = QLabel(title)
        header.setStyleSheet(f"color: {TEXT}; font-size: 17px; font-weight: 700; padding-left: 4px;")
        v.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._row = QWidget()
        self._row_layout = QHBoxLayout(self._row)
        self._row_layout.setContentsMargins(2, 2, 2, 2)
        self._row_layout.setSpacing(16)
        self._row_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self._row)

        v.addWidget(self.scroll)
        self.setFixedHeight(280)

    def set_items(self, items: list, art_size: int = 170):
        # Clear
        while self._row_layout.count():
            it = self._row_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for item in items:
            card = MediaCard(item, art_size=art_size)
            card.clicked.connect(self.item_clicked)
            card.play_requested.connect(self.item_play_requested)
            card.context_menu_requested.connect(self.item_context_requested)
            self._row_layout.addWidget(card)


# ── Grid view ───────────────────────────────────────────────────────────────

class GridView(QWidget):
    """Responsive grid of media cards."""
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)
    item_context_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(20)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self._container)
        v.addWidget(self.scroll)

    def set_items(self, items: list, art_size: int = 170):
        self._items = items
        self._art_size = art_size
        self._reflow()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def _reflow(self):
        # Clear
        while self._grid.count():
            it = self._grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self._items:
            return
        viewport_w = self.scroll.viewport().width()
        gap = 20
        col_w = self._art_size + gap
        cols = max(1, viewport_w // col_w)
        for i, item in enumerate(self._items):
            card = MediaCard(item, art_size=self._art_size)
            card.clicked.connect(self.item_clicked)
            card.play_requested.connect(self.item_play_requested)
            card.context_menu_requested.connect(self.item_context_requested)
            self._grid.addWidget(card, i // cols, i % cols)


# ── Home view ───────────────────────────────────────────────────────────────

class HomeView(QWidget):
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)
    item_context_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(28)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        self._layout = QVBoxLayout(inner)
        self._layout.setSpacing(28)
        self._layout.setContentsMargins(4, 4, 4, 24)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll.setWidget(inner)
        v.addWidget(self.scroll)

    def load(self):
        # Clear
        while self._layout.count():
            it = self._layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        try:
            resume = self.api.get_resume_items(limit=12)
            if resume:
                s = Section("Continue Watching")
                s.set_items(resume)
                self._wire(s)
                self._layout.addWidget(s)

            audio_resume = self.api.get_resume_items(limit=12, media_type="Audio")
            if audio_resume:
                s = Section("Continue Listening")
                s.set_items(audio_resume)
                self._wire(s)
                self._layout.addWidget(s)

            latest = self.api.get_latest_media(limit=20)
            if latest:
                s = Section("Recently Added")
                s.set_items(latest)
                self._wire(s)
                self._layout.addWidget(s)
        except Exception as e:
            err = QLabel(f"Could not load home: {e}")
            err.setStyleSheet(f"color: {TEXT_DIM}; padding: 40px;")
            self._layout.addWidget(err)

    def _wire(self, section: Section):
        section.item_clicked.connect(self.item_clicked)
        section.item_play_requested.connect(self.item_play_requested)
        section.item_context_requested.connect(self.item_context_requested)


# ── Music view (Artists / Albums / Songs) ───────────────────────────────────

class MusicView(QWidget):
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)
    item_context_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.artists_grid = GridView()
        self.albums_grid = GridView()
        self.songs_list = QListWidget()
        self.songs_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; }
            QListWidget::item { padding: 8px 12px; border-radius: 6px; }
            QListWidget::item:hover { background: rgba(255,255,255,0.05); }
            QListWidget::item:selected { background: rgba(167,139,250,0.18); }
        """)
        self.songs_list.itemDoubleClicked.connect(self._on_song_double_clicked)

        for grid in (self.artists_grid, self.albums_grid):
            grid.item_clicked.connect(self.item_clicked)
            grid.item_play_requested.connect(self.item_play_requested)
            grid.item_context_requested.connect(self.item_context_requested)

        self.tabs.addTab(self.artists_grid, "Artists")
        self.tabs.addTab(self.albums_grid, "Albums")
        self.tabs.addTab(self.songs_list, "Songs")

        v.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._loaded = {0: False, 1: False, 2: False}

    def load(self):
        self._loaded = {0: False, 1: False, 2: False}
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, idx: int):
        if self._loaded.get(idx):
            return
        self._loaded[idx] = True
        try:
            if idx == 0:
                self.artists_grid.set_items(self.api.get_artists(limit=200))
            elif idx == 1:
                self.albums_grid.set_items(self.api.get_albums(limit=300))
            elif idx == 2:
                self._load_songs()
        except Exception as e:
            print(f"Music tab load: {e}")

    def _load_songs(self):
        try:
            data = self.api.get_items(item_type="Audio", limit=500,
                                       sort_by="SortName", recursive=True)
            self.songs_list.clear()
            for it in data.get("Items", []):
                title = it.get("Name", "")
                artist = ", ".join(it.get("Artists", [])) or it.get("AlbumArtist", "")
                duration = fmt_duration_ticks(it.get("RunTimeTicks", 0))
                widget_text = f"  {title}     {artist}     {duration}"
                lwi = QListWidgetItem(widget_text)
                lwi.setData(Qt.ItemDataRole.UserRole, it)
                self.songs_list.addItem(lwi)
        except Exception as e:
            print(f"Songs load: {e}")

    def _on_song_double_clicked(self, lwi: QListWidgetItem):
        item = lwi.data(Qt.ItemDataRole.UserRole)
        if item:
            # Build a queue from the entire visible list, starting at this song
            all_items = []
            sel_index = 0
            for i in range(self.songs_list.count()):
                d = self.songs_list.item(i).data(Qt.ItemDataRole.UserRole)
                all_items.append(d)
                if d.get("Id") == item.get("Id"):
                    sel_index = i
            PlayerBus.get().queue_play_now.emit(all_items, sel_index)


# ── Generic typed grid (Movies, TV) ─────────────────────────────────────────

class TypedGridView(QWidget):
    item_clicked = pyqtSignal(dict)
    item_play_requested = pyqtSignal(dict)
    item_context_requested = pyqtSignal(dict, "QPoint")

    def __init__(self, item_type: str, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self.item_type = item_type

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.grid = GridView()
        self.grid.item_clicked.connect(self.item_clicked)
        self.grid.item_play_requested.connect(self.item_play_requested)
        self.grid.item_context_requested.connect(self.item_context_requested)
        v.addWidget(self.grid)

    def load(self):
        try:
            data = self.api.get_items(item_type=self.item_type, limit=300, recursive=True)
            self.grid.set_items(data.get("Items", []))
        except Exception as e:
            print(f"TypedGrid load: {e}")
