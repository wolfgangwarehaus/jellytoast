"""
Native Genres view — Phase 4 of the native-UI pivot.

A grid of genre tiles for the music library. Clicking a tile opens
the AlbumLibraryGrid scoped to that genre's parent_id (Jellyfin
treats genres as virtual containers, so passing the genre's Id as
parent_id with IncludeItemTypes=MusicAlbum returns just the albums
tagged with that genre).

Each tile is a name-only card — Jellyfin's MusicGenres endpoint
doesn't expose representative artwork, and faking one (e.g. picking
a random album in the genre) reads as noise rather than signal.
The tiles use the accent color as a subtle wash so the grid feels
alive without per-genre artwork.
"""

from typing import Dict, List

from PySide6.QtCore import Qt, QSize, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QGridLayout, QScrollArea,
)

from modules.async_io import run_async
from modules.jellyfin_api import get_api
from modules.ui_helpers import ACCENT, ACCENT_DEEP, TEXT, TEXT_FAINT
from modules.design_tokens import (
    TYPE_SUBHEAD, TYPE_MICRO, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


class _GenreTile(QFrame):
    """Name-only tile. Cover-less by design — Jellyfin doesn't expose
    representative genre artwork."""

    clicked_id = Signal(str, str)  # (genre_id, genre_name)

    TILE_WIDTH = 200
    TILE_HEIGHT = 88

    def __init__(self, item: Dict, parent=None):
        super().__init__(parent)
        self._item = item
        self._id = item.get("Id", "")
        self._name = item.get("Name", "")
        self.setFixedSize(self.TILE_WIDTH, self.TILE_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("genreTile")
        # Accent gradient wash — subtle but distinguishes the tile
        # from a flat dark void. Uses both accent shades so the wash
        # has depth.
        self.setStyleSheet(f"""
            QFrame#genreTile {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 {ACCENT_DEEP}, stop:1 {ACCENT}
                );
                border-radius: 8px;
                border: none;
            }}
            QFrame#genreTile:hover {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 {ACCENT}, stop:1 {ACCENT_DEEP}
                );
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        layout.setSpacing(0)

        label = QLabel(self._name)
        label.setStyleSheet(
            f"color: white; {type_qss(TYPE_SUBHEAD)} background: transparent;"
        )
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label, 1)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked_id.emit(self._id, self._name)
        super().mousePressEvent(e)


class GenresView(QWidget):
    """Grid of music genre tiles. Click any tile → genre_selected
    signal carries (genre_id, genre_name) so the host can swap to
    a filtered AlbumLibraryGrid."""

    genre_selected = Signal(str, str)

    _genres_loaded = Signal(object)

    HEADER_LABEL = "GENRES"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self._tiles: List[_GenreTile] = []
        self._current_cols = 0

        self.setObjectName("genresView")
        self.setStyleSheet("""
            QWidget#genresView { background: transparent; }
            QWidget#genresView QScrollArea {
                background: transparent; border: none;
            }
            QWidget#genresView QScrollBar:vertical {
                background: transparent; width: 8px;
                margin: 4px 2px 4px 0; border: none;
            }
            QWidget#genresView QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px; min-height: 28px;
            }
            QWidget#genresView QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.32);
            }
            QWidget#genresView QScrollBar::add-line:vertical,
            QWidget#genresView QScrollBar::sub-line:vertical { height: 0; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QLabel(self.HEADER_LABEL)
        self._header.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)} "
            f"padding: {SPACE_LG}px {SPACE_XL}px {SPACE_SM}px {SPACE_XL}px;"
        )
        outer.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._container = QWidget()
        self._grid_layout = QGridLayout(self._container)
        self._grid_layout.setContentsMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        self._grid_layout.setHorizontalSpacing(SPACE_LG)
        self._grid_layout.setVerticalSpacing(SPACE_LG)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._genres_loaded.connect(self._on_genres_loaded)

    def load_genres(self):
        self._clear_tiles()
        self._header.setText(f"{self.HEADER_LABEL}  ·  Loading…")
        run_async(
            self.api.get_genres,
            on_result=lambda items: self._genres_loaded.emit(items),
            on_error=lambda _e: self._genres_loaded.emit([]),
        )

    @Slot(object)
    def _on_genres_loaded(self, items):
        items = items or []
        self._header.setText(f"{self.HEADER_LABEL}  ·  {len(items)}")
        for item in items:
            tile = _GenreTile(item)
            tile.clicked_id.connect(self.genre_selected.emit)
            self._tiles.append(tile)
        self._current_cols = 0
        self._reflow_grid()

    def _clear_tiles(self):
        for tile in self._tiles:
            self._grid_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = []

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow_grid()

    def _reflow_grid(self):
        if not self._tiles:
            return
        viewport = self._scroll.viewport()
        avail = (viewport.width() if viewport is not None else self.width()) \
                - 2 * SPACE_XL
        per_tile = _GenreTile.TILE_WIDTH + SPACE_LG
        cols = max(1, (avail + SPACE_LG) // per_tile)
        if cols == self._current_cols:
            return
        self._current_cols = cols
        for tile in self._tiles:
            self._grid_layout.removeWidget(tile)
        for i, tile in enumerate(self._tiles):
            row, col = divmod(i, cols)
            self._grid_layout.addWidget(
                tile, row, col, Qt.AlignmentFlag.AlignHCenter,
            )
        for col in range(cols):
            self._grid_layout.setColumnStretch(col, 1)
        for col in range(cols, cols + 16):
            self._grid_layout.setColumnStretch(col, 0)
