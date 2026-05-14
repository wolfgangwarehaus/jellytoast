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

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.providers import get_provider
from modules.ui_helpers import (
    install_autofade_scrollbars, EmptyState,
)
from modules.design_tokens import (
    SPACE_MD, SPACE_LG, SPACE_XL,
)


RAIL_LIMIT = 12

# Rail keys for the per-rail completion tracker. Used by
# _mark_rail_done to figure out whether the empty/error state
# should still be shown after every rail has reported in.
_RAIL_KEYS = ("latest", "favorites", "recent", "frequent", "random")


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
        # StrongFocus so the view itself receives keyboard focus when
        # the host calls setFocus on it after _show_suggestions_view.
        # keyPressEvent intercepts Down to dive into the first visible
        # rail's first tile — keyboard parity for the visual flow
        # "head of page → first item".
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
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
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget(self._scroll)
        self._container.setStyleSheet("background: transparent;")
        col = QVBoxLayout(self._container)
        col.setContentsMargins(0, 0, 0, SPACE_XL)
        col.setSpacing(SPACE_MD)

        # Use the shared HorizontalRail (model/view/delegate based) so
        # rendering matches the rest of the music surfaces. show_year=
        # False keeps the rail compact (title / artist), same as the
        # legacy LibraryTile-based rail it replaces.
        from modules.horizontal_rail import HorizontalRail
        self._latest = HorizontalRail(
            "Latest", kind="album", cache_namespace="suggesttile",
        )
        self._favorites = HorizontalRail(
            "Favorites", kind="album", cache_namespace="suggesttile",
        )
        self._recent = HorizontalRail(
            "Recently played", kind="album", cache_namespace="suggesttile",
        )
        self._frequent = HorizontalRail(
            "Frequently played", kind="album", cache_namespace="suggesttile",
        )
        self._random = HorizontalRail(
            "Random", kind="album", cache_namespace="suggesttile",
        )
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
        # Cached rail list so sign-out can iterate and clear cleanly.
        self._rails = (
            self._latest, self._favorites, self._recent,
            self._frequent, self._random,
        )

        # Loading / empty / error overlay. Lives in the same scroll
        # container so it sits where the rails would; rails hide
        # themselves until they have items, so this is what the user
        # sees on cold launch or after a sign-out clear. Wired into
        # the layout *above* the rails so it reads as a centered
        # in-page status when nothing else is visible.
        self._status = EmptyState(
            glyph="♪",
            headline="Loading suggestions…",
            sub="",
            action_label=None,
        )
        col.insertWidget(0, self._status)
        self._status.action_clicked.connect(self._retry_load)
        # Per-rail completion tracker. Each rail reports "loaded"
        # (got items), "empty" (got zero items), or "errored" once
        # its async fetch resolves. When all five have reported,
        # _maybe_show_status decides what (if anything) to show.
        self._rail_status: dict[str, str] = {k: "pending" for k in _RAIL_KEYS}

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

    def _clear(self):
        """Drop every rail's items. Called from the host on sign-out
        so the previous session's suggestions don't render to an
        unauthenticated user. Rails auto-hide on empty data (see
        _HorizontalRail.set_items), so this also visually collapses
        the view to a blank page until the next load lands."""
        for rail in getattr(self, "_rails", ()):
            try:
                rail.set_items([])
            except Exception:
                pass

    def load(self, parent_id: str = ""):
        """Async-fetch all five rails. Parent_id scopes to the music
        library so non-music collections don't pollute the
        recommendations. Empty parent_id falls back to the user's
        whole library — acceptable when library resolution is still
        pending but should be rare in practice.

        Each rail tries the disk cache first so a cold launch shows
        rails populated immediately, then refreshes from the server
        in the background and replaces if the rail's items changed."""
        self._parent_id = parent_id
        scope = {"parent_id": parent_id}

        # Reset the completion tracker and show the loading state.
        # If a cache hit lands a non-empty rail before any network
        # fetch returns, _mark_rail_done will hide the status as soon
        # as that rail reports "loaded" — so a warm-cache user sees
        # the loading copy only for a frame, not the full network
        # round-trip.
        self._rail_status = {k: "pending" for k in _RAIL_KEYS}
        self._status.set_state(
            glyph="♪",
            headline="Loading suggestions…",
            sub="",
            action_label="",
        )
        self._status.setVisible(True)

        for cache_name, signal, rail_key in (
            (self.CACHE_LATEST, self._latest_loaded, "latest"),
            (self.CACHE_FAVORITES, self._favorites_loaded, "favorites"),
            (self.CACHE_RECENT, self._recent_loaded, "recent"),
            (self.CACHE_FREQUENT, self._frequent_loaded, "frequent"),
        ):
            cached = disk_cache.load(cache_name, scope)
            if cached:
                signal.emit(cached)
                # Treat a cache hit as a tentative "loaded" — the live
                # fetch below will overwrite this state when it lands.
                self._mark_rail_done(rail_key, items=cached, errored=False)

        # Latest — Jellyfin's /Users/{id}/Items/Latest, Subsonic's
        # getAlbumList2?type=newest.
        run_async(
            self.api.get_latest_media, parent_id, RAIL_LIMIT,
            on_result=lambda items: self._on_rail_loaded(
                "latest", self.CACHE_LATEST, scope,
                self._latest_loaded, items or [],
            ),
            on_error=lambda _e: self._on_rail_error(
                "latest", self._latest_loaded,
            ),
        )

        # Favorites — IsFavorite filter.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "SortName", "Ascending", True, "", "IsFavorite",
            on_result=lambda resp: self._on_rail_loaded(
                "favorites", self.CACHE_FAVORITES, scope,
                self._favorites_loaded, (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._on_rail_error(
                "favorites", self._favorites_loaded,
            ),
        )

        # Recently played — DatePlayed desc, IsPlayed filter.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "DatePlayed,SortName", "Descending", True, "", "IsPlayed",
            on_result=lambda resp: self._on_rail_loaded(
                "recent", self.CACHE_RECENT, scope,
                self._recent_loaded, (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._on_rail_error(
                "recent", self._recent_loaded,
            ),
        )

        # Frequently played — PlayCount desc, IsPlayed filter.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "PlayCount,SortName", "Descending", True, "", "IsPlayed",
            on_result=lambda resp: self._on_rail_loaded(
                "frequent", self.CACHE_FREQUENT, scope,
                self._frequent_loaded, (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._on_rail_error(
                "frequent", self._frequent_loaded,
            ),
        )

        # Random — fresh shuffle every visit, no disk cache.
        run_async(
            self.api.get_items,
            parent_id, "MusicAlbum", RAIL_LIMIT, 0,
            "Random", "Ascending", True, "", "",
            on_result=lambda resp: self._on_rail_loaded(
                "random", None, scope, self._random_loaded,
                (resp or {}).get("Items") or [],
            ),
            on_error=lambda _e: self._on_rail_error(
                "random", self._random_loaded,
            ),
        )

    def focus_first_item(self):
        """Drop keyboard focus on the first visible rail's first tile.
        Host calls this when the suggestions surface is first shown
        so arrow keys + Enter "just work" without an extra click.
        Falls back to keeping focus on the view itself if no rail has
        landed items yet."""
        for rail in self._rails:
            if not rail.isVisible():
                continue
            getter = getattr(rail, "first_focusable", None)
            if getter is None:
                continue
            target = getter()
            if target is not None:
                target.setFocus()
                return

    def keyPressEvent(self, e):
        # Down at the page level dives into the first rail — same
        # contract as search's "Down from input → first result". Any
        # other key falls through to the default handler.
        if e.key() == Qt.Key.Key_Down and not e.modifiers():
            self.focus_first_item()
            e.accept()
            return
        super().keyPressEvent(e)

    def _retry_load(self):
        """Wired to the EmptyState action button. Re-runs the same
        load that just failed with the cached parent_id."""
        self.load(self._parent_id)

    def _on_rail_loaded(self, rail_key: str, cache_name, scope: dict,
                        signal, items: list):
        """Persist a rail's fresh items, emit them to the rail widget,
        and update the per-rail status tracker."""
        if items and cache_name is not None:
            disk_cache.save(cache_name, scope, items)
        signal.emit(items)
        self._mark_rail_done(rail_key, items=items, errored=False)

    def _on_rail_error(self, rail_key: str, signal):
        """Surface an empty rail to keep the layout stable and report
        the failure to the status tracker so we can show a unified
        error state if every rail failed."""
        signal.emit([])
        self._mark_rail_done(rail_key, items=[], errored=True)

    def _mark_rail_done(self, rail_key: str, *, items: list, errored: bool):
        if errored:
            self._rail_status[rail_key] = "errored"
        elif items:
            self._rail_status[rail_key] = "loaded"
        else:
            self._rail_status[rail_key] = "empty"
        self._maybe_show_status()

    def _maybe_show_status(self):
        """Decide whether the loading / empty / error overlay should
        still be visible. As soon as any rail reports "loaded" we
        hide it — partial success counts as "the surface is useful".
        We only show empty/error copy once every rail has reported
        in and none of them landed items."""
        statuses = self._rail_status.values()
        if any(s == "loaded" for s in statuses):
            self._status.setVisible(False)
            return
        if any(s == "pending" for s in statuses):
            # Still waiting on a rail — keep the loading copy up.
            return
        # All rails reported, none have items. Pick the right copy.
        if all(s == "errored" for s in statuses):
            self._status.set_state(
                glyph="⚠",
                headline="Couldn't load suggestions",
                sub="Check your connection to the server, then try again.",
                action_label="Try again",
            )
        else:
            # Some empty, maybe some errored, but no full success
            # anywhere. Default to the "library has nothing yet" read
            # since that's the more actionable interpretation for a
            # new server; a transient outage will hit retry naturally.
            self._status.set_state(
                glyph="♪",
                headline="Nothing here yet",
                sub="Add music to your server to see Suggestions.",
                action_label="Refresh",
            )
        self._status.setVisible(True)
