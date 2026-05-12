"""
Horizontal rail widget — header label + horizontal scroll of tiles
built on QListView + QAbstractListModel + QStyledItemDelegate.

Used by search_view and suggestions_view to render horizontal rails
of album / artist / playlist tiles. Reuses library_grid's
``_LibraryItemsModel`` and ``_TileDelegate`` so the rendering matches
the rest of the music surfaces exactly — same paint, same hover,
same hit-test logic.

Hidden until ``set_items`` lands at least one item; an empty rail
collapses cleanly so the surrounding layout doesn't leave a labeled
void.
"""

from typing import Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout,
    QAbstractItemView, QListView,
)

from modules.library_grid import (
    _LibraryItemsModel, _TileDelegate, _artist_id_for_album,
)
from modules.providers import get_provider
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars, screen_dpr,
    TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_MICRO, apply_type, type_qss,
    SPACE_SM, SPACE_LG, SPACE_XL,
)


class _RailListView(QListView):
    """Horizontal QListView for rails. IconMode, no wrapping,
    horizontal scroll. mousePressEvent hit-tests the delegate's
    overlay / subtitle sub-rects so the play / artist click routes
    each fire their own signal."""

    play_clicked = Signal(str)             # item_id
    browse_clicked = Signal(str)           # item_id
    artist_browse_clicked = Signal(str)    # artist_id

    def __init__(self, delegate: _TileDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate
        self.setItemDelegate(delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        # Viewport transparency — flatten first-paint to dodge the
        # default palette-Base lightgrey flash on cold show.
        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self.setStyleSheet(
            "QListView { background: transparent; border: none; }"
        )

    def wheelEvent(self, e):
        """Wheel handling: vertical scroll (no modifier) goes to the
        outer vertical scroll area so the user can navigate between
        rails. Horizontal rail scroll only fires when Shift is held —
        matches the macOS / "scroll modifier" convention familiar to
        any user with a regular mouse wheel."""
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            super().wheelEvent(e)
            return
        # No shift — pass through to parent so the outer vertical
        # scroll moves. e.ignore() lets Qt walk the parent chain.
        e.ignore()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(e)
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            super().mousePressEvent(e)
            return
        item = idx.data(_LibraryItemsModel.ItemRole) or {}
        item_id = item.get("Id", "")
        cell = self.visualRect(idx)
        # Hit-test order: overlay → subtitle → fall through (browse).
        # Year line is suppressed in rails (show_year=False) so no
        # year hit-test needed.
        if (self._delegate._show_play_overlay
                and self._delegate.overlay_rect_for(cell).contains(pos)
                and item_id):
            self.play_clicked.emit(item_id)
            e.accept()
            return
        sub_rect = self._delegate.subtitle_rect_for(cell, item)
        if sub_rect.contains(pos):
            aid = (_artist_id_for_album(item)
                   if self._delegate._kind == "album" else "")
            if aid:
                self.artist_browse_clicked.emit(aid)
                e.accept()
                return
        if item_id:
            self.browse_clicked.emit(item_id)
            e.accept()
            return
        super().mousePressEvent(e)


class HorizontalRail(QWidget):
    """Reusable rail widget — header label + horizontal scroll of
    tiles. Uses _LibraryItemsModel + _TileDelegate so the rendering
    matches the rest of the music surfaces (LibraryGrid, GenresView,
    SongsView). Hidden until ``set_items`` lands at least one item."""

    play_requested = Signal(str)
    browse_requested = Signal(str)
    artist_browse_requested = Signal(str)

    HEIGHT = 248  # tile + caption stack + breathing room

    def __init__(self, label: str, kind: str,
                 show_year: bool = False, show_subtitle: bool = True,
                 cache_namespace: str = "railtile", parent=None):
        super().__init__(parent)
        self._kind = kind
        self._cache_ns = cache_namespace
        self.setStyleSheet("background: transparent;")
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        self._header = QLabel(label)
        self._header.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)} "
            f"padding: 0 {SPACE_XL}px;"
        )
        apply_type(self._header, TYPE_MICRO)
        outer.addWidget(self._header)

        self._model = _LibraryItemsModel(self)
        self._delegate = _TileDelegate(
            kind, self,
            show_year=show_year, show_subtitle=show_subtitle,
        )
        self._view = _RailListView(self._delegate, self)
        self._view.setModel(self._model)
        self._view.setFixedHeight(self.HEIGHT)
        self._view.setViewportMargins(SPACE_XL, 0, SPACE_XL, 0)
        install_autofade_scrollbars(self._view)
        outer.addWidget(self._view)

        # Live-accent: delegate re-reads TEXT on every paint, so a
        # theme change just needs a viewport invalidate. (No accent
        # color on tiles — text-only — so this is for theme-mode
        # parity.)
        from modules.player_state import PlayerBus
        PlayerBus.get().theme_changed.connect(self._view.viewport().update)

        self._view.play_clicked.connect(self.play_requested.emit)
        self._view.browse_clicked.connect(self.browse_requested.emit)
        self._view.artist_browse_clicked.connect(
            self.artist_browse_requested.emit
        )

    def set_items(self, items: List[Dict]):
        items = items or []
        self._model.set_items(items)
        if not items:
            self.setVisible(False)
            return
        self.setVisible(True)
        # Kick cover loads for each item — same DPR-aware sizing as
        # LibraryGrid so cache slots overlap across surfaces.
        api = get_provider()
        dpr = screen_dpr(self)
        target_phys = max(
            _TileDelegate.COVER_SIZE,
            int(round(_TileDelegate.COVER_SIZE * dpr)),
        )
        radius_phys = int(round(_TileDelegate.COVER_RADIUS * dpr))
        server_px = max(360, target_phys)
        for row, item in enumerate(items):
            cover_id = item.get("Id", "")
            if not cover_id:
                continue
            cover_url = api.get_image_url(cover_id, "Primary", server_px)
            if not cover_url:
                continue

            def _on_pix(pix, r=row):
                self._model.set_cover(r, pix)

            load_image_async(
                f"{cover_id}|{self._cache_ns}",
                cover_url, target_phys, target_phys,
                _on_pix, rounded_radius=radius_phys,
                on_error=lambda: None,
            )

    def first_focusable(self):
        return self._view if self._model.rowCount() > 0 else None
