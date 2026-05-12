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
from PySide6.QtGui import QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QScrollArea,
)

from modules.async_io import run_async
from modules.providers import get_provider
from modules.library_grid import LibraryTile
from modules.songs_view import _SongRow
from modules.sort_utils import article_stripped_key
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars, screen_dpr,
    BORDER, TEXT, TEXT_DIM, TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_SUBHEAD, TYPE_BODY, TYPE_MICRO, apply_type, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


SONGS_LIMIT = 12
ALBUMS_LIMIT = 14
ARTISTS_LIMIT = 14
DEBOUNCE_MS = 300


def _name_score(name: str, query: str) -> int:
    """How strongly an item's name matches the query. Higher is
    better; 0 means no meaningful name match (the server may have
    matched on metadata like artist or album, but the item's own
    name doesn't reflect the query).

    Tiers:
      100  exact match               ("feist" == "feist")
       80  prefix match               ("sufjan" → "sufjan stevens")
       60  word-start match           ("die" → "let it die")
       40  substring match            ("bee" → "the beekeeper")
        0  no name match

    Strings are normalized via ``article_stripped_key`` so casing,
    diacritics, and leading articles ("The Beatles") don't matter.
    Used by SearchView to reorder result sections by relevance — a
    section whose top item scores 100 floats above one whose top
    item scores 0."""
    n = article_stripped_key(name)
    q = article_stripped_key(query)
    if not n or not q:
        return 0
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if any(part.startswith(q) for part in n.split()):
        return 60
    if q in n:
        return 40
    return 0


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
        apply_type(self._header, TYPE_MICRO)
        outer.addWidget(self._header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setFixedHeight(248)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        install_autofade_scrollbars(self._scroll)

        self._strip = QWidget(self._scroll)
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
            # show_year=False matches the Suggestions rail density —
            # tiles read as "title / artist" so the two horizontal-rail
            # surfaces feel consistent.
            tile = LibraryTile(
                item, kind=self._kind, show_year=False, parent=self._strip,
            )
            tile.play_requested.connect(self.play_requested.emit)
            tile.browse_requested.connect(self.browse_requested.emit)
            self._tiles.append(tile)
            insert_at = self._strip_layout.count() - 1
            self._strip_layout.insertWidget(insert_at, tile)
            tile.show()
            # Match LibraryTile's DPR-aware request size — shares
            # cache slots with the album-grid view.
            from modules.library_grid import LibraryTile as _LT
            dpr = screen_dpr(self)
            target_phys = max(_LT.COVER_SIZE, int(round(_LT.COVER_SIZE * dpr)))
            radius_phys = int(round(8 * dpr))
            server_px = max(360, target_phys)
            cover_url = api.get_image_url(item.get("Id", ""), "Primary", server_px)
            if cover_url:
                load_image_async(
                    f"{item.get('Id')}|searchtile",
                    cover_url, target_phys, target_phys,
                    tile.set_cover, rounded_radius=radius_phys,
                )
        self.setVisible(True)

    def first_focusable(self):
        return self._tiles[0] if self._tiles else None


class _SongsSection(QWidget):
    """Vertical list of song rows. Click a row → install the visible
    song list as the live queue starting at that index. Self-hides
    when set_items lands an empty list."""

    play_requested = Signal(int, list)  # start_idx, items snapshot
    album_browse_requested = Signal(str)  # album_id

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
        apply_type(self._header, TYPE_MICRO)
        outer.addWidget(self._header)

        self._list = QWidget(self)
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
            row.album_browse_requested.connect(self.album_browse_requested.emit)
            self._rows.append(row)
            self._list_layout.addWidget(row)
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if cover_id:
                # Match _SongRow.set_thumb's DPR contract so the cache
                # slot stores the physical-sized pixmap.
                from modules.songs_view import _SongRow
                dpr = screen_dpr(self)
                target_phys = max(_SongRow.THUMB_SIZE, int(round(_SongRow.THUMB_SIZE * dpr)))
                radius_phys = int(round(4 * dpr))
                server_px = max(120, target_phys)
                cover_url = api.get_image_url(cover_id, "Primary", server_px)
                if cover_url:
                    load_image_async(
                        f"{cover_id}|searchsong",
                        cover_url, target_phys, target_phys,
                        row.set_thumb, rounded_radius=radius_phys,
                    )
        self.setVisible(True)

    @Slot(int)
    def _on_row_clicked(self, index: int):
        if 0 <= index < len(self._items):
            self.play_requested.emit(index, list(self._items))

    def first_focusable(self):
        return self._rows[0] if self._rows else None


class _SearchInput(QLineEdit):
    """QLineEdit subclass that emits dismiss_requested on Escape so the
    host can return to whichever surface called the search up.
    Additionally:
      - Enter / Return → ``submit_requested`` so SearchView can fire
        the search immediately, bypassing the typing debounce.
      - Down arrow → ``focus_first_result_requested`` so SearchView
        can move keyboard focus into the result column."""

    dismiss_requested = Signal()
    submit_requested = Signal()
    focus_first_result_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_requested.emit()
            return
        if event.key() == Qt.Key.Key_Down:
            self.focus_first_result_requested.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submit_requested.emit()
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

    _all_loaded = Signal(object)

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
        self._input.submit_requested.connect(self._fire_immediately)
        self._input.focus_first_result_requested.connect(
            self._focus_first_result
        )
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
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget(self._scroll)
        self._container.setStyleSheet("background: transparent;")
        col = QVBoxLayout(self._container)
        col.setContentsMargins(0, SPACE_MD, 0, SPACE_XL)
        col.setSpacing(SPACE_MD)
        # Saved for the per-query relevance reorder — see _reorder_sections.
        self._sections_layout = col

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
        self._songs_section.album_browse_requested.connect(
            self.album_browse_requested.emit
        )
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

        self._all_loaded.connect(self._on_all_loaded)

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

    def _fire_immediately(self):
        """Enter in the input bypasses the typing debounce so users can
        commit a query they've already finished typing without waiting
        for the timer."""
        self._debounce.stop()
        self._fire_search()

    def _focus_first_result(self):
        """Move focus from the input into the first focusable result,
        in display order. Called when the user presses Down arrow in
        the input. Quietly no-ops when no results have rendered yet.

        Hero is itself the focus target; sections expose ``first_focusable``
        so we can dive into their child rows/tiles."""
        for i in range(self._sections_layout.count()):
            item = self._sections_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is None or widget is self._status:
                continue
            if not widget.isVisible():
                continue
            getter = getattr(widget, "first_focusable", None)
            if getter is not None:
                target = getter()
                if target is not None:
                    target.setFocus()
                    return
            elif widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
                widget.setFocus()
                return

    def _fire_search(self):
        query = self._current_query
        if not query:
            return
        self._nonce += 1
        nonce = self._nonce
        self._show_empty_state("Searching…")

        # Single multi-type fetch. On Subsonic this is one search3
        # round-trip; on Jellyfin it's three sequential per-type calls
        # under the hood, but the SearchView side still renders all
        # three sections in one state transition.
        run_async(
            self.api.search_all, query,
            SONGS_LIMIT, ALBUMS_LIMIT, ARTISTS_LIMIT,
            on_result=lambda buckets, n=nonce:
                self._all_loaded.emit((n, buckets or {})),
            on_error=lambda _e, n=nonce:
                self._all_loaded.emit((n, {})),
        )

    def _stale(self, payload) -> bool:
        nonce, _items = payload
        return nonce != self._nonce

    @Slot(object)
    def _on_all_loaded(self, payload):
        if self._stale(payload):
            return
        _, buckets = payload
        print(f"[search] q={self._current_query!r} buckets: Audio={len(buckets.get('Audio') or [])} MusicAlbum={len(buckets.get('MusicAlbum') or [])} MusicArtist={len(buckets.get('MusicArtist') or [])}", flush=True)
        self._songs_section.set_items(buckets.get("Audio") or [])
        self._albums_rail.set_items(buckets.get("MusicAlbum") or [])
        self._artists_rail.set_items(buckets.get("MusicArtist") or [])
        self._reorder_sections(self._current_query, buckets)
        self._maybe_clear_status()

    def _reorder_sections(self, query: str, buckets: dict):
        """Sort the three result sections by how strongly each one's
        top item matches the query. Section with the strongest name
        match floats to the top — so typing "Feist" puts Artists
        first, while "Mushaboom" puts Songs first.

        Empty sections sink to the bottom (their score is -1) but are
        kept in the layout so the next query can repopulate them in
        place. The trailing layout stretch is left untouched."""
        # Default tiebreak: Artists, then Albums, then Songs. This is
        # the order Python's stable sort falls back to when scores
        # tie, and it matches the user-mental hierarchy "more
        # specific entity first" — typing "Feist" lands the Artist
        # tile above the Feist songs/albums even though those score 0
        # on name match.
        section_data = [
            (self._artists_rail, buckets.get("MusicArtist") or []),
            (self._albums_rail, buckets.get("MusicAlbum") or []),
            (self._songs_section, buckets.get("Audio") or []),
        ]

        def section_score(pair):
            _, items = pair
            if not items:
                return -1
            return max(
                _name_score(it.get("Name", "") or "", query)
                for it in items
            )

        section_data.sort(key=section_score, reverse=True)

        # Reseat each section between the status label and the
        # trailing stretch. removeWidget keeps the widget alive — only
        # its layout slot moves.
        status_idx = self._sections_layout.indexOf(self._status)
        for widget, _ in section_data:
            self._sections_layout.removeWidget(widget)
        for offset, (widget, _) in enumerate(section_data):
            self._sections_layout.insertWidget(
                status_idx + 1 + offset, widget,
            )

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
