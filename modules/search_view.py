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

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modules.async_io import run_async
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    TYPE_MICRO,
    TYPE_SUBHEAD,
    apply_type,
    type_qss,
)
from modules.providers import get_provider
from modules.sort_utils import article_stripped_key
from modules.ui_helpers import (
    BORDER,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
    install_autofade_scrollbars,
    load_image_async,
    screen_dpr,
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


class _SongsSection(QWidget):
    """Vertical list of song rows. Click a row → install the visible
    song list as the live queue starting at that index. Album-cell
    click → emit album_browse_requested. Self-hides when set_items
    lands an empty list.

    Uses songs_view's _SongsListModel + _SongRowDelegate + _SongsListView
    so the rendering matches the bulk songs view exactly."""

    play_requested = Signal(int, list)  # start_idx, items snapshot
    album_browse_requested = Signal(str)  # album_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        self._header = QLabel("Songs")
        self._header.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)} padding: 0 {SPACE_XL}px;"
        )
        apply_type(self._header, TYPE_MICRO)
        outer.addWidget(self._header)

        from modules.songs_view import (
            _SongRowDelegate,
            _SongsListModel,
            _SongsListView,
        )

        self._model = _SongsListModel(self)
        self._delegate = _SongRowDelegate(self)
        self._view = _SongsListView(self._delegate, self)
        self._view.setModel(self._model)
        # No internal vertical scroll — the section is embedded inside
        # the search column's outer scroll. Height is sized to fit all
        # rows in set_items so the outer scroll handles overflow.
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setViewportMargins(SPACE_LG, 0, SPACE_LG, 0)
        outer.addWidget(self._view)

        self._view.clicked.connect(self._on_view_clicked)
        self._view.album_clicked.connect(self.album_browse_requested.emit)

    def set_items(self, items: List[Dict]):
        items = list(items or [])
        self._model.set_items(items)
        if not items:
            self.setVisible(False)
            return

        # Size the view to exactly fit its rows so the surrounding
        # search column owns vertical scrolling.
        from modules.songs_view import _SongRowDelegate as _SRD

        self._view.setFixedHeight(_SRD.ROW_HEIGHT * len(items) + 4)

        api = get_provider()
        dpr = screen_dpr(self)
        target_phys = max(
            _SRD.THUMB_SIZE,
            int(round(_SRD.THUMB_SIZE * dpr)),
        )
        radius_phys = int(round(_SRD.THUMB_RADIUS * dpr))
        # Server fetch at the fixed worst-case-DPR source size; the L2
        # raw cache then derives every DPR's variant locally from a
        # single per-item entry. See docs/research/dpr_cache_keys.md.
        server_px = _SRD.THUMB_SIZE * 3
        for row, item in enumerate(items):
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if not cover_id:
                continue
            cover_url = api.get_image_url(cover_id, "Primary", server_px)
            if not cover_url:
                continue

            def _on_pix(pix, r=row):
                self._model.set_cover(r, pix)

            load_image_async(
                f"{cover_id}|searchsong",
                cover_url,
                target_phys,
                target_phys,
                _on_pix,
                rounded_radius=radius_phys,
                on_error=lambda: None,
            )
        self.setVisible(True)

    @Slot(object)
    def _on_view_clicked(self, idx):
        if not idx.isValid():
            return
        row = idx.row()
        items = self._model.items()
        if 0 <= row < len(items):
            self.play_requested.emit(row, list(items))

    def first_focusable(self):
        return self._view if self._model.rowCount() > 0 else None


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

    songs_play_requested = Signal(int, list)  # start_idx, items snapshot
    album_play_requested = Signal(str)  # album_id (overlay click)
    album_browse_requested = Signal(str)  # album_id (tile click)
    artist_browse_requested = Signal(str)  # artist_id
    dismiss_requested = Signal()  # close button or Esc

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
            SPACE_XL,
            SPACE_LG,
            SPACE_XL,
            SPACE_MD,
        )
        input_layout.setSpacing(SPACE_MD)

        self._input = _SearchInput()
        self._input.setPlaceholderText("Search music…")
        self._input.setClearButtonEnabled(True)
        self._input.setStyleSheet(self._input_qss())
        self._input.textChanged.connect(self._on_text_changed)
        self._input.dismiss_requested.connect(self.dismiss_requested.emit)
        self._input.submit_requested.connect(self._fire_immediately)
        self._input.focus_first_result_requested.connect(self._focus_first_result)
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
                background: {ink_alpha(0.08)};
                color: {TEXT};
            }}
            QPushButton:pressed {{ background: {ink_alpha(0.14)}; }}
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
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
            f"color: {TEXT_DIM}; {type_qss(TYPE_SUBHEAD)} padding: {SPACE_XL * 2}px {SPACE_XL}px;"
        )
        col.addWidget(self._status)

        self._songs_section = _SongsSection()
        self._songs_section.play_requested.connect(self.songs_play_requested.emit)
        self._songs_section.album_browse_requested.connect(self.album_browse_requested.emit)
        col.addWidget(self._songs_section)

        # Use the shared HorizontalRail (model/view/delegate based) so
        # rendering matches every other music surface in the app.
        from modules.horizontal_rail import HorizontalRail

        self._albums_rail = HorizontalRail(
            "Albums",
            kind="album",
            cache_namespace="searchtile",
        )
        self._albums_rail.play_requested.connect(self.album_play_requested.emit)
        self._albums_rail.browse_requested.connect(self.album_browse_requested.emit)
        col.addWidget(self._albums_rail)

        # Artists rail — kind="artist" suppresses the play overlay (no
        # canonical "play an artist" action), so only browse fires.
        self._artists_rail = HorizontalRail(
            "Artists",
            kind="artist",
            cache_namespace="searchtile",
        )
        self._artists_rail.browse_requested.connect(self.artist_browse_requested.emit)
        col.addWidget(self._artists_rail)

        col.addStretch(1)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._all_loaded.connect(self._on_all_loaded)

        # Live-apply accent so the input's focus border tracks any
        # accent change made in Settings without requiring a restart.
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._reapply_accent)
        # Re-fire the current query when offline mode flips — the
        # data source swaps between server and downloads.db.
        # QueuedConnection defers the re-query to the next event-loop
        # tick so the toggle widget repaints first.
        PlayerBus.get().offline_mode_changed.connect(
            self._on_offline_mode_changed,
            Qt.ConnectionType.QueuedConnection,
        )

        # Cross-section keyboard nav. The event filter watches each
        # section's inner view and translates "exit-arrow" key presses
        # (Up at first row, Down at last row, Up/Down in a horizontal
        # rail) into focus hops between sections, so keyboard users
        # can walk Songs → Albums → Artists with arrow keys instead
        # of having the outer scroll area swallow the events.
        # currentChanged keeps the outer scroll in sync with the
        # keyboard cursor — without this the highlighted item would
        # slide below the viewport as the user keeps arrowing down.
        for view in (self._songs_section._view, self._albums_rail._view, self._artists_rail._view):
            view.installEventFilter(self)
            sm = view.selectionModel()
            if sm is not None:
                sm.currentChanged.connect(
                    lambda *_args, v=view: self._ensure_visible(v),
                )

    def _input_qss(self) -> str:
        """QSS for the search input. Focus border uses the current
        accent so a live accent change in Settings re-tints it via
        _reapply_accent — without this, the border stays whatever
        accent was active when the view was first built."""
        from modules.theme import _hex_to_rgb as _h2r
        from modules.ui_helpers import ACCENT as _ACCENT

        try:
            ar, ag, ab = _h2r(_ACCENT)
        except Exception:
            ar, ag, ab = 167, 139, 250
        return f"""
            QLineEdit {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 10px 14px;
                {type_qss(TYPE_BODY)}
                selection-background-color: rgba({ar},{ag},{ab},0.35);
            }}
            QLineEdit:focus {{
                border-color: rgba({ar},{ag},{ab},0.72);
                background: {ink_alpha(0.08)};
            }}
        """

    def _reapply_accent(self):
        """Re-stamp surfaces whose QSS baked the accent at construction.
        Wired to PlayerBus.theme_changed."""
        if hasattr(self, "_input"):
            self._input.setStyleSheet(self._input_qss())

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

        # Offline mode: skip the network entirely and match against
        # downloaded metadata in-process. Results land on the same
        # _all_loaded signal so the result-rendering path is shared.
        from modules import offline as _offline

        if _offline.is_offline_mode():
            buckets = self._local_search(query)
            self._all_loaded.emit((nonce, buckets, False))
            return

        # Single multi-type fetch. On Subsonic this is one search3
        # round-trip; on Jellyfin it's three sequential per-type calls
        # under the hood, but the SearchView side still renders all
        # three sections in one state transition.
        run_async(
            self.api.search_all,
            query,
            SONGS_LIMIT,
            ALBUMS_LIMIT,
            ARTISTS_LIMIT,
            on_result=lambda buckets, n=nonce: self._all_loaded.emit((n, buckets or {}, False)),
            on_error=lambda _e, n=nonce: self._all_loaded.emit((n, {}, True)),
        )

    def _local_search(self, query: str) -> dict:
        """Offline-mode search across the downloaded library. Matches
        against more than just ``Name`` so a query like "Air" surfaces
        the Moon Safari album (matched on AlbumArtists), the Air artist
        tile (synthesized from that album's AlbumArtists since no real
        artist node was downloaded), and the artist's tracks (matched
        on AlbumArtist / Artists), not just tracks literally named "Air".

        Returned shape mirrors ``provider.search_all`` — three buckets
        keyed by the Jellyfin Type names so the rest of the SearchView
        rendering path is identical."""
        from modules import offline as _offline

        q = query.strip().lower()
        if not q:
            return {"Audio": [], "MusicAlbum": [], "MusicArtist": []}

        def _hay(*fields) -> str:
            """Flatten the matchable text for a single item into one
            lowercase blob. Strings + lists-of-strings + lists-of-dicts
            with a ``Name`` key all get folded in; anything else is
            ignored. One ``in`` check then covers every field."""
            parts: list = []
            for f in fields:
                if not f:
                    continue
                if isinstance(f, str):
                    parts.append(f)
                elif isinstance(f, list):
                    for entry in f:
                        if isinstance(entry, str):
                            parts.append(entry)
                        elif isinstance(entry, dict):
                            n = entry.get("Name")
                            if isinstance(n, str):
                                parts.append(n)
            return "   ".join(parts).lower()

        # Songs — match against Name, Album, AlbumArtist, and Artists[].
        songs: list = []
        for meta in _offline.list_complete_items("track") or []:
            hay = _hay(
                meta.get("Name"),
                meta.get("Album"),
                meta.get("AlbumArtist"),
                meta.get("Artists"),
            )
            if q in hay:
                songs.append(meta)
                if len(songs) >= SONGS_LIMIT:
                    break

        # Albums — match against Name, AlbumArtist, and AlbumArtists[].Name.
        albums: list = []
        complete_albums = _offline.list_complete_items("album") or []
        for meta in complete_albums:
            hay = _hay(
                meta.get("Name"),
                meta.get("AlbumArtist"),
                meta.get("AlbumArtists"),
            )
            if q in hay:
                albums.append(meta)
                if len(albums) >= ALBUMS_LIMIT:
                    break

        # Artists — union of real artist nodes + AlbumArtists synthesized
        # from every downloaded album. Real nodes win on Id collision so
        # any extra fields (image ids, server-side artist metadata) the
        # real node carries aren't lost to a stub from album metadata.
        artists_by_id: dict = {}
        for meta in _offline.list_complete_items("artist") or []:
            aid = meta.get("Id")
            if aid and aid not in artists_by_id:
                artists_by_id[aid] = meta
        for meta in complete_albums:
            for entry in meta.get("AlbumArtists") or []:
                if not isinstance(entry, dict):
                    continue
                aid = entry.get("Id")
                name = entry.get("Name")
                if not aid or aid in artists_by_id:
                    continue
                artists_by_id[aid] = {
                    "Id": aid,
                    "Name": name or "",
                    "Type": "MusicArtist",
                }
        artists: list = []
        for meta in artists_by_id.values():
            name = (meta.get("Name") or "").lower()
            if q in name:
                artists.append(meta)
                if len(artists) >= ARTISTS_LIMIT:
                    break

        return {
            "Audio": songs,
            "MusicAlbum": albums,
            "MusicArtist": artists,
        }

    def _on_offline_mode_changed(self, _on: bool):
        if not self._current_query:
            return
        # QueuedConnection on the bus side defers this to the next
        # event-loop tick so the toggle widget repaints first.
        self._fire_search()

    def _stale(self, payload) -> bool:
        nonce = payload[0]
        return nonce != self._nonce

    @Slot(object)
    def _on_all_loaded(self, payload):
        if self._stale(payload):
            return
        _, buckets, errored = payload
        self._songs_section.set_items(buckets.get("Audio") or [])
        self._albums_rail.set_items(buckets.get("MusicAlbum") or [])
        self._artists_rail.set_items(buckets.get("MusicArtist") or [])
        self._reorder_sections(self._current_query, buckets)
        self._maybe_clear_status(errored=errored)

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
            return max(_name_score(it.get("Name", "") or "", query) for it in items)

        section_data.sort(key=section_score, reverse=True)

        # Reseat each section between the status label and the
        # trailing stretch. removeWidget keeps the widget alive — only
        # its layout slot moves.
        status_idx = self._sections_layout.indexOf(self._status)
        for widget, _ in section_data:
            self._sections_layout.removeWidget(widget)
        for offset, (widget, _) in enumerate(section_data):
            self._sections_layout.insertWidget(
                status_idx + 1 + offset,
                widget,
            )

    def _maybe_clear_status(self, errored: bool = False):
        any_visible = (
            self._songs_section.isVisible()
            or self._albums_rail.isVisible()
            or self._artists_rail.isVisible()
        )
        if any_visible:
            self._hide_status()
            return
        # No results landed. If the provider call errored, say so —
        # otherwise the user reads "no results" as "nothing matched"
        # when the request actually failed. We surface a short hint
        # rather than the raw exception (the route through async_io
        # has already logged it for diagnostics).
        if errored:
            self._show_empty_state("Search failed — check your connection and try again.")
        else:
            self._show_empty_state(f'No results for "{self._current_query}"')

    def keyPressEvent(self, event: QKeyEvent):
        # Defense-in-depth: if focus has wandered off the input, Escape
        # on the surface itself still dismisses.
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss_requested.emit()
            return
        super().keyPressEvent(event)

    # ── Cross-section keyboard navigation ───────────────────────────

    def _visible_sections(self):
        """Sections in current display order. _sections_layout is
        re-ordered per-query by _reorder_sections, so we read it
        live rather than caching."""
        out = []
        for i in range(self._sections_layout.count()):
            item = self._sections_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is None or widget is self._status:
                continue
            if not widget.isVisible():
                continue
            out.append(widget)
        return out

    def _section_owning(self, child):
        for sec in (self._songs_section, self._albums_rail, self._artists_rail):
            if sec is child or sec.isAncestorOf(child):
                return sec
        return None

    def _hop_section(self, current_section, direction: int) -> bool:
        sections = self._visible_sections()
        if current_section not in sections:
            return False
        cur = sections.index(current_section)
        target = cur + direction
        if direction < 0 and target < 0:
            # Past the top of the result column — return to the input.
            self._input.setFocus()
            self._input.selectAll()
            self._scroll.verticalScrollBar().setValue(0)
            return True
        if 0 <= target < len(sections):
            getter = getattr(sections[target], "first_focusable", None)
            if getter is not None:
                t = getter()
                if t is not None:
                    t.setFocus()
                    self._ensure_visible(t)
                    return True
        return False

    def _ensure_visible(self, view):
        """Scroll the outer container so the view's current cell is
        inside the viewport. Without this, arrow-key navigation rolls
        the highlighted row right off the bottom edge — the inner view
        doesn't scroll (sections are sized to fit their rows), so
        only the outer QScrollArea can keep things in frame."""
        if view is None or view.model() is None:
            return
        idx = view.currentIndex() if hasattr(view, "currentIndex") else None
        if idx is None or not idx.isValid():
            # No tracked cell — fall back to making the view itself
            # visible. Useful right after a section hop where focus
            # has just landed but currentIndex hasn't yet seeded.
            self._scroll.ensureWidgetVisible(view, 0, 40)
            return
        cell = view.visualRect(idx)
        if cell.isNull() or cell.isEmpty():
            self._scroll.ensureWidgetVisible(view, 0, 40)
            return
        # Map the cell into the scroll widget's coordinate space —
        # that's the space ensureVisible() expects.
        top_left = view.mapTo(self._container, cell.topLeft())
        bot_right = view.mapTo(self._container, cell.bottomRight())
        # Aim the scroll at both top and bottom corners so a tall cell
        # (rail tile with caption) doesn't get clipped at either edge.
        # ymargin keeps a little breathing room above and below.
        self._scroll.ensureVisible(top_left.x(), top_left.y(), 0, 60)
        self._scroll.ensureVisible(bot_right.x(), bot_right.y(), 0, 60)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key = event.key()
        if key not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            return super().eventFilter(obj, event)
        section = self._section_owning(obj)
        if section is None:
            return super().eventFilter(obj, event)
        # Songs is the only vertical-flow section — Up/Down only hops
        # when the cursor is at the section's first/last row. For the
        # horizontal rails, Up/Down always hops since the rail itself
        # is a single row and arrow-up/down has no in-rail meaning.
        if section is self._songs_section:
            view = section._view
            model = view.model()
            idx = view.currentIndex()
            row = idx.row() if idx.isValid() else 0
            last = (model.rowCount() - 1) if model is not None else 0
            if key == Qt.Key.Key_Up and row <= 0:
                if self._hop_section(section, -1):
                    return True
            elif key == Qt.Key.Key_Down and row >= last:
                if self._hop_section(section, +1):
                    return True
            return super().eventFilter(obj, event)
        # Horizontal rail.
        if key == Qt.Key.Key_Up:
            if self._hop_section(section, -1):
                return True
        elif key == Qt.Key.Key_Down:
            if self._hop_section(section, +1):
                return True
        return super().eventFilter(obj, event)
