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

Built on QListView + QAbstractListModel + QStyledItemDelegate to
match the rendering scaffolding used everywhere else.
"""

from typing import Dict, List

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_XL,
    TYPE_SUBHEAD,
)
from modules.providers import get_provider
from modules.ui_helpers import EmptyState, install_autofade_scrollbars

# ── Model ────────────────────────────────────────────────────────────────


class _GenresModel(QAbstractListModel):
    """Plain item list for genre tiles. No per-row cache (no artwork
    to load); ItemRole returns the source dict and the delegate
    pulls everything from it."""

    ItemRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._items)):
            return None
        if role == self.ItemRole:
            return self._items[row]
        return None

    def items(self) -> List[Dict]:
        return self._items

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()


# ── Delegate ─────────────────────────────────────────────────────────────


class _GenreDelegate(QStyledItemDelegate):
    """Paints one genre tile — accent gradient + name. Hover state
    swaps the gradient direction so the tile reads as interactive.
    No artwork to load; the whole paint is solid-color drawing."""

    TILE_WIDTH = 200
    TILE_HEIGHT = 88
    CELL_W = TILE_WIDTH + SPACE_LG
    CELL_H = TILE_HEIGHT + SPACE_LG
    RADIUS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_fonts()
        # Rebuild the cached font + metrics when the theme / font scale
        # changes at runtime — matches the live-accent contract the other
        # delegates follow (see _TileDelegate in library_grid).
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._build_fonts)

    def _build_fonts(self):
        title_font = QFont()
        title_font.setPixelSize(TYPE_SUBHEAD.size_px)
        title_font.setBold(True)
        self._title_font = title_font
        self._fm_title = QFontMetrics(title_font)

    def sizeHint(self, option, index):
        return QSize(self.CELL_W, self.CELL_H)

    def paint(self, painter, option, index):
        item = index.data(_GenresModel.ItemRole)
        if item is None:
            return
        name = item.get("Name", "")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Re-read accent on every paint so PlayerBus.theme_changed
        # propagates with just a viewport().update().
        from modules.ui_helpers import ACCENT as _A
        from modules.ui_helpers import ACCENT_DEEP as _AD

        # Center the (TILE_WIDTH × TILE_HEIGHT) tile inside the cell.
        cell = option.rect
        x = cell.x() + (cell.width() - self.TILE_WIDTH) // 2
        y = cell.y() + (cell.height() - self.TILE_HEIGHT) // 2
        tile = QRect(x, y, self.TILE_WIDTH, self.TILE_HEIGHT)

        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        # Gradient stops swap on hover so the tile "lights up".
        grad = QLinearGradient(QPoint(tile.x(), tile.y()), QPoint(tile.right(), tile.bottom()))
        if hovered:
            grad.setColorAt(0.0, QColor(_A))
            grad.setColorAt(1.0, QColor(_AD))
        else:
            grad.setColorAt(0.0, QColor(_AD))
            grad.setColorAt(1.0, QColor(_A))

        path = QPainterPath()
        path.addRoundedRect(QRectF(tile), self.RADIUS, self.RADIUS)
        painter.fillPath(path, grad)

        # Genre name — subhead font, white, left-aligned, vertically
        # centered. Eliding falls back to "…" if the name is too long
        # for one line of the tile. Font + metrics are cached on the
        # delegate (see _build_fonts) and rebound on theme_changed, so
        # the hover-repaint hot path allocates nothing here.
        painter.setFont(self._title_font)
        fm = self._fm_title
        text_rect = tile.adjusted(SPACE_LG, SPACE_MD, -SPACE_LG, -SPACE_MD)
        painter.setPen(QColor("white"))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm.elidedText(name, Qt.TextElideMode.ElideRight, text_rect.width()),
        )

        painter.restore()


# ── View ─────────────────────────────────────────────────────────────────


class _GenresListView(QListView):
    """QListView tuned for the genres surface: IconMode, uniform
    sizing, mouse tracking for hover repaints, click → emit
    ``tile_clicked(id, name)``."""

    tile_clicked = Signal(str, str)  # (genre_id, genre_name)

    def __init__(self, delegate: _GenreDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate
        self.setItemDelegate(delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        # Viewport transparency — flatten the first-paint to dodge the
        # default-palette Base lightgrey flash.
        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self.setStyleSheet("QListView { background: transparent; border: none; }")

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(e)
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            super().mousePressEvent(e)
            return
        item = idx.data(_GenresModel.ItemRole) or {}
        gid = item.get("Id", "")
        gname = item.get("Name", "")
        if gid:
            self.tile_clicked.emit(gid, gname)
            e.accept()
            return
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        """Right-click a genre tile → Start genre radio / Create smart
        playlist. Genre radio keys off the genre *name*
        (``get_genre_radio``), not an id; the RadioFeeder auto-extends
        the INSTANT_MIX queue from the stamped ``seed_kind="genre"``."""
        idx = self.indexAt(e.pos())
        if not idx.isValid():
            super().contextMenuEvent(e)
            return
        item = idx.data(_GenresModel.ItemRole) or {}
        gname = item.get("Name", "")
        if not gname:
            super().contextMenuEvent(e)
            return

        from modules.ui_helpers import (
            opaque_menu,
            open_create_smart_playlist,
            start_seed_radio,
        )

        menu = opaque_menu(self)
        radio_act = menu.addAction("Start genre radio")
        sp_act = menu.addAction(f"Create smart playlist: {gname} Discoveries")
        chosen = menu.exec(e.globalPos())
        if chosen is radio_act:
            start_seed_radio("genre", item.get("Id", ""), gname)
        elif chosen is sp_act:
            open_create_smart_playlist(self, "genre", gname)


# ── Public view ──────────────────────────────────────────────────────────


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

        self.setObjectName("genresView")
        self.setStyleSheet("""
            QWidget#genresView,
            QWidget#genresView QWidget,
            QWidget#genresView QListView {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        self._model = _GenresModel(self)
        self._delegate = _GenreDelegate(self)
        self._view = _GenresListView(self._delegate, self)
        self._view.setModel(self._model)
        self._view.setViewportMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        install_autofade_scrollbars(self._view)

        # Stack the genre grid with an empty-state surface so an
        # empty library (or a silently-failed fetch) doesn't read as
        # a blank scroll area.
        self._empty_state = EmptyState(
            glyph="♪",
            headline="No genres yet",
            sub="Your library hasn't reported any genres — try "
            "refreshing the library or wait for tracks to import.",
            action_label="Refresh",
            parent=self,
        )
        self._empty_state.action_clicked.connect(
            self._on_empty_state_refresh,
        )
        self._content_stack = QStackedWidget(self)
        self._content_stack.setStyleSheet("background: transparent;")
        self._content_stack.addWidget(self._view)
        self._content_stack.addWidget(self._empty_state)
        outer.addWidget(self._content_stack, 1)
        self._initial_load_complete = False

        self._view.tile_clicked.connect(self.genre_selected.emit)

        # Live-accent: delegate re-reads ACCENT / ACCENT_DEEP at paint
        # time, so a theme change just needs a viewport invalidate.
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._view.viewport().update)

        self._genres_loaded.connect(self._on_genres_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)

    # ── Backwards-compatible accessors ────────────────────────────────

    @property
    def _tiles(self) -> List[Dict]:
        """jellytoast.py reads this to gate re-loads on emptiness.
        Old implementation exposed a list of tile widgets; the new
        one returns the model's item dicts. Same shape (a list,
        truthy when loaded)."""
        return self._model.items()

    # ── Public API ────────────────────────────────────────────────────

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
            self._genres_loaded.emit(cached)
            run_async(
                self.api.get_genres,
                on_result=lambda items: self._refresh_loaded.emit(items),
                on_error=lambda _e: None,
            )
            return
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
        self._initial_load_complete = True
        self._model.set_items(items)
        self._content_stack.setCurrentIndex(0 if items else 1)

    def _on_empty_state_refresh(self):
        """User tapped Refresh on the empty-state — drop the cache
        and re-fetch. Same pattern as LibraryGrid's recovery."""
        try:
            disk_cache.clear(self.CACHE_NAME)
        except Exception:
            pass
        self._initial_load_complete = False
        self._content_stack.setCurrentIndex(0)
        self.load_genres()

    def _clear(self):
        """Drop the model + reset stack to the grid page. Called
        from the host on sign-out so the previous session's genres
        don't render to a now-unauthenticated user."""
        self._model.set_items([])
        self._initial_load_complete = False
        self._content_stack.setCurrentIndex(0)

    @Slot(object)
    def _on_refresh_loaded(self, items):
        """Background-refresh handler — only re-renders if the genre
        list actually changed since the cached snapshot."""
        items = items or []
        disk_cache.save(self.CACHE_NAME, self._SCOPE, items)
        if self._items_signature(items) == self._items_signature(self._model.items()):
            return
        self._model.set_items(items)

    @staticmethod
    def _items_signature(items):
        return tuple(it.get("Id", "") for it in items)
