"""
Native artist detail page.

Shown when the user clicks an artist tile in the native Artists grid.
Replaces JF Web's artist detail rendering for the music library.

Layout:
- Header band: artist photo (left), name + genre + counts (right).
  (Back navigation is the top-bar back arrow — no in-page back button.)
- Body: grid of the artist's albums sorted by release year ascending
  (oldest → newest, "chronological release order"). Reuses LibraryTile
  for the album cells so the visual reads identically to the main
  Albums grid.

Click any album tile → existing browse path (preview in NowPlayingPage).
Play overlay → install that album as the live queue + start.
"""

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import QCoreApplication, Qt, Signal, Slot

logger = logging.getLogger(__name__)
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jellytoast.async_io import run_async
from jellytoast.design_tokens import (
    SPACE_LG,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    TYPE_DISPLAY,
    TYPE_MICRO,
    apply_type,
    font,
    rad,
    type_qss,
)
from jellytoast.keyboard_focus import (
    focus_first_item_on,
    keyboard_arrow_press,
    keyboard_focus_in,
    keyboard_focus_out,
    register_keyboard_mode_view,
)
from jellytoast.library_grid import (
    _LibraryItemsModel,
    _TileDelegate,
    _year_int,
)
from jellytoast.providers import get_provider
from jellytoast.ui_helpers import (
    EmptyState,
    ink_alpha,
    install_autofade_scrollbars,
    load_image_async,
    screen_dpr,
)

# ── Offline resolution helper ──────────────────────────────────────────────
#
# Module-level so tests can exercise the "meta + albums" lookup without
# needing a QApplication or a real ArtistPage instance. Returns
# ``(meta, albums)`` where either side may be ``None`` / ``[]``.
#
# The function never raises — a malformed snapshot just yields an empty
# match — because it runs from the GUI thread in response to a click and
# the "Couldn't load artist" empty state is a worse outcome than a
# best-effort render.


def _build_artist_name_map(tracks: List[Dict], albums: List[Dict]) -> Dict[str, str]:
    """Walk every downloaded track + album once and lift every
    ``(artist_id → artist_name)`` mapping out of their ``ArtistItems`` /
    ``AlbumArtists`` arrays. Used to resolve the artist's display name
    when the artist node itself isn't in the offline graph, so we can
    fall back to a string-name match against ``album['AlbumArtist']``."""
    out: Dict[str, str] = {}
    for tr in tracks or []:
        for ar in tr.get("ArtistItems") or []:
            aid = (ar or {}).get("Id")
            name = (ar or {}).get("Name")
            if aid and name and aid not in out:
                out[aid] = name
    for al in albums or []:
        for ar in al.get("AlbumArtists") or []:
            aid = (ar or {}).get("Id")
            name = (ar or {}).get("Name")
            if aid and name and aid not in out:
                out[aid] = name
    return out


def _resolve_offline_artist(artist_id: str):
    """Look up an artist's offline metadata + their downloaded albums.

    Resolution order:
    1. Frozen artist node + its album children (the happy path — the
       user explicitly downloaded the whole artist).
    2. Id-match against every complete album's ``AlbumArtists[].Id``
       (the common case — the user downloaded one album by this
       artist).
    3. **String-name match** against every complete album's
       ``AlbumArtist`` field. Catches the Subsonic case where the
       artist's Id differs across endpoints (a track's
       ``ArtistItems[0].Id`` can disagree with the album's
       ``AlbumArtists[0].Id``), so the Id-only match misses albums the
       user actually has. The name to match by is looked up from the
       union of every track + album in the graph.

    ``meta`` is synthesized from the album entries when the artist node
    itself isn't present. Returns ``(meta, albums)``.
    """
    from jellytoast import offline as _offline

    meta = _offline.get_snapshot(artist_id)
    albums = _offline.child_snapshots(artist_id, kind="album")

    complete_albums = None  # lazy-load once if any fallback needs it

    # Fallback 1: artist node missing, but some downloaded album lists
    # this artist_id in AlbumArtists.
    if not albums:
        complete_albums = _offline.list_complete_items("album") or []
        albums = [
            a
            for a in complete_albums
            if any((ar or {}).get("Id") == artist_id for ar in (a.get("AlbumArtists") or []))
        ]

    # Synthesize meta from the first matching album's AlbumArtists entry
    # if we have albums but no artist node.
    if meta is None and albums:
        for a in albums:
            for ar in a.get("AlbumArtists") or []:
                if (ar or {}).get("Id") == artist_id:
                    meta = {
                        "Id": artist_id,
                        "Name": ar.get("Name")
                        or QCoreApplication.translate("ArtistPage", "Unknown artist"),
                        "Type": "MusicArtist",
                    }
                    break
            if meta:
                break

    # Fallback 2: still nothing — the user's server hands out a
    # different artist_id from the one stored in the album snapshot.
    # Resolve the artist's *name* from any track/album that does mention
    # this id, then re-match albums by lowercase string equality on
    # ``album['AlbumArtist']``.
    if not albums:
        if complete_albums is None:
            complete_albums = _offline.list_complete_items("album") or []
        complete_tracks = _offline.list_complete_items("track") or []
        name_map = _build_artist_name_map(complete_tracks, complete_albums)
        artist_name = name_map.get(artist_id)
        if artist_name:
            wanted = artist_name.strip().lower()
            albums = [
                a for a in complete_albums if (a.get("AlbumArtist") or "").strip().lower() == wanted
            ]
            if meta is None and albums:
                meta = {
                    "Id": artist_id,
                    "Name": artist_name,
                    "Type": "MusicArtist",
                }

    return meta, albums


class ArtistPage(QWidget):
    """Artist detail surface — header + chronological album grid."""

    dismiss_requested = Signal()
    # Forwarded from each album tile so the host can open the preview /
    # install-and-play path without ArtistPage knowing about either.
    album_browse_requested = Signal(str)
    album_play_requested = Signal(str)
    year_browse_requested = Signal(int)  # year — click the year on an album tile

    HEADER_COVER = 180

    # Async fetch results land on the GUI thread via these.
    _meta_loaded = Signal(str, object)  # (artist_id, meta or None)
    _albums_loaded = Signal(str, object)  # (artist_id, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._artist_id: str = ""
        self._artist_meta: Dict = {}

        self.setObjectName("artistPage")
        self._apply_page_styling()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # No dedicated back button — the top-bar back arrow already walks the
        # nav history out of the artist page (see _dismiss_artist_page).

        # Header: artist photo + meta block.
        header = QHBoxLayout()
        header.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        header.setSpacing(SPACE_XL)

        self._cover = QLabel()
        self._cover.setFixedSize(self.HEADER_COVER, self.HEADER_COVER)
        self._cover.setStyleSheet(self._cover_qss())
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: Optional[QPixmap] = None
        header.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        meta = QVBoxLayout()
        meta.setContentsMargins(0, SPACE_SM, 0, 0)
        meta.setSpacing(SPACE_SM)

        self._kicker = QLabel(self.tr("ARTIST"))
        self._kicker.setStyleSheet(self._kicker_qss())
        apply_type(self._kicker, TYPE_MICRO)
        meta.addWidget(self._kicker)

        self._name = QLabel(self.tr("Loading…"))
        self._name.setFont(font(TYPE_DISPLAY))
        self._name.setStyleSheet(self._name_qss())
        self._name.setWordWrap(True)
        meta.addWidget(self._name)

        self._info = QLabel("")
        self._info.setStyleSheet(self._info_qss())
        meta.addWidget(self._info)

        meta.addStretch(1)
        meta_widget = QWidget(self)
        meta_widget.setLayout(meta)
        header.addWidget(meta_widget, 1)

        outer.addLayout(header)

        # Album grid — model/view/delegate. show_subtitle=False because
        # every album on this page has the same artist (the page's
        # subject); repeating the artist line under each cover would
        # be noise.
        self._model = _LibraryItemsModel(self)
        self._delegate = _TileDelegate(
            "album",
            self,
            show_year=True,
            show_subtitle=False,
        )
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setFlow(QListView.Flow.LeftToRight)
        self._view.setWrapping(True)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setUniformItemSizes(True)
        self._view.setMouseTracking(True)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setSpacing(0)
        self._view.setViewportMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        # Flatten viewport first-paint.
        _vp = self._view.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self._view.setStyleSheet("QListView { background: transparent; border: none; }")
        install_autofade_scrollbars(self._view)
        # Click routing: overlay → play, anywhere else → browse.
        self._view.mousePressEvent = self._on_view_press
        # Keyboard routing: Enter on the focused tile plays the album,
        # matching the play-overlay-click intent. focusInEvent seeds
        # currentIndex so the focus ring paints immediately on entry.
        self._view.keyPressEvent = self._on_view_key
        self._view.focusInEvent = self._on_view_focus_in
        self._view.focusOutEvent = self._on_view_focus_out
        # Keyboard-nav focus ring — the reused _TileDelegate paints it gated
        # on _keyboard_mode; Tab / chrome-Down land focus on the grid.
        self._view._keyboard_mode = False
        register_keyboard_mode_view(self._view)
        self.setFocusProxy(self._view)

        # Stack the album grid with an empty-state surface so an
        # artist with no albums returned (rare — usually means the
        # albums fetch failed silently or the artist is genuinely
        # empty) reads as "no albums" instead of a blank grid.
        self._empty_state = EmptyState(
            glyph="♪",
            headline=self.tr("No albums for this artist"),
            sub=self.tr(
                "The artist is in your library but no albums "
                "are showing — try refreshing or going back."
            ),
            parent=self,
        )
        self._grid_stack = QStackedWidget(self)
        self._grid_stack.setStyleSheet("background: transparent;")
        self._grid_stack.addWidget(self._view)
        self._grid_stack.addWidget(self._empty_state)
        outer.addWidget(self._grid_stack, 1)
        self._initial_albums_load_complete = False

        self._meta_loaded.connect(self._on_meta_loaded)
        self._albums_loaded.connect(self._on_albums_loaded)
        # Cross-DPR cover refresh — re-fetch the artist header
        # photo + album thumbnails at the new physical target when
        # the user drags the window between scaled monitors. Same
        # pattern as library_grid / songs_view / NP bar.
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().dpr_changed.connect(self._on_dpr_changed)
        # Re-load the current artist when offline mode flips — the
        # data source swaps between provider and downloads.db.
        # QueuedConnection defers the re-query to the next event-loop
        # tick so the toggle widget repaints first.
        PlayerBus.get().offline_mode_changed.connect(
            self._on_offline_mode_changed,
            Qt.ConnectionType.QueuedConnection,
        )
        # Live-apply: re-stamp theme-dependent stylesheets when the
        # color editor (or accent picker) fires theme_changed.
        try:
            PlayerBus.get().theme_changed.connect(self._apply_styling)
        except Exception:
            pass

    # ── Theme-dependent styling (live-apply via PlayerBus.theme_changed) ──

    @staticmethod
    def _page_qss() -> str:
        """Page-level QSS — scrollbar handle uses SLIDER_GROOVE-ish
        tint. Read live so the color editor can adjust scrollbar
        weight in place."""
        from jellytoast import ui_helpers as _u

        return f"""
            QWidget#artistPage,
            QWidget#artistPage QWidget,
            QWidget#artistPage QLabel,
            QWidget#artistPage QFrame,
            QWidget#artistPage QScrollArea,
            QWidget#artistPage QScrollArea > QWidget,
            QWidget#artistPage QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QWidget#artistPage QScrollBar:vertical {{
                background: transparent; width: 8px;
                margin: 4px 2px 4px 0; border: none;
            }}
            QWidget#artistPage QScrollBar::handle:vertical {{
                background: {_u.SLIDER_GROOVE};
                border-radius: {rad(3)}px; min-height: 28px;
            }}
            QWidget#artistPage QScrollBar::handle:vertical:hover {{
                background: {ink_alpha(0.32)};
            }}
            QWidget#artistPage QScrollBar::add-line:vertical,
            QWidget#artistPage QScrollBar::sub-line:vertical {{ height: 0; }}
        """

    @staticmethod
    def _cover_qss() -> str:
        from jellytoast import ui_helpers as _u

        return f"background: {_u.HOVER_LIST_ROW}; border-radius: 90px;"

    @staticmethod
    def _kicker_qss() -> str:
        from jellytoast import ui_helpers as _u

        return f"color: {_u.TEXT_FAINT}; {type_qss(TYPE_MICRO)}"

    @staticmethod
    def _name_qss() -> str:
        from jellytoast import ui_helpers as _u

        return f"color: {_u.TEXT};"

    @staticmethod
    def _info_qss() -> str:
        from jellytoast import ui_helpers as _u

        return f"color: {_u.TEXT_DIM}; {type_qss(TYPE_BODY)}"

    def _apply_page_styling(self):
        self.setStyleSheet(self._page_qss())

    def _apply_styling(self):
        """Re-stamp every theme-dependent stylesheet on
        PlayerBus.theme_changed. Connected from load_artist's first
        call (since __init__ runs before the bus connection is wired
        in some host orders — safer to be defensive)."""
        self._apply_page_styling()
        if hasattr(self, "_cover"):
            self._cover.setStyleSheet(self._cover_qss())
        if hasattr(self, "_kicker"):
            self._kicker.setStyleSheet(self._kicker_qss())
        if hasattr(self, "_name"):
            self._name.setStyleSheet(self._name_qss())
        if hasattr(self, "_info"):
            self._info.setStyleSheet(self._info_qss())
        # Delegate-rendered list repaints with new colors via viewport
        # update — the delegate already reads tokens at paint time.
        if hasattr(self, "_view") and self._view.viewport() is not None:
            self._view.viewport().update()

    def _on_offline_mode_changed(self, _on: bool):
        if not self._artist_id:
            return
        aid = self._artist_id
        # Force a re-load by clearing the idempotent-show guard.
        self._artist_id = ""
        self._artist_meta = {}
        # QueuedConnection on the bus side defers this to the next
        # event-loop tick so the toggle widget repaints first.
        self.load_artist(aid)

    def _on_dpr_changed(self):
        """Reload the artist photo + drop the album-tile covers so
        the next paint re-requests them at the new physical size."""
        if not self._artist_id:
            return
        # Re-run the meta-loaded path so the photo re-requests with
        # the new DPR-aware target size.
        if self._artist_meta:
            self._on_meta_loaded(self._artist_id, self._artist_meta)
        # Clear album cover pixmaps + re-run the albums-loaded path
        # with the current model items so set_items kicks fresh
        # cover loads sized for the new monitor.
        items = list(self._model.items())
        if items:
            self._model.clear_covers()
            self._on_albums_loaded(self._artist_id, items)

    # ── Click hit-test ────────────────────────────────────────────────

    def _on_view_press(self, e):
        """Replace QListView.mousePressEvent so we can hit-test the
        delegate's play-overlay sub-rect — same pattern as the main
        album grid + horizontal rails."""
        if e.button() != Qt.MouseButton.LeftButton:
            QListView.mousePressEvent(self._view, e)
            return
        pos = e.position().toPoint()
        idx = self._view.indexAt(pos)
        if not idx.isValid():
            QListView.mousePressEvent(self._view, e)
            return
        item = idx.data(_LibraryItemsModel.ItemRole) or {}
        item_id = item.get("Id", "")
        cell = self._view.visualRect(idx)
        if (
            self._delegate._show_play_overlay
            and self._delegate.overlay_rect_for(cell).contains(pos)
            and item_id
        ):
            self.album_play_requested.emit(item_id)
            e.accept()
            return
        # Year click → jump to that year's browse, same as the main album grid.
        # Takes priority over the tile-body click so the painted year is live.
        year_int = _year_int(item)
        if year_int and self._delegate.year_rect_for(cell, item).contains(pos):
            self.year_browse_requested.emit(year_int)
            e.accept()
            return
        if item_id:
            self.album_browse_requested.emit(item_id)
            e.accept()
            return
        QListView.mousePressEvent(self._view, e)

    def _on_view_key(self, e):
        """Enter on the focused album *opens* the album page (browse),
        same as a click on the tile body. A second Enter on the
        album page itself starts playback — matches the keyboard
        commit-twice contract the library grid + search rails use.
        Arrow keys engage keyboard mode + move the ring."""
        if keyboard_arrow_press(self._view, e):
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self._view.currentIndex()
            if idx.isValid():
                item = idx.data(_LibraryItemsModel.ItemRole) or {}
                item_id = item.get("Id", "")
                if item_id:
                    self.album_browse_requested.emit(item_id)
                    e.accept()
                    return
        QListView.keyPressEvent(self._view, e)

    def _on_view_focus_in(self, e):
        """Engage keyboard mode + seed currentIndex on keyboard focus so the
        ring paints immediately (shared recipe — keyboard_focus)."""
        keyboard_focus_in(self._view, e)
        QListView.focusInEvent(self._view, e)

    def _on_view_focus_out(self, e):
        keyboard_focus_out(self._view, e)
        QListView.focusOutEvent(self._view, e)

    # ── Public API ─────────────────────────────────────────────────────

    def focus_first_item(self):
        """Keyboard parity — the app's chrome-Down filter dives here."""
        focus_first_item_on(self._view)

    def load_artist(self, artist_id: str):
        """Async-fetch artist metadata + their albums. Populates the
        page when the result lands. No-op if the same artist is
        already loaded (idempotent re-show)."""
        if not artist_id:
            return
        if artist_id == self._artist_id and self._artist_meta:
            return
        self._artist_id = artist_id
        self._artist_meta = {}
        self._model.set_items([])
        # Reset the stack to the grid page so a previous artist's
        # empty state doesn't linger on the new artist's load.
        self._grid_stack.setCurrentIndex(0)
        self._initial_albums_load_complete = False
        self._name.setText(self.tr("Loading…"))
        self._info.setText("")
        # Offline mode: resolve everything from the local snapshot graph
        # instead of hitting the provider. The provider would just time
        # out and the user would see "Couldn't load artist" even when
        # they have albums by this artist downloaded.
        try:
            from jellytoast import offline as _offline

            if _offline.is_offline_mode():
                self._load_artist_offline(artist_id)
                return
        except Exception:
            pass

        run_async(
            self.api.get_item,
            artist_id,
            on_result=lambda meta, aid=artist_id: self._meta_loaded.emit(aid, meta),
            on_error=lambda _e, aid=artist_id: self._meta_loaded.emit(aid, None),
        )
        run_async(
            self.api.get_artist_albums,
            artist_id,
            on_result=lambda albums, aid=artist_id: self._albums_loaded.emit(aid, albums),
            on_error=lambda _e, aid=artist_id: self._albums_loaded.emit(aid, []),
        )

    def _load_artist_offline(self, artist_id: str):
        """Offline-mode load: resolve artist meta + albums from the
        local snapshot graph, then drive the same async-handler path the
        online flow uses so the rest of the page rendering is shared.

        The id-only match against ``AlbumArtists[].Id`` can miss albums
        on Subsonic where the artist's id varies across endpoints — see
        :func:`_resolve_offline_artist` for the string-name fallback."""
        meta, albums = _resolve_offline_artist(artist_id)
        logger.debug(
            "artist offline lookup: id=%s meta=%s albums=%s",
            artist_id,
            "yes" if meta else "no",
            len(albums),
        )
        self._meta_loaded.emit(artist_id, meta)
        self._albums_loaded.emit(artist_id, albums)

    # ── Async handlers ─────────────────────────────────────────────────

    def _rebuild_info(self):
        """Compose the artist sub-header from whatever async state has
        landed: the meta genre (once meta resolves) + the album count
        (once albums resolve). Called by BOTH async handlers so the two
        fetches can resolve in any order without one clobbering the
        other's contribution to the line."""
        bits = []
        genres = [g for g in (self._artist_meta.get("Genres") or []) if g]
        if genres:
            bits.append(genres[0])
        if self._initial_albums_load_complete:
            n = self._model.rowCount()
            bits.append(self.tr("{0} albums").format(n) if n != 1 else self.tr("1 album"))
        self._info.setText("  ·  ".join(bits))

    @Slot(str, object)
    def _on_meta_loaded(self, artist_id: str, meta: Optional[Dict]):
        if artist_id != self._artist_id:
            return  # stale; user moved on
        if meta is None:
            self._name.setText(self.tr("Couldn't load artist"))
            return
        self._artist_meta = meta
        self._name.setText(meta.get("Name") or self.tr("Unknown"))
        # Rebuild from shared state so this handler doesn't clobber the
        # album count _on_albums_loaded may have already set. Both async
        # handlers call _rebuild_info(), so meta + albums can resolve in
        # EITHER order — previously, meta resolving second overwrote the
        # count with a genre-only (or blank) line, e.g. a single-album
        # artist's header showed nothing. Live find 2026-06-02.
        self._rebuild_info()
        # Cover / artist photo. HiDPI: target physical pixels and the
        # DPR-multiplied radius so the cached slot matches what the
        # label paints; _on_cover_loaded just tags + sets.
        dpr = screen_dpr(self)
        target_phys = max(self.HEADER_COVER, int(round(self.HEADER_COVER * dpr)))
        radius_phys = int(round(90 * dpr))
        # Fixed worst-case-DPR source size — L2 raw caches one entry per
        # artist that derives every DPR locally. See
        # docs/research/dpr_cache_keys.md.
        server_px = self.HEADER_COVER * 3
        url = self.api.get_image_url(artist_id, "Primary", server_px)
        if url:
            # Gate the async header callback on the still-current artist so a
            # late photo can't paint over a different artist's header.
            aid = artist_id
            load_image_async(
                f"{artist_id}|artistphoto",
                url,
                target_phys,
                target_phys,
                lambda pix, a=aid: None if a != self._artist_id else self._on_cover_loaded(pix),
                rounded_radius=radius_phys,
            )

    @Slot(object)
    def _on_cover_loaded(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        self._cover_orig = pix
        dpr = screen_dpr(self)
        if dpr != 1.0:
            pix.setDevicePixelRatio(dpr)
        self._cover.setPixmap(pix)

    @Slot(str, object)
    def _on_albums_loaded(self, artist_id: str, albums: Optional[List[Dict]]):
        if artist_id != self._artist_id:
            return
        albums = albums or []

        # Sort chronologically — oldest first. Jellyfin's
        # get_artist_albums returns descending; sort client-side so we
        # don't have to fork the API helper. ProductionYear is the
        # most reliable field for music; PremiereDate falls back to
        # something parseable.
        def _year(item):
            y = item.get("ProductionYear")
            if y:
                return int(y)
            # PremiereDate is ISO 8601 — first 4 chars are the year.
            pd = (item.get("PremiereDate") or "").strip()
            if pd[:4].isdigit():
                return int(pd[:4])
            return 9999  # unknown years sort last

        albums = sorted(albums, key=_year)

        # Populate the model + mark complete BEFORE rebuilding the info
        # line, so _rebuild_info() reads the album count off the model.
        # QListView in IconMode with setResizeMode(Adjust) handles column
        # reflow on resize automatically — no manual grid math.
        self._model.set_items(albums)
        self._initial_albums_load_complete = True
        self._rebuild_info()
        if not albums:
            self._grid_stack.setCurrentIndex(1)
            return
        self._grid_stack.setCurrentIndex(0)
        dpr = screen_dpr(self)
        target_phys = max(
            _TileDelegate.COVER_SIZE,
            int(round(_TileDelegate.COVER_SIZE * dpr)),
        )
        radius_phys = int(round(_TileDelegate.COVER_RADIUS * dpr))
        # Reuse library_grid's fixed source-px (worst-case 3× display)
        # so the L2 raw cache stays one-entry-per-album across DPRs.
        # See docs/research/dpr_cache_keys.md.
        server_px = _TileDelegate.COVER_SIZE * 3
        # The tile cover loads are async; capture the artist they belong to
        # so a late-arriving pixmap can't bleed onto a DIFFERENT artist the
        # user navigated to meanwhile. The entry guard above only covers the
        # synchronous dispatch, not these per-tile callbacks.
        aid = self._artist_id
        for row, album in enumerate(albums):
            cover_id = album.get("Id", "")
            if not cover_id:
                continue
            cover_url = self.api.get_image_url(cover_id, "Primary", server_px)
            if not cover_url:
                continue

            def _on_pix(pix, r=row, a=aid):
                if a != self._artist_id:
                    return
                self._model.set_cover(r, pix)

            load_image_async(
                f"{cover_id}|artistalbumtile",
                cover_url,
                target_phys,
                target_phys,
                _on_pix,
                rounded_radius=radius_phys,
                on_error=lambda: None,
            )
