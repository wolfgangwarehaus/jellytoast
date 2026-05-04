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

from PySide6.QtCore import Qt, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea, QSizePolicy,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.jellyfin_api import get_api
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars,
    TEXT, TEXT_DIM, TEXT_FAINT,
)
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

class LibraryTile(QFrame):
    """One library item in the grid (album or playlist). Cover + title
    + subtitle; hover reveals a centered play button overlay that's a
    child of the cover container (so it floats above the artwork
    without disturbing layout). `kind` controls the subtitle field
    — album shows artist, playlist shows track count."""

    play_requested = Signal(str)    # item_id
    browse_requested = Signal(str)  # item_id

    COVER_SIZE = 180
    OVERLAY_SIZE = 56

    def __init__(self, item: Dict, kind: str = "album",
                 show_subtitle: bool = True, parent=None):
        super().__init__(parent)
        self._item = item
        self._kind = kind
        self._item_id = item.get("Id", "")
        self._show_subtitle = show_subtitle
        # Artists hide the play overlay — "play an artist" has no
        # canonical meaning (their newest album? all tracks shuffled?).
        # The whole-tile click opens the artist page where the user
        # picks a specific album to play.
        self._show_play_overlay = (kind != "artist")
        self.setObjectName("libraryTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(self.COVER_SIZE)
        self.setStyleSheet("""
            QFrame#libraryTile { background: transparent; border: none; }
            QFrame#libraryTile QLabel { background: transparent; }
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

        # Title — bold body, single line, centered, eliding.
        self._title = _ElidingLabel(item.get("Name", "Unknown"))
        self._title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        )
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._title)

        # Year line — albums only, sits between title and subtitle.
        # ProductionYear is a string like "2010"; falls back to the
        # PremiereDate's year prefix. Hidden when neither is present
        # so the tile collapses cleanly for unscored items.
        if self._kind == "album":
            year_text = self._compute_year()
            self._year = QLabel(year_text)
            self._year.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
            )
            self._year.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._year.setVisible(bool(year_text))
            layout.addWidget(self._year)

        # Subtitle — kind-dependent. Albums show the artist; playlists
        # show track count; artists show first genre. Same caption
        # styling so the tile reads consistently across kinds. Hidden
        # via show_subtitle=False when the surrounding context already
        # makes the line redundant (e.g. all albums on an ArtistPage
        # share the same artist).
        self._subtitle = _ElidingLabel(self._compute_subtitle())
        self._subtitle.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._subtitle.setVisible(self._show_subtitle)
        layout.addWidget(self._subtitle)

        layout.addStretch(0)

    def _compute_year(self) -> str:
        # Only meaningful for album items. ProductionYear is the
        # canonical field; PremiereDate (ISO 8601) is the fallback.
        y = self._item.get("ProductionYear")
        if y:
            return str(y)
        pd = (self._item.get("PremiereDate") or "").strip()
        return pd[:4] if pd[:4].isdigit() else ""

    def _compute_subtitle(self) -> str:
        if self._kind == "playlist":
            count = self._item.get("ChildCount") or 0
            return f"{count} tracks" if count != 1 else "1 track"
        if self._kind == "artist":
            # Genres array is the most useful one-line meta; drop
            # leading-empty entries Jellyfin sometimes returns.
            genres = [g for g in (self._item.get("Genres") or []) if g]
            return genres[0] if genres else ""
        # Default (album): artist line
        return self._item.get("AlbumArtist") or ", ".join(
            self._item.get("AlbumArtists", []) or []
        ) or ""

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
        if self._show_play_overlay:
            self._play_overlay.show()
            self._play_overlay.raise_()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._play_overlay.hide()

    # ── Click → browse (play overlay handles its own click) ────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.browse_requested.emit(self._item_id)
        super().mousePressEvent(e)

    @Slot()
    def _on_play_clicked(self):
        # Play button consumes the click so it doesn't bubble to the
        # tile's mousePressEvent (which would also emit browse_requested).
        self.play_requested.emit(self._item_id)


# ── Alphabet index ──────────────────────────────────────────────────────

class _AlphabetIndex(QWidget):
    """Vertical A–Z strip on the right edge of the grid. Letters are
    subtle by default; the current letter (first character of the
    top-most visible album) renders bright. Clicking a letter emits
    jump_requested(letter) — the grid scrolls the first matching tile
    into view.

    Mirrors the iOS Music app / Jellyfin Web pattern. Inert until the
    grid wires its scroll bar + jump handlers."""

    LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    jump_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(20)
        self.setStyleSheet("background: transparent;")
        self._current = ""
        self._buttons = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, SPACE_LG, 4, SPACE_LG)
        layout.setSpacing(0)
        for ch in self.LETTERS:
            btn = QPushButton(ch)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet(self._btn_style(active=False))
            # stretch=1 so the 26 letters distribute evenly across the
            # available height — keeps the strip readable on tall and
            # short windows alike, no fixed per-letter height needed.
            btn.clicked.connect(
                lambda _checked=False, c=ch: self.jump_requested.emit(c)
            )
            layout.addWidget(btn, 1)
            self._buttons[ch] = btn

    @staticmethod
    def _btn_style(active: bool) -> str:
        if active:
            return (
                f"QPushButton {{ background: transparent; color: {TEXT}; "
                "border: none; padding: 0; font-size: 9px; font-weight: 700; }}"
                "QPushButton:hover { color: white; }"
            )
        return (
            "QPushButton { background: transparent; color: rgba(255,255,255,0.30); "
            "border: none; padding: 0; font-size: 9px; }"
            "QPushButton:hover { color: white; }"
        )

    def set_current_letter(self, letter: str):
        letter = (letter or "").upper()
        if letter == self._current:
            return
        if self._current and self._current in self._buttons:
            self._buttons[self._current].setStyleSheet(self._btn_style(active=False))
        self._current = letter
        if letter and letter in self._buttons:
            self._buttons[letter].setStyleSheet(self._btn_style(active=True))


# ── Grid ────────────────────────────────────────────────────────────────

class LibraryGrid(QWidget):
    """Responsive grid of LibraryTiles. Recomputes column count on
    resize. Async-fetches items + lazy-loads each cover via the shared
    image pipeline so scrolling stays smooth.

    `kind` controls what's fetched and how each tile is rendered:
      "album"    → IncludeItemTypes=MusicAlbum, subtitle = artist
      "playlist" → IncludeItemTypes=Playlist,   subtitle = track count
    """

    play_requested = Signal(str)
    browse_requested = Signal(str)

    # Async result lands on the GUI thread via this signal so we don't
    # touch widgets from the worker thread.
    _items_loaded = Signal(object)   # API response dict (cold load)
    _refresh_loaded = Signal(object) # API response dict (background refresh)

    TILE_WIDTH = LibraryTile.COVER_SIZE
    GAP = SPACE_LG          # 16px between tiles
    PADDING = SPACE_XL      # 24px around the grid

    _ITEM_TYPE = {"album": "MusicAlbum", "playlist": "Playlist",
                   "artist": "MusicArtist"}
    CACHE_NAME = "library"

    def __init__(self, kind: str = "album", parent=None):
        super().__init__(parent)
        self.api = get_api()
        self.kind = kind
        self._tiles: List[LibraryTile] = []
        self._current_cols = 0
        # Last load_items() args, remembered so re-sort can re-fetch
        # without forcing the host to track them. Initial sort comes
        # from Settings so the first fetch matches what the top-bar
        # restored from disk.
        from modules.settings import get_settings
        s = get_settings()
        self._parent_id: str = ""
        # Optional genre-filter — Jellyfin treats genre as a parallel
        # filter to ParentId, not a parent. Setting this re-fetches
        # with ?GenreIds=<id>, scoping the grid to that genre's items.
        self._genre_id: str = ""
        self._sort_by: str = s.library_sort_by or "SortName"
        self._sort_order: str = (
            "Descending" if s.library_sort_order == "descending" else "Ascending"
        )

        self.setObjectName("libraryGrid")
        # Sweep transparency across every descendant widget so the
        # body shows through under the scroll bar lane and any other
        # interior chrome. The application-level QSS paints every
        # QWidget with the body color by default; we override that
        # *for everything inside libraryGrid* via the descendant
        # selector. Per-widget styles on tiles / overlays still win
        # because they have higher specificity.
        self.setStyleSheet("""
            QWidget#libraryGrid,
            QWidget#libraryGrid QWidget,
            QWidget#libraryGrid QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        # Top padding so the first row of tiles doesn't crowd the bar
        # below the titlebar. The kicker label that used to live here
        # was removed — the top-bar dropdown identifies the section.
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        # Scroll area holds the tile grid.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        install_autofade_scrollbars(self._scroll)

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._container)
        self._grid_layout.setContentsMargins(
            self.PADDING, 0, self.PADDING, self.PADDING,
        )
        self._grid_layout.setHorizontalSpacing(self.GAP)
        self._grid_layout.setVerticalSpacing(self.GAP + SPACE_SM)
        # AlignTop only — column stretch (set in _reflow_grid) handles
        # horizontal distribution. Each cell gets equal stretch so the
        # leftover horizontal space spreads evenly between columns
        # rather than clumping the tiles to the left edge.
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._container)

        # Body row: scroll + alphabet index sit side-by-side. The
        # alphabet is a fixed-width strip on the right that highlights
        # as the user scrolls and offers click-to-jump.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._scroll, 1)
        self._alphabet = _AlphabetIndex()
        self._alphabet.jump_requested.connect(self._on_alphabet_jump)
        body.addWidget(self._alphabet)
        outer.addLayout(body, 1)

        # Scroll position → highlighted letter.
        self._scroll.verticalScrollBar().valueChanged.connect(
            self._on_scrolled
        )

        self._items_loaded.connect(self._on_items_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)
        # Scope tracked across the cache hit → background refresh
        # round-trip so the refresh callback knows what scope to
        # validate against and persist to.
        self._refresh_scope: dict = {}

    # ── Public API ─────────────────────────────────────────────────────

    def load_items(self, parent_id: str = "", genre_id: str = ""):
        """Async-fetch items of this grid's `kind`. `parent_id` scopes
        by library/folder; `genre_id` filters by genre (Jellyfin
        applies these in parallel — passing both narrows further).
        Empty = whole user library, recursive.

        Two-phase: if a disk cache matches the current scope, render
        from it instantly and verify against the server in the
        background. On a true cold load (no cache), fire the regular
        fetch and persist on success."""
        self._parent_id = parent_id
        self._genre_id = genre_id
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        scope = {
            "kind": self.kind,
            "parent_id": parent_id,
            "genre_id": genre_id,
            "sort_by": sort_by,
            "sort_order": self._sort_order,
        }
        self._refresh_scope = scope
        cached = disk_cache.load(self.CACHE_NAME, scope)
        if cached:
            self._clear_tiles()
            self._items_loaded.emit({"Items": cached})
            run_async(
                self.api.get_items, parent_id, item_type, 1000, 0,
                sort_by, self._sort_order, True, genre_id,
                on_result=lambda resp: self._refresh_loaded.emit(resp),
                on_error=lambda _e: None,
            )
            return
        # Cold load — clear stale tiles, fire fresh fetch, persist on
        # success.
        self._clear_tiles()
        run_async(
            self.api.get_items, parent_id, item_type, 1000, 0,
            sort_by, self._sort_order, True, genre_id,  # recursive, genre
            on_result=lambda resp: self._on_cold_fetch(resp),
            on_error=lambda _e: self._items_loaded.emit({"Items": []}),
        )

    def _on_cold_fetch(self, resp):
        """First-fetch handler when there was no cache — persist to
        disk so subsequent launches render instantly, then drive the
        normal render path."""
        items = (resp or {}).get("Items") or []
        if items and self._refresh_scope:
            disk_cache.save(self.CACHE_NAME, self._refresh_scope, items)
        self._items_loaded.emit(resp)

    @staticmethod
    def _sort_for_kind(sort_by: str, kind: str) -> str:
        """Substitute a safe fallback when the active sort key isn't
        valid for this kind. Playlists don't have AlbumArtist or
        PremiereDate fields; artists don't have AlbumArtist (they
        *are* the artist) or PremiereDate. Sorting by a missing field
        returns zero items from Jellyfin instead of failing loudly,
        so we fall back to SortName."""
        if not sort_by:
            return "SortName"
        first_key = sort_by.split(",", 1)[0]
        if kind in ("playlist", "artist") and first_key in (
            "AlbumArtist", "PremiereDate"
        ):
            return "SortName"
        return sort_by

    def set_sort(self, sort_by: str, sort_order: str):
        """Update sort criteria + re-fetch. sort_by is the Jellyfin
        SortBy string (e.g. "SortName" or "AlbumArtist,SortName");
        sort_order is the JellyToast top-bar string ("ascending" |
        "descending") which we map to Jellyfin's API casing.
        Re-fetches with the existing parent + genre scoping intact."""
        self._sort_by = sort_by or "SortName"
        self._sort_order = (
            "Descending" if sort_order == "descending" else "Ascending"
        )
        self.load_items(self._parent_id, self._genre_id)

    # ── Async result handler ───────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        for item in items:
            tile = LibraryTile(item, kind=self.kind)
            tile.play_requested.connect(self.play_requested.emit)
            tile.browse_requested.connect(self.browse_requested.emit)
            self._tiles.append(tile)
            cover_url = self.api.get_image_url(
                item.get("Id", ""), "Primary", 360,
            )
            if cover_url:
                load_image_async(
                    f"{item.get('Id')}|{self.kind}tile",
                    cover_url, 360, 360,
                    tile.set_cover, rounded_radius=8,
                )
        # Pre-compute alphabet → first matching tile index so the
        # alphabet jump is O(1) at click time. The indexed field
        # depends on the active sort: by-name uses Name, by-artist
        # uses AlbumArtist, etc. Date-based sorts don't index
        # alphabetically — _alphabet_field_for_sort returns None and
        # the alphabet hides.
        self._letter_to_tile: Dict[str, int] = {}
        for i, tile in enumerate(self._tiles):
            letter = self._index_letter_for(tile._item)
            if letter and letter.isalpha() and letter not in self._letter_to_tile:
                self._letter_to_tile[letter] = i
        # Show / hide the alphabet strip based on whether the active
        # sort is alphabetical. Hidden for date sorts.
        self._alphabet.setVisible(
            self._alphabet_field_for_sort(self._sort_by) is not None
        )
        # Force a reflow so tiles get placed in the grid.
        self._current_cols = 0
        self._reflow_grid()
        # Prime the alphabet's current letter to the first tile's.
        if self._tiles and self._alphabet.isVisible():
            letter = self._index_letter_for(self._tiles[0]._item)
            if letter:
                self._alphabet.set_current_letter(letter)

    @Slot(object)
    def _on_refresh_loaded(self, resp):
        """Background-refresh handler — fires after we already rendered
        from the disk cache. Only re-renders if the fresh result's
        item-id sequence actually differs from what's on screen, which
        keeps the typical "library hasn't changed since last launch"
        path silent (no flash, no tile re-creation). When it does
        differ we drop the existing tiles and re-render fresh."""
        items = (resp or {}).get("Items") or []
        if self._refresh_scope:
            disk_cache.save(self.CACHE_NAME, self._refresh_scope, items)
        if self._items_signature(items) == self._items_signature(
            [t._item for t in self._tiles]
        ):
            return
        self._clear_tiles()
        self._on_items_loaded({"Items": items})

    @staticmethod
    def _items_signature(items):
        """Comparable signature used to short-circuit no-change refreshes.
        Tuple of item Ids — typical edits (renames, played-flag toggles)
        don't change Ids, so the typical refresh round-trip stays silent.
        Adds / deletions / reorders do show up here and trigger a
        re-render."""
        return tuple(it.get("Id", "") for it in items)

    # ── Alphabet jump + scroll-driven highlight ─────────────────────────

    @staticmethod
    def _alphabet_field_for_sort(sort_by: str):
        """Return the item field whose first character feeds the
        alphabet index for the given Jellyfin SortBy string. Returns
        None for non-alphabetical sorts (the alphabet hides)."""
        first_key = (sort_by or "").split(",", 1)[0]
        if first_key == "SortName":
            # Empty string is a sentinel meaning "use SortName / Name
            # fallback chain in _index_letter_for".
            return ""
        if first_key == "AlbumArtist":
            return "AlbumArtist"
        # PremiereDate / DateCreated / DatePlayed — sortable but not
        # by first character.
        return None

    def _index_letter_for(self, item: dict) -> str:
        """First character to use for alphabet indexing of `item`,
        per the active sort. Empty string when there's no meaningful
        letter (date sort, or missing field)."""
        field = self._alphabet_field_for_sort(self._sort_by)
        if field is None:
            return ""
        if field:
            val = item.get(field, "") or ""
            if isinstance(val, list):
                val = val[0] if val else ""
        else:
            # Sort-by-name fallback chain — Jellyfin sometimes strips
            # leading articles ("The Beatles" → SortName "Beatles, The").
            val = item.get("SortName") or item.get("Name") or ""
        val = (val or "").strip()
        return val[0].upper() if val else ""

    @Slot(str)
    def _on_alphabet_jump(self, letter: str):
        # Walk backward through the alphabet to find a letter with an
        # entry — clicking Q on a library with no Q albums should land
        # at the last P (the closest preceding match), not no-op. If
        # nothing precedes (e.g. clicking A on an empty grid), bail.
        alphabet = _AlphabetIndex.LETTERS
        target = (letter or "").upper()
        if target not in alphabet:
            return
        idx = None
        for i in range(alphabet.index(target), -1, -1):
            candidate = self._letter_to_tile.get(alphabet[i])
            if candidate is not None and 0 <= candidate < len(self._tiles):
                idx = candidate
                break
        if idx is None:
            return
        # Scroll the tile to the *top* of the viewport — ensureWidgetVisible
        # stops scrolling as soon as the tile enters the viewport, which
        # leaves it at the bottom edge. Setting the scroll bar to the
        # tile's y (minus a small breathing margin) anchors it as the
        # first row instead.
        tile = self._tiles[idx]
        bar = self._scroll.verticalScrollBar()
        target_y = max(0, tile.y() - 12)
        bar.setValue(min(target_y, bar.maximum()))

    @Slot(int)
    def _on_scrolled(self, _value: int):
        if not self._alphabet.isVisible():
            return
        top = self._scroll.verticalScrollBar().value()
        for tile in self._tiles:
            if tile.y() + tile.height() >= top:
                # First tile whose bottom edge is at or below the
                # viewport top — that's the first visible tile.
                letter = self._index_letter_for(tile._item)
                if letter:
                    self._alphabet.set_current_letter(letter)
                return

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

    def showEvent(self, e):
        super().showEvent(e)
        # Cold-launch + cache-hit path: items render synchronously
        # during load_items, before the grid widget has been made
        # current in the host's content_stack. The synchronous reflow
        # at that moment sees a 0-width viewport and lays out one
        # column. Defer one more reflow until after Qt has laid out
        # this widget at its real visible size — _reflow_grid is a
        # no-op if the column count hasn't changed, so the cost is
        # cheap on subsequent shows.
        QTimer.singleShot(0, self._reflow_grid)

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
            # AlignHCenter so the tile sits in the middle of its
            # stretch-distributed cell — leftover horizontal space
            # spreads evenly between columns, instead of clumping the
            # tiles flush left with empty space on the right edge.
            self._grid_layout.addWidget(
                tile, row, col, Qt.AlignmentFlag.AlignHCenter,
            )
        # Each visible column gets equal stretch so the row fills the
        # available width with even gaps. Reset stretch on any extra
        # columns from a previous wider window.
        for col in range(cols):
            self._grid_layout.setColumnStretch(col, 1)
        for col in range(cols, cols + 16):
            self._grid_layout.setColumnStretch(col, 0)
