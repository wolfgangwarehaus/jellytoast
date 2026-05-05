"""
Native Search view — Phase 5 of the native-UI pivot.

Replaces JF Web's /search.html with a PySide6 surface: search input
at top, three result sections below (Songs / Albums / Artists). Each
section reuses the existing native cell — _SongRow for songs, LibraryTile
for albums + artists — so result clicks route through the same paths as
the main library views (preview NowPlayingPage / install MANUAL queue /
open ArtistPage).

Why native: search is the most-trafficked remaining JF Web surface for
a music-only user. Owning it retires the embed's last frequently-hit
interaction and lets us ship a tighter UX (debounced, type-scoped,
no JF Web round-trip).
"""

from typing import Dict, List

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QScrollArea,
)

from modules.async_io import run_async
from modules.providers import get_provider
from modules.library_grid import LibraryTile
from modules.songs_view import _SongRow
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars,
    BORDER, TEXT, TEXT_DIM, TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_SUBHEAD, TYPE_BODY, TYPE_MICRO, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


SONGS_LIMIT = 12
ALBUMS_LIMIT = 14
ARTISTS_LIMIT = 14
DEBOUNCE_MS = 300


class _Rail(QWidget):
    """Section header + horizontal scroll of LibraryTiles. Mirrors the
    rail in suggestions_view but generalised across album/artist kinds.
    Hidden until set_items lands at least one item."""

    play_requested = Signal(str)
    browse_requested = Signal(str)

    def __init__(self, label: str, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
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
        outer.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setFixedHeight(248)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        install_autofade_scrollbars(self._scroll)

        self._strip = QWidget()
        self._strip.setStyleSheet("background: transparent;")
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(SPACE_XL, 0, SPACE_XL, 0)
        self._strip_layout.setSpacing(SPACE_LG)
        self._strip_layout.addStretch(1)
        self._scroll.setWidget(self._strip)
        outer.addWidget(self._scroll)

        self._tiles: List[LibraryTile] = []

    def set_items(self, items: List[Dict]):
        for tile in self._tiles:
            self._strip_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = []

        if not items:
            self.setVisible(False)
            return

        api = get_provider()
        for item in items:
            tile = LibraryTile(item, kind=self._kind)
            tile.play_requested.connect(self.play_requested.emit)
            tile.browse_requested.connect(self.browse_requested.emit)
            self._tiles.append(tile)
            insert_at = self._strip_layout.count() - 1
            self._strip_layout.insertWidget(insert_at, tile)
            cover_url = api.get_image_url(item.get("Id", ""), "Primary", 360)
            if cover_url:
                load_image_async(
                    f"{item.get('Id')}|searchtile",
                    cover_url, 360, 360,
                    tile.set_cover, rounded_radius=8,
                )
        self.setVisible(True)


class _SongsSection(QWidget):
    """Vertical list of song rows. Click a row → install the visible
    song list as the live queue starting at that index. Self-hides
    when set_items lands an empty list."""

    play_requested = Signal(int, list)  # start_idx, items snapshot

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setVisible(False)
        self._items: List[Dict] = []
        self._rows: List[_SongRow] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        self._header = QLabel("Songs")
        self._header.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)} "
            f"padding: 0 {SPACE_XL}px;"
        )
        outer.addWidget(self._header)

        self._list = QWidget()
        self._list.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(SPACE_LG, 0, SPACE_LG, 0)
        self._list_layout.setSpacing(0)
        outer.addWidget(self._list)

    def set_items(self, items: List[Dict]):
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self._items = list(items or [])

        if not self._items:
            self.setVisible(False)
            return

        api = get_provider()
        for i, item in enumerate(self._items):
            row = _SongRow(i, item)
            row.play_requested.connect(self._on_row_clicked)
            self._rows.append(row)
            self._list_layout.addWidget(row)
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if cover_id:
                cover_url = api.get_image_url(cover_id, "Primary", 120)
                if cover_url:
                    load_image_async(
                        f"{cover_id}|searchsong",
                        cover_url, 120, 120,
                        row.set_thumb, rounded_radius=4,
                    )
        self.setVisible(True)

    @Slot(int)
    def _on_row_clicked(self, index: int):
        if 0 <= index < len(self._items):
            self.play_requested.emit(index, list(self._items))


class _SearchInput(QLineEdit):
    """QLineEdit subclass that emits dismiss_requested on Escape so the
    host can return to whichever surface called the search up. Otherwise
    Escape would just clear the input — useful but not what the user
    expects when the entire surface is the search view."""

    dismiss_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_requested.emit()
            return
        super().keyPressEvent(event)


class SearchView(QWidget):
    """Native search surface. Three result sections wired to the host's
    existing routing: songs install a MANUAL queue, albums preview into
    NowPlayingPage, artists open ArtistPage. The host wires those via
    the Signal surface below."""

    songs_play_requested = Signal(int, list)   # start_idx, items snapshot
    album_play_requested = Signal(str)         # album_id (overlay click)
    album_browse_requested = Signal(str)       # album_id (tile click)
    artist_browse_requested = Signal(str)      # artist_id
    dismiss_requested = Signal()               # close button or Esc

    _songs_loaded = Signal(object)
    _albums_loaded = Signal(object)
    _artists_loaded = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._current_query = ""

        # Per-query nonces guard against late-arriving responses for an
        # earlier query overwriting a newer query's results. Each new
        # search bumps the nonce; result handlers compare and drop stale.
        self._nonce = 0

        self.setObjectName("searchView")
        # Sweep transparency across every descendant so the scroll bar
        # lane lets the body show through.
        self.setStyleSheet("""
            QWidget#searchView,
            QWidget#searchView QWidget,
            QWidget#searchView QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Search input row — input expands, close button on the right.
        input_row = QFrame()
        input_row.setStyleSheet("background: transparent;")
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(
            SPACE_XL, SPACE_LG, SPACE_XL, SPACE_MD,
        )
        input_layout.setSpacing(SPACE_MD)

        self._input = _SearchInput()
        self._input.setPlaceholderText("Search music…")
        self._input.setClearButtonEnabled(True)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255,255,255,0.06);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 10px 14px;
                {type_qss(TYPE_BODY)}
                selection-background-color: rgba(255,255,255,0.20);
            }}
            QLineEdit:focus {{
                border-color: rgba(255,255,255,0.32);
                background: rgba(255,255,255,0.08);
            }}
        """)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.dismiss_requested.connect(self.dismiss_requested.emit)
        input_layout.addWidget(self._input, 1)

        # Close button uses the Unicode ✕ glyph — matches the settings
        # dialog pattern and avoids needing a new SVG in the icon
        # registry just for this surface.
        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(36, 36)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("Close search")
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_DIM};
                border: none;
                border-radius: 8px;
                {type_qss(TYPE_SUBHEAD)}
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.08);
                color: {TEXT};
            }}
            QPushButton:pressed {{ background: rgba(255,255,255,0.14); }}
        """)
        self._close_btn.clicked.connect(self.dismiss_requested.emit)
        input_layout.addWidget(self._close_btn)

        outer.addWidget(input_row)

        # Debounce timer — restarted on every keystroke; fires the
        # search once the user stops typing for DEBOUNCE_MS.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire_search)

        # Results column wrapped in a scroll area so long result sets
        # scroll vertically; sections themselves use horizontal scroll
        # for the rails.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        col = QVBoxLayout(self._container)
        col.setContentsMargins(0, SPACE_MD, 0, SPACE_XL)
        col.setSpacing(SPACE_MD)

        # Empty state — shown when no query, replaced by sections + a
        # "no results" label as the user types.
        self._status = QLabel("Type to search your library")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_SUBHEAD)} "
            f"padding: {SPACE_XL * 2}px {SPACE_XL}px;"
        )
        col.addWidget(self._status)

        self._songs_section = _SongsSection()
        self._songs_section.play_requested.connect(self.songs_play_requested.emit)
        col.addWidget(self._songs_section)

        self._albums_rail = _Rail("Albums", kind="album")
        self._albums_rail.play_requested.connect(self.album_play_requested.emit)
        self._albums_rail.browse_requested.connect(self.album_browse_requested.emit)
        col.addWidget(self._albums_rail)

        # Artists rail — kind="artist" suppresses the play overlay (no
        # canonical "play an artist" action), so only browse fires.
        self._artists_rail = _Rail("Artists", kind="artist")
        self._artists_rail.browse_requested.connect(self.artist_browse_requested.emit)
        col.addWidget(self._artists_rail)

        col.addStretch(1)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._songs_loaded.connect(self._on_songs_loaded)
        self._albums_loaded.connect(self._on_albums_loaded)
        self._artists_loaded.connect(self._on_artists_loaded)

    def focus_input(self):
        """Host calls this when swapping the surface in so the user can
        type immediately without an extra click on the input."""
        self._input.setFocus()
        self._input.selectAll()

    def reset(self):
        """Clear the input + result sections — used when the host is
        about to show search fresh (e.g. after dismissing an old query)."""
        self._input.clear()
        self._show_empty_state("Type to search your library")

    def _show_empty_state(self, text: str):
        self._status.setText(text)
        self._status.setVisible(True)
        self._songs_section.setVisible(False)
        self._albums_rail.setVisible(False)
        self._artists_rail.setVisible(False)

    def _hide_status(self):
        self._status.setVisible(False)

    @Slot(str)
    def _on_text_changed(self, text: str):
        self._current_query = text.strip()
        if not self._current_query:
            self._debounce.stop()
            self._show_empty_state("Type to search your library")
            return
        self._debounce.start()

    def _fire_search(self):
        query = self._current_query
        if not query:
            return
        self._nonce += 1
        nonce = self._nonce
        self._show_empty_state("Searching…")

        # Three parallel calls — one per kind — so each section gets a
        # deterministic per-type cap rather than competing for slots in
        # a single mixed limit.
        run_async(
            self.api.search, query, SONGS_LIMIT, "Audio",
            on_result=lambda items, n=nonce: self._songs_loaded.emit((n, items or [])),
            on_error=lambda _e, n=nonce: self._songs_loaded.emit((n, [])),
        )
        run_async(
            self.api.search, query, ALBUMS_LIMIT, "MusicAlbum",
            on_result=lambda items, n=nonce: self._albums_loaded.emit((n, items or [])),
            on_error=lambda _e, n=nonce: self._albums_loaded.emit((n, [])),
        )
        run_async(
            self.api.search, query, ARTISTS_LIMIT, "MusicArtist",
            on_result=lambda items, n=nonce: self._artists_loaded.emit((n, items or [])),
            on_error=lambda _e, n=nonce: self._artists_loaded.emit((n, [])),
        )

    def _stale(self, payload) -> bool:
        nonce, _items = payload
        return nonce != self._nonce

    @Slot(object)
    def _on_songs_loaded(self, payload):
        if self._stale(payload):
            return
        _, items = payload
        self._songs_section.set_items(items)
        self._maybe_clear_status()

    @Slot(object)
    def _on_albums_loaded(self, payload):
        if self._stale(payload):
            return
        _, items = payload
        self._albums_rail.set_items(items)
        self._maybe_clear_status()

    @Slot(object)
    def _on_artists_loaded(self, payload):
        if self._stale(payload):
            return
        _, items = payload
        self._artists_rail.set_items(items)
        self._maybe_clear_status()

    def _maybe_clear_status(self):
        any_visible = (
            self._songs_section.isVisible()
            or self._albums_rail.isVisible()
            or self._artists_rail.isVisible()
        )
        if any_visible:
            self._hide_status()
        else:
            # All three landed empty — show "no results". This works
            # because every search fires all three; whichever lands
            # last triggers this branch.
            self._show_empty_state(f'No results for "{self._current_query}"')

    def keyPressEvent(self, event: QKeyEvent):
        # Defense-in-depth: if focus has wandered off the input, Escape
        # on the surface itself still dismisses.
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_requested.emit()
            return
        super().keyPressEvent(event)
