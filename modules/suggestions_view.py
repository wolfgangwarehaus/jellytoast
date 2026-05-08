"""
Native Suggestions ("Discover") view — Phase 5 of the native-UI pivot.

Replaces JF Web's Music → Suggestions tab with a vertical stack of
horizontally-scrolling album rails:

  - Latest             → newest albums in the library
  - Favorites          → starred / favorited albums
  - Recently played    → albums sorted by last-played, played-only
  - Frequently played  → albums sorted by play count, played-only
  - Random             → fresh shuffle of the catalog each visit

Each rail reuses LibraryTile (kind="album"), so browse + play-overlay
clicks route through the same paths as the main album grid.

A new user with no play history will see the Latest rail populated
and the played rails empty — those rails hide themselves when their
fetch returns nothing so the surface stays uncluttered.
"""

from typing import Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QScrollArea,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.providers import get_provider
from modules.library_grid import LibraryTile
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars, TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_MICRO, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


RAIL_LIMIT = 12


class _Rail(QWidget):
    """One horizontal rail: kicker label + horizontal scroll of album
    tiles. Hidden until set_items() lands at least one item — keeps
    empty rails (e.g. Recently Played on a fresh account) from
    leaving a labeled void."""

    play_requested = Signal(str)
    browse_requested = Signal(str)
    artist_browse_requested = Signal(str)

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label_text = label
        self.setStyleSheet("background: transparent;")
        # Hidden by default; populated rails reveal themselves.
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

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Tile (180px) + caption + year + artist ≈ 248px — give the
        # scroll area enough height for the whole tile column.
        self._scroll.setFixedHeight(248)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
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
        # Drop any existing tiles before repopulating (re-entry on
        # tab-back happens via the parent's load() call).
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
            # show_year=False so the artist subtitle takes the year's
            # vertical slot — Suggestions tiles read as "title / artist"
            # rather than "title / year / artist", matching the visual
            # density typical music apps use on rails.
            tile = LibraryTile(
                item, kind="album", show_year=False, parent=self._strip,
            )
            tile.play_requested.connect(self.play_requested.emit)
            tile.browse_requested.connect(self.browse_requested.emit)
            tile.artist_browse_requested.connect(
                self.artist_browse_requested.emit
            )
            self._tiles.append(tile)
            # Insert above the trailing stretch so tiles flow left.
            insert_at = self._strip_layout.count() - 1
            self._strip_layout.insertWidget(insert_at, tile)
            tile.show()
            cover_url = api.get_image_url(item.get("Id", ""), "Primary", 360)
            if cover_url:
                load_image_async(
                    f"{item.get('Id')}|suggesttile",
                    cover_url, 360, 360,
                    tile.set_cover, rounded_radius=8,
                )
        self.setVisible(True)


class SuggestionsView(QWidget):
    """Vertical stack of three horizontal album rails. The host wires
    play/browse signals to the existing album-play and now-playing
    routes so tile clicks behave identically to the album grid."""

    play_requested = Signal(str)    # album_id → host's _on_grid_play_album
    browse_requested = Signal(str)  # album_id → host's _show_now_playing(preview)
    artist_browse_requested = Signal(str)  # artist_id → host's _show_artist_page

    _latest_loaded = Signal(object)
    _favorites_loaded = Signal(object)
    _recent_loaded = Signal(object)
    _frequent_loaded = Signal(object)
    _random_loaded = Signal(object)

    HEADER_LABEL = "SUGGESTIONS"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._parent_id = ""

        self.setObjectName("suggestionsView")
        # Sweep transparency across every descendant so the scroll bar
        # lane lets the body show through.
        self.setStyleSheet("""
            QWidget#suggestionsView,
            QWidget#suggestionsView QWidget,
            QWidget#suggestionsView QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        # Top padding so the first rail's section label doesn't crowd
        # the bar above. Top-level "SUGGESTIONS" kicker was removed —
        # the per-rail labels (Latest / Recently played / Frequently
        # played) carry the section identity.
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        # Outer vertical scroll wraps the rails so the column itself
        # scrolls when there's not enough vertical room (small windows
        # or future rails pushing past the viewport).
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget(self._scroll)
        self._container.setStyleSheet("background: transparent;")
        col = QVBoxLayout(self._container)
        col.setContentsMargins(0, 0, 0, SPACE_XL)
        col.setSpacing(SPACE_MD)

        self._latest = _Rail("Latest")
        self._favorites = _Rail("Favorites")
        self._recent = _Rail("Recently played")
        self._frequent = _Rail("Frequently played")
        self._random = _Rail("Random")
        for rail in (self._latest, self._favorites, self._recent,
                     self._frequent, self._random):
            rail.play_requested.connect(self.play_requested.emit)
            rail.browse_requested.connect(self.browse_requested.emit)
            rail.artist_browse_requested.connect(
                self.artist_browse_requested.emit
            )
            col.addWidget(rail)
        col.addStretch(1)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._latest_loaded.connect(self._latest.set_items)
        self._favorites_loaded.connect(self._favorites.set_items)
        self._recent_loaded.connect(self._recent.set_items)
        self._frequent_loaded.connect(self._frequent.set_items)
        self._random_loaded.connect(self._random.set_items)

    # Cache name per rail. Rails fetch in parallel and we render
    # whichever lands first, so each gets its own cache entry rather
    # than one combined "suggestions" payload that would have to wait
    # for all three to finish before saving.
    CACHE_LATEST = "suggestions_latest"
    CACHE_FAVORITES = "suggestions_favorites"
    CACHE_RECENT = "suggestions_recent"
    CACHE_FREQUENT = "suggestions_frequent"
    # Random rail intentionally has no cache — we want a fresh shuffle
    # every visit, so seeding from a stale snapshot would defeat the
    # rail's purpose.

    def load(self, parent_id: str = ""):
        """Async-fetch all three rails. Parent_id scopes to the music
        library so non-music collections don't pollute the
        recommendations. Empty parent_id falls back to the user's
        whole library — acceptable when library resolution is still
        pending but should be rare in practice.

        Each rail tries the disk cache first so a cold launch shows
        rails populated immediately, then refreshes from the server
        in the background and replaces if the rail's items changed."""
        self._parent_id = parent_id
        scope = {"parent_id": parent_id}

        for cache_name, signal in (
            (self.CACHE_LATEST, self._latest_loaded),
            (self.CACHE_FAVORITES, self._favorites_loaded),
            (self.CACHE_RECENT, self._recent_loaded),
            (self.CACHE_FREQUENT, self._frequent_loaded),
        ):
            cached = disk_cache.load(cache_name, scope)
            if cached:
                signal.emit(cached)

        # Latest — uses /Users/{id}/Items/Latest (Jellyfin's curated
        # "newly added" endpoint, returns items unwrapped, not in the
        # standard Items envelope). Subsonic maps to
        # getAlbumList2?type=newest.
        run_async(
            self.api.get_latest_media, parent_id, RAIL_LIMIT,
            on_result=lambda items: self._on_rail_loaded(
                self.CACHE_LATEST, scope, self._latest_loaded, items or [],
            ),
            on_error=lambda _e: self._latest_loaded.emit([]),
        )

        # Favorites — IsFavorite filter scopes to the user's starred
        # albums. Subsonic maps this to getAlbumList2?type=starred.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "SortName", "Ascending", True, "", "IsFavorite",
            on_result=lambda resp: self._on_rail_loaded(
                self.CACHE_FAVORITES, scope, self._favorites_loaded,
                (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._favorites_loaded.emit([]),
        )

        # Recently played — sort by DatePlayed desc, IsPlayed filter so
        # we only get items the user has actually heard.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "DatePlayed,SortName", "Descending", True, "", "IsPlayed",
            on_result=lambda resp: self._on_rail_loaded(
                self.CACHE_RECENT, scope, self._recent_loaded,
                (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._recent_loaded.emit([]),
        )

        # Frequently played — sort by PlayCount desc, IsPlayed filter
        # so the rail isn't dominated by zero-count albums (Jellyfin
        # would otherwise place them all together at the bottom).
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "PlayCount,SortName", "Descending", True, "", "IsPlayed",
            on_result=lambda resp: self._on_rail_loaded(
                self.CACHE_FREQUENT, scope, self._frequent_loaded,
                (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._frequent_loaded.emit([]),
        )

        # Random — fresh shuffle each visit, no disk cache. Subsonic
        # maps SortBy=Random to getAlbumList2?type=random; Jellyfin
        # accepts SortBy=Random natively.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "Random", "Ascending", True, "", "",
            on_result=lambda resp: self._random_loaded.emit(
                (resp or {}).get("Items") or []
            ),
            on_error=lambda _e: self._random_loaded.emit([]),
        )

    def _on_rail_loaded(self, cache_name: str, scope: dict, signal,
                        items: list):
        """Persist a rail's fresh items and emit them. set_items on
        each rail is idempotent — if the items are identical to what's
        rendered, the rail will rebuild but it's a small payload
        (RAIL_LIMIT = 20 tiles) so the flicker is negligible compared
        to the win on cold launch."""
        if items:
            disk_cache.save(cache_name, scope, items)
        signal.emit(items)
