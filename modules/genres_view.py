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

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QGridLayout, QScrollArea,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.providers import get_provider
from modules.ui_helpers import (
    install_autofade_scrollbars, ACCENT, ACCENT_DEEP,
)
from modules.design_tokens import (
    TYPE_SUBHEAD, type_qss,
    SPACE_MD, SPACE_LG, SPACE_XL,
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
        self._apply_accent_styling()
        # Live-accent: rebuild the gradient when the user picks a new
        # accent in Settings → Display.
        from modules.player_state import PlayerBus
        PlayerBus.get().theme_changed.connect(self._apply_accent_styling)

    def _apply_accent_styling(self):
        # Re-pull ACCENT / ACCENT_DEEP at call time so live changes
        # propagate; the module-level names imported at the top of
        # this file are a snapshot from first import.
        from modules.ui_helpers import ACCENT as _A, ACCENT_DEEP as _AD
        # Accent gradient wash — subtle but distinguishes the tile
        # from a flat dark void. Uses both accent shades so the wash
        # has depth.
        self.setStyleSheet(f"""
            QFrame#genreTile {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 {_AD}, stop:1 {_A}
                );
                border-radius: 8px;
                border: none;
            }}
            QFrame#genreTile:hover {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 {_A}, stop:1 {_AD}
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
    _refresh_loaded = Signal(object)

    HEADER_LABEL = "GENRES"
    CACHE_NAME = "genres"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._tiles: List[_GenreTile] = []
        self._current_cols = 0

        self.setObjectName("genresView")
        # Sweep transparency across every descendant so the scroll bar
        # lane lets the body show through.
        self.setStyleSheet("""
            QWidget#genresView,
            QWidget#genresView QWidget,
            QWidget#genresView QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget(self._scroll)
        self._grid_layout = QGridLayout(self._container)
        self._grid_layout.setContentsMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        self._grid_layout.setHorizontalSpacing(SPACE_LG)
        self._grid_layout.setVerticalSpacing(SPACE_LG)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._genres_loaded.connect(self._on_genres_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)

    # ── Public API ─────────────────────────────────────────────────────

    # Scope is empty since get_genres takes no parameters that we
    # vary — same query for the whole user library every time.
    _SCOPE: dict = {}

    def load_genres(self):
        """Render genres. Two-phase: render from disk cache instantly
        if present, then verify against the server in the background.
        Cold load fires straight through to the API and persists on
        success."""
        cached = disk_cache.load(self.CACHE_NAME, self._SCOPE)
        if cached:
            self._clear_tiles()
            self._genres_loaded.emit(cached)
            run_async(
                self.api.get_genres,
                on_result=lambda items: self._refresh_loaded.emit(items),
                on_error=lambda _e: None,
            )
            return
        self._clear_tiles()
        run_async(
            self.api.get_genres,
            on_result=lambda items: self._on_cold_fetch(items),
            on_error=lambda _e: self._genres_loaded.emit([]),
        )

    def _on_cold_fetch(self, items):
        items = items or []
        if items:
            disk_cache.save(self.CACHE_NAME, self._SCOPE, items)
        self._genres_loaded.emit(items)

    @Slot(object)
    def _on_genres_loaded(self, items):
        items = items or []
        for item in items:
            tile = _GenreTile(item, parent=self._container)
            tile.clicked_id.connect(self.genre_selected.emit)
            self._tiles.append(tile)
        self._current_cols = 0
        self._reflow_grid()

    @Slot(object)
    def _on_refresh_loaded(self, items):
        """Background-refresh handler — only re-renders if the genre
        list actually changed since the cached snapshot."""
        items = items or []
        disk_cache.save(self.CACHE_NAME, self._SCOPE, items)
        if self._items_signature(items) == self._items_signature(
            [t._item for t in self._tiles]
        ):
            return
        self._clear_tiles()
        self._on_genres_loaded(items)

    @staticmethod
    def _items_signature(items):
        return tuple(it.get("Id", "") for it in items)

    def _clear_tiles(self):
        for tile in self._tiles:
            self._grid_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = []

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow_grid()

    def showEvent(self, e):
        super().showEvent(e)
        # Cache-hit cold launch can populate tiles before the widget
        # gets its real geometry; defer one more reflow so the column
        # count is recomputed at the visible size.
        QTimer.singleShot(0, self._reflow_grid)

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
