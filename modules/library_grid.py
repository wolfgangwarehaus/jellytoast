"""
Native album library grid — Phase 4 of the native-UI pivot.

Replaces JF Web's Music → Albums browse view with a PySide6 grid of
album tiles. Each tile clicks two ways:

- Edge / cover / text → browse_requested(album_id): host swaps to
  NowPlayingPage in preview mode (current track keeps playing).
- Centered hover-revealed play overlay → play_requested(album_id):
  host installs the album as the live queue and starts from track 0.

The two-click split mirrors how Spotify / Apple Music / Plexamp tile
grids behave and matches the same pattern we use elsewhere — the
tile's "intent" is browse; play is the explicit secondary action.

Why this exists: replacing JF Web for browse views removes the entire
brittle bridge layer (URL interception, intent_detected, silence_jfweb,
queue_state attribution, AlbumId-uniformity heuristics, JS click
capture). A native tile's play button calls bus.queue_play_now
directly with the right QueueContext — no round-trip, no inference.
"""

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QSize, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QGridLayout,
    QScrollArea, QSizePolicy,
)

from modules.async_io import run_async
from modules.jellyfin_api import get_api
from modules.ui_helpers import load_image_async, TEXT, TEXT_DIM, TEXT_FAINT
from modules.icons import icon
from modules.design_tokens import (
    TYPE_BODY, TYPE_CAPTION, TYPE_MICRO, font, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


# ── Eliding label (local copy — small enough not to share yet) ──────────

class _ElidingLabel(QLabel):
    """QLabel that elides overflow with '…' instead of growing the parent."""
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


# ── Tile ────────────────────────────────────────────────────────────────

class AlbumTile(QFrame):
    """One album in the grid. Cover + title + artist; hover reveals a
    centered play button overlay that's a child of the cover container
    (so it floats above the artwork without disturbing layout)."""

    play_requested = Signal(str)    # album_id
    browse_requested = Signal(str)  # album_id

    COVER_SIZE = 180
    OVERLAY_SIZE = 56

    def __init__(self, item: Dict, parent=None):
        super().__init__(parent)
        self._item = item
        self._album_id = item.get("Id", "")
        self.setObjectName("albumTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(self.COVER_SIZE)
        self.setStyleSheet("""
            QFrame#albumTile { background: transparent; border: none; }
            QFrame#albumTile QLabel { background: transparent; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        # Cover: a QFrame as a fixed-size container so we can position
        # the play overlay absolutely inside it. The QLabel inside paints
        # the artwork; the QPushButton sits on top.
        self._cover_box = QFrame(self)
        self._cover_box.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover_box.setStyleSheet("""
            QFrame {
                background: rgba(255, 255, 255, 0.04);
                border-radius: 8px;
            }
        """)

        self._cover = QLabel(self._cover_box)
        self._cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet("background: transparent;")

        self._play_overlay = QPushButton(self._cover_box)
        self._play_overlay.setIcon(icon("play"))
        self._play_overlay.setIconSize(QSize(28, 28))
        self._play_overlay.setFixedSize(self.OVERLAY_SIZE, self.OVERLAY_SIZE)
        self._play_overlay.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_overlay.setStyleSheet("""
            QPushButton {
                background: rgba(0, 0, 0, 0.65);
                color: white;
                border: 2px solid rgba(255, 255, 255, 0.85);
                border-radius: 28px;
            }
            QPushButton:hover { background: rgba(0, 0, 0, 0.85); }
            QPushButton:pressed { background: rgba(0, 0, 0, 0.95); }
        """)
        # Center the overlay in the cover.
        self._play_overlay.move(
            (self.COVER_SIZE - self.OVERLAY_SIZE) // 2,
            (self.COVER_SIZE - self.OVERLAY_SIZE) // 2,
        )
        self._play_overlay.clicked.connect(self._on_play_clicked)
        self._play_overlay.hide()

        layout.addWidget(self._cover_box)

        # Title — bold body, single line, eliding.
        self._title = _ElidingLabel(item.get("Name", "Unknown"))
        self._title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        )
        layout.addWidget(self._title)

        # Artist — muted caption. Album items expose AlbumArtist directly
        # (or AlbumArtists as a list); fall back to empty if missing.
        artist = item.get("AlbumArtist") or ", ".join(
            item.get("AlbumArtists", []) or []
        ) or ""
        self._artist = _ElidingLabel(artist)
        self._artist.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        layout.addWidget(self._artist)

        layout.addStretch(0)

    # ── Cover loader callback ──────────────────────────────────────────

    @Slot(object)
    def set_cover(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        # Scale to cover size with center-crop so non-square art doesn't
        # letterbox.
        scaled = pix.scaled(
            self.COVER_SIZE, self.COVER_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.size() != QSize(self.COVER_SIZE, self.COVER_SIZE):
            x = max(0, (scaled.width() - self.COVER_SIZE) // 2)
            y = max(0, (scaled.height() - self.COVER_SIZE) // 2)
            scaled = scaled.copy(x, y, self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setPixmap(scaled)

    # ── Hover → reveal play overlay ────────────────────────────────────

    def enterEvent(self, e):
        super().enterEvent(e)
        self._play_overlay.show()
        self._play_overlay.raise_()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._play_overlay.hide()

    # ── Click → browse (play overlay handles its own click) ────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.browse_requested.emit(self._album_id)
        super().mousePressEvent(e)

    @Slot()
    def _on_play_clicked(self):
        # Play button consumes the click so it doesn't bubble to the
        # tile's mousePressEvent (which would also emit browse_requested).
        self.play_requested.emit(self._album_id)


# ── Grid ────────────────────────────────────────────────────────────────

class AlbumLibraryGrid(QWidget):
    """Responsive grid of AlbumTiles. Recomputes column count on resize.
    Async-fetches albums + lazy-loads each cover via the shared image
    pipeline so scrolling stays smooth."""

    play_requested = Signal(str)
    browse_requested = Signal(str)

    # Async result lands on the GUI thread via this signal so we don't
    # touch widgets from the worker thread.
    _albums_loaded = Signal(object)  # list of items

    TILE_WIDTH = AlbumTile.COVER_SIZE
    GAP = SPACE_LG          # 16px between tiles
    PADDING = SPACE_XL      # 24px around the grid

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self._tiles: List[AlbumTile] = []
        self._current_cols = 0

        self.setObjectName("albumGrid")
        self.setStyleSheet("""
            QWidget#albumGrid { background: transparent; }
            QWidget#albumGrid QScrollArea {
                background: transparent; border: none;
            }
            QWidget#albumGrid QScrollBar:vertical {
                background: transparent; width: 8px;
                margin: 4px 2px 4px 0; border: none;
            }
            QWidget#albumGrid QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px; min-height: 28px;
            }
            QWidget#albumGrid QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.32);
            }
            QWidget#albumGrid QScrollBar::add-line:vertical,
            QWidget#albumGrid QScrollBar::sub-line:vertical { height: 0; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header: kicker + count. Kicker uses MICRO + .upper() because
        # type_qss won't actually transform-uppercase in QSS.
        self._header = QLabel("ALBUMS")
        self._header.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)} "
            f"padding: {SPACE_LG}px {SPACE_XL}px {SPACE_SM}px {SPACE_XL}px;"
        )
        outer.addWidget(self._header)

        # Scroll area holds the tile grid.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._container)
        self._grid_layout.setContentsMargins(
            self.PADDING, 0, self.PADDING, self.PADDING,
        )
        self._grid_layout.setHorizontalSpacing(self.GAP)
        self._grid_layout.setVerticalSpacing(self.GAP + SPACE_SM)
        self._grid_layout.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._albums_loaded.connect(self._on_albums_loaded)

    # ── Public API ─────────────────────────────────────────────────────

    def load_albums(self, parent_id: str = ""):
        """Async-fetch all albums under `parent_id` (empty = whole user
        library, recursive). Repopulates the grid when the result lands."""
        # Clear existing tiles immediately so stale art doesn't linger.
        self._clear_tiles()
        self._header.setText("ALBUMS  ·  Loading…")
        run_async(
            self.api.get_items, parent_id, "MusicAlbum", 1000, 0,
            "SortName", "Ascending", True,  # recursive
            on_result=lambda resp: self._albums_loaded.emit(resp),
            on_error=lambda _e: self._albums_loaded.emit({"Items": []}),
        )

    # ── Async result handler ───────────────────────────────────────────

    @Slot(object)
    def _on_albums_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        total = (resp or {}).get("TotalRecordCount", len(items))
        self._header.setText(f"ALBUMS  ·  {total}")
        for item in items:
            tile = AlbumTile(item)
            tile.play_requested.connect(self.play_requested.emit)
            tile.browse_requested.connect(self.browse_requested.emit)
            self._tiles.append(tile)
            cover_url = self.api.get_image_url(
                item.get("Id", ""), "Primary", 360,
            )
            if cover_url:
                load_image_async(
                    f"{item.get('Id')}|albumtile",
                    cover_url, 360, 360,
                    tile.set_cover, rounded_radius=8,
                )
        # Force a reflow so tiles get placed in the grid.
        self._current_cols = 0
        self._reflow_grid()

    def _clear_tiles(self):
        for tile in self._tiles:
            self._grid_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = []

    # ── Responsive reflow ──────────────────────────────────────────────

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow_grid()

    def _reflow_grid(self):
        if not self._tiles:
            return
        # Use the scroll area's viewport width (the actual usable area
        # for tiles) rather than self.width(), which counts the
        # scrollbar lane too.
        viewport = self._scroll.viewport()
        avail = (viewport.width() if viewport is not None else self.width()) \
                - 2 * self.PADDING
        per_tile = self.TILE_WIDTH + self.GAP
        cols = max(1, (avail + self.GAP) // per_tile)
        if cols == self._current_cols:
            return
        self._current_cols = cols
        # Pull every tile out of the layout, re-insert at new (row, col).
        for tile in self._tiles:
            self._grid_layout.removeWidget(tile)
        for i, tile in enumerate(self._tiles):
            row, col = divmod(i, cols)
            self._grid_layout.addWidget(tile, row, col)
