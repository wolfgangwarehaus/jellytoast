"""
Native Songs view — Phase 4 of the native-UI pivot.

Replaces JF Web's Music → Songs tab with a PySide6 list. Each row:
small cover thumb + title + artist + album + duration. Click any row
to install the visible song list as the live queue and start playing
from that index.

Why a list, not a grid: songs are far denser than albums (a typical
library is hundreds-to-thousands of tracks) and the per-track
metadata (artist + album + duration) reads more comfortably across
a row than stacked under a tile.

Sort is reused from the shared library_sort_by Settings, with
per-kind fallback (artist/release-date sorts that apply to albums
fall back to SortName for songs since the fields aren't always
present on Audio items).
"""

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QSize, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QScrollArea,
    QSizePolicy,
)

from modules.async_io import run_async
from modules.jellyfin_api import get_api
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars, fmt_duration_ticks,
    ACCENT, TEXT, TEXT_DIM, TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_BODY, TYPE_CAPTION, TYPE_MICRO, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


class _ElidingLabel(QLabel):
    """QLabel that elides overflow with `…` (mirrors the helpers in
    library_grid / now_playing_page; tiny enough to keep duplicated)."""
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        super().setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str):
        self._full = text or ""
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        avail = max(0, self.width() - 4)
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return self.minimumSizeHint()


# ── Row ─────────────────────────────────────────────────────────────────

class _SongRow(QFrame):
    """Single song row. Cover thumb + title + artist + album + duration.
    Click → emit play_requested(index) so the parent can install the
    visible list as the live queue starting at this row."""

    play_requested = Signal(int)

    THUMB_SIZE = 44
    ROW_HEIGHT = 56

    def __init__(self, index: int, item: Dict, parent=None):
        super().__init__(parent)
        self._index = index
        self._item = item
        self._is_current = False
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("songRow")
        self.setStyleSheet("""
            QFrame#songRow {
                background: transparent; border: none; border-radius: 6px;
            }
            QFrame#songRow:hover { background: rgba(255, 255, 255, 0.04); }
            QFrame#songRow QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, 4, SPACE_MD, 4)
        layout.setSpacing(SPACE_MD)

        # Cover thumb — square, fills row height.
        self._thumb = QLabel()
        self._thumb.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background: rgba(255,255,255,0.04); border-radius: 4px;"
        )
        layout.addWidget(self._thumb)

        # Title — bold body, single line, eliding.
        self._title = _ElidingLabel(item.get("Name", "Unknown"))
        self._title.setStyleSheet(self._title_css(active=False))
        layout.addWidget(self._title, 3)

        # Artist — dim caption.
        artists = item.get("Artists") or []
        artist = ", ".join(artists) if artists else (item.get("AlbumArtist", "") or "")
        self._artist = _ElidingLabel(artist)
        self._artist.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        layout.addWidget(self._artist, 2)

        # Album — fainter caption.
        self._album = _ElidingLabel(item.get("Album", "") or "")
        self._album.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        layout.addWidget(self._album, 2)

        # Duration — fixed-width, monospace, right-aligned.
        ticks = item.get("RunTimeTicks", 0) or 0
        self._duration = QLabel(fmt_duration_ticks(ticks) if ticks else "")
        self._duration.setFixedWidth(56)
        self._duration.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._duration.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )
        layout.addWidget(self._duration)

    @staticmethod
    def _title_css(active: bool) -> str:
        if active:
            return f"color: {ACCENT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        return f"color: {TEXT}; {type_qss(TYPE_BODY)}"

    def set_thumb(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        scaled = pix.scaled(
            self.THUMB_SIZE, self.THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.size() != QSize(self.THUMB_SIZE, self.THUMB_SIZE):
            x = max(0, (scaled.width() - self.THUMB_SIZE) // 2)
            y = max(0, (scaled.height() - self.THUMB_SIZE) // 2)
            scaled = scaled.copy(x, y, self.THUMB_SIZE, self.THUMB_SIZE)
        self._thumb.setPixmap(scaled)

    def set_current(self, is_current: bool):
        if is_current == self._is_current:
            return
        self._is_current = is_current
        self._title.setStyleSheet(self._title_css(active=is_current))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._index)
        super().mousePressEvent(e)


# ── List view ───────────────────────────────────────────────────────────

class SongsView(QWidget):
    """Vertical list of all songs in the music library. Click a row →
    install the visible list as the live queue starting at that
    index. Sort is honored from the shared library_sort_by setting,
    with per-kind sanitization since some album-level sort fields
    don't apply to Audio items."""

    play_requested = Signal(int, list)  # start_idx, item_list

    _items_loaded = Signal(object)

    HEADER_LABEL = "SONGS"
    ITEM_TYPE = "Audio"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self._rows: List[_SongRow] = []
        self._items: List[Dict] = []
        self._parent_id: str = ""

        # Initial sort from Settings (shared with the grids).
        from modules.settings import get_settings
        s = get_settings()
        self._sort_by = s.library_sort_by or "SortName"
        self._sort_order = (
            "Descending" if s.library_sort_order == "descending" else "Ascending"
        )

        self.setObjectName("songsView")
        self.setStyleSheet("""
            QWidget#songsView { background: transparent; }
            QWidget#songsView QScrollArea {
                background: transparent; border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget()
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setContentsMargins(
            SPACE_LG, 0, SPACE_LG, SPACE_LG,
        )
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._items_loaded.connect(self._on_items_loaded)

    # ── Public API ─────────────────────────────────────────────────────

    def load_songs(self, parent_id: str = ""):
        """Async-fetch all songs under `parent_id`. Repopulates when
        the result lands. Limit 2000 — tracks are denser than albums
        so we cap higher; pagination on the scroll is a follow-up."""
        self._parent_id = parent_id
        self._clear_rows()
        sort_by = self._safe_sort(self._sort_by)
        run_async(
            self.api.get_items, parent_id, self.ITEM_TYPE, 2000, 0,
            sort_by, self._sort_order, True,  # recursive
            on_result=lambda resp: self._items_loaded.emit(resp),
            on_error=lambda _e: self._items_loaded.emit({"Items": []}),
        )

    def set_sort(self, sort_by: str, sort_order: str):
        self._sort_by = sort_by or "SortName"
        self._sort_order = (
            "Descending" if sort_order == "descending" else "Ascending"
        )
        self.load_songs(self._parent_id)

    @staticmethod
    def _safe_sort(sort_by: str) -> str:
        # Audio items always have SortName; other album-level fields
        # may be absent (PremiereDate), so fall back to SortName for
        # sorts that would otherwise return zero items.
        if not sort_by:
            return "SortName"
        first = sort_by.split(",", 1)[0]
        if first == "PremiereDate":
            return "SortName"
        return sort_by

    # ── Async result handler ───────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        self._items = items
        for i, item in enumerate(items):
            row = _SongRow(i, item)
            row.play_requested.connect(self._on_row_clicked)
            self._rows.append(row)
            # Insert above the trailing stretch so spacing stays right.
            insert_at = self._list_layout.count() - 1
            self._list_layout.insertWidget(insert_at, row)
            # Lazy cover load — uses AlbumPrimaryImageTag if present
            # (means the image is on the album, not the track itself).
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if cover_id:
                cover_url = self.api.get_image_url(cover_id, "Primary", 120)
                if cover_url:
                    load_image_async(
                        f"{cover_id}|songrow",
                        cover_url, 120, 120,
                        row.set_thumb, rounded_radius=4,
                    )

    def _clear_rows(self):
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self._items = []

    # ── Row click → emit play with snapshot ────────────────────────────

    @Slot(int)
    def _on_row_clicked(self, index: int):
        if 0 <= index < len(self._items):
            self.play_requested.emit(index, list(self._items))
