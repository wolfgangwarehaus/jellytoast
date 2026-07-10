"""Navigation + view-routing for the main window.

The nav-bar / tab routing, the lazy view-show methods (downloads, smart
playlists, radio, music grids, songs, genres, suggestions, search, artist,
now-playing), the back/forward nav-history stack, and the grid-play handlers,
extracted from ``jellytoast/app.py``.

``_NavMixin`` is mixed into ``JellytoastWindow`` — not standalone. Its methods
reference window state/widgets (``self.content_stack``, ``self.top_bar``, the
lazy ``self.*_view`` / ``*_grid`` / ``np_page`` / ``artist_page`` refs,
``self._nav_history`` / ``self._nav_pos`` / ``self._suppress_nav_push``,
``self._NAV_HISTORY_CAP``) and call into sibling mixins / window core
(``self._music_fetch_plan``, ``self._kick_load_when_ready``,
``self._apply_music_chrome``); all resolve on the combined instance. The view
classes are imported in-method (lazy) exactly as before — no view-module ↔ host
cycle, and the boot import stays light. ``keyPressEvent`` and the
``_NAV_HISTORY_CAP`` constant stay on the window (Qt override / host state).
"""

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import QApplication

from jellytoast.now_playing_page import NowPlayingPage
from jellytoast.player_state import QueueContext, QueueKind
from jellytoast.settings import get_settings


class _NavMixin:
    """Navigation + view routing, mixed into ``JellytoastWindow``.
    Plain-``object`` mixin (single Qt base on the window)."""

    def _on_nav_requested(self, action: str):
        # Back / forward walk the jellytoast surface history — every
        # _show_* push is captured in _nav_history.
        if action == "back":
            self._go_back()
            return
        if action == "forward":
            self._go_forward()
            return
        if action == "search":
            self._show_search_view()
            return
        # Home routes to whichever native music surface the user picked
        # in Settings → General → "When Home is pressed, open:". Default
        # is the Albums grid — the canonical music landing.
        if action == "home":
            self._route_home()
            return

    def _on_tab_requested(self, index: int, label: str):
        # Tab dropdown is only populated with the music collection's
        # tabs (the only collection the native chrome ever shows), so
        # every label here maps to a native surface. Unknown labels
        # fall through silently rather than navigating somewhere
        # surprising.
        lab = label.lower()
        if lab == "albums":
            self._show_native_music_grid("album")
        elif lab == "playlists":
            self._show_native_music_grid("playlist")
        elif lab == "smart playlists":
            self._show_smart_playlists_view()
        elif lab in ("artists", "album artists"):
            self._show_native_music_grid("artist")
        elif lab == "songs":
            self._show_songs_view()
        elif lab == "genres":
            self._show_genres_view()
        elif lab == "suggestions":
            self._show_suggestions_view()
        elif lab == "downloads":
            self._show_downloads_library_view()
        elif lab == "radio":
            self._show_radio_view()
        else:
            return
        self.top_bar.set_active_tab(label)

    def _show_downloads_library_view(self):
        """Lazy-build + swap to the standalone Downloads page that
        lists every explicitly-downloaded item. Lives in the same
        ``content_stack`` as the album / artist grids; reusing the
        stack means back/forward navigation Just Works."""
        view = getattr(self, "_downloads_lib_view", None)
        if view is None:
            from jellytoast.downloads_library_view import DownloadsLibraryView

            view = DownloadsLibraryView()
            self._downloads_lib_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_smart_playlists_view(self):
        """Lazy-build + swap to the saved-smart-playlists page. Rules
        resolve to tracks through the active provider's ``query_items``
        on Play; the result installs a regular PLAYLIST queue so all
        existing now-playing UI works without surgery."""
        view = getattr(self, "_smart_playlists_view", None)
        if view is None:
            from jellytoast.smart_playlists_view import SmartPlaylistsView

            view = SmartPlaylistsView(self)
            self._smart_playlists_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_radio_view(self):
        """Lazy-build + swap to the standalone Radio page that lists
        every internet-radio station from the active provider. CRUD
        rides the provider abstraction; clicking a row installs a
        single-item INTERNET_RADIO queue."""
        view = getattr(self, "_radio_view", None)
        if view is None:
            from jellytoast.radio_view import RadioView

            view = RadioView(self.queue_mgr)
            self._radio_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_native_music_grid(self, kind: str = "album"):
        """Lazy-build + swap to a native LibraryGrid for the music
        library context. Albums + artists scope to the music library's
        parent_id (Recursive=True walks its tree); playlists fetch with
        empty parent_id because Jellyfin stores playlists as standalone
        items outside any library — scoping by music_lib_id would
        return nothing."""
        if kind == "playlist":
            parent_id = ""
        else:
            parent_id = self._music_fetch_plan()
        self._show_library_grid(kind, parent_id)

    @Slot()
    def _show_self(self):
        # Drop the minimized bit before showing — show() alone won't un-iconify
        # a window that was minimized to the taskbar.
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def _browse_album(self, album_id: str):
        """Route an album-tile / album-label click. If the clicked
        album is the one currently driving the live queue (queue
        context kind == ALBUM and source_id matches), jump straight to
        the live now-playing page — that's where the user is already
        listening from, so the preview/browse mode would just hide
        the live state. Any other album opens in preview mode.
        """
        ctx = self.queue_mgr.context
        same_album = (
            ctx.kind == QueueKind.ALBUM
            and (ctx.source_id or "").lower() == (album_id or "").lower()
        )
        if same_album and (album_id or ""):
            self._show_now_playing()
        else:
            self._show_now_playing(
                preview_id=album_id,
                preview_kind="album",
            )

    def _browse_playlist(self, playlist_id: str):
        """Route a playlist-tile click. Mirror _browse_album's logic:
        if the clicked playlist is currently driving the live queue,
        drop straight into the now-playing view instead of opening a
        redundant preview."""
        ctx = self.queue_mgr.context
        same_playlist = (
            ctx.kind == QueueKind.PLAYLIST
            and (ctx.source_id or "").lower() == (playlist_id or "").lower()
        )
        if same_playlist and (playlist_id or ""):
            self._show_now_playing()
        else:
            self._show_now_playing(
                preview_id=playlist_id,
                preview_kind="playlist",
            )

    def _show_now_playing(self, preview_id: str = "", preview_kind: str = "album"):
        # Lazy-build on first open. From the second open onward this is
        # just a stack flip; the page subscribes to the bus continuously
        # once it exists, so it stays in sync.
        if self.np_page is None:
            self.np_page = NowPlayingPage(self.queue_mgr, self)
            self.np_page.dismiss_requested.connect(self._dismiss_now_playing)
            # Bottom-bar left cluster (cover + title + artist + heart)
            # follows the page's preview state inversely: visible while
            # previewing (so the currently-playing track stays surfaced
            # in the bottom while the user browses), hidden in live mode
            # (the page itself shows the active track in large).
            self.np_page.preview_changed.connect(
                lambda is_preview: self.np_bar.set_left_cluster_visible(is_preview)
            )

            # Top-bar dropdown follows the preview state too — when the
            # user clicks a track in a previewed album, _on_row_clicked
            # clears the preview flag and starts the queue; the label
            # needs to flip from "Browsing" to "Now Playing" so the
            # nav state is honest.
            def _sync_top_bar_label(is_preview, _self=self):
                if _self.content_stack.currentWidget() is _self.np_page:
                    _self.top_bar.set_now_playing_mode(
                        True,
                        label=("Browsing" if is_preview else "Now Playing"),
                    )

            self.np_page.preview_changed.connect(_sync_top_bar_label)
            self.content_stack.addWidget(self.np_page)
        # preview_id != "" → browse mode (preview an album/playlist
        # without disturbing the live queue). Empty → live mode.
        if preview_id:
            self.np_page.load_preview(preview_id, preview_kind)
        else:
            self.np_page.clear_preview()
        self.content_stack.setCurrentWidget(self.np_page)
        # Top-bar dropdown reflects whether the user is in live
        # playback ("Now Playing") or previewing another album /
        # playlist ("Browsing"). Both modes show the same chevron menu
        # so the user can navigate away without using the back button.
        nav_label = "Browsing" if preview_id else "Now Playing"
        self.top_bar.set_now_playing_mode(True, label=nav_label)
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda pid=preview_id, pk=preview_kind: self._show_now_playing(pid, pk))

    def _on_content_changed(self, _idx: int):
        """Sync top-bar mode with the visible content surface. The
        np_page splits into "Now Playing" (live playback) and
        "Browsing" (preview of an album / playlist that isn't the
        active queue) — surfaced via the dropdown label so the user
        always knows which mode they're in."""
        if self.np_page is None:
            self.top_bar.set_now_playing_mode(False)
            return
        on_np = self.content_stack.currentWidget() is self.np_page
        if not on_np:
            self.top_bar.set_now_playing_mode(False)
            return
        label = "Browsing" if self.np_page._preview_id else "Now Playing"
        self.top_bar.set_now_playing_mode(True, label=label)

    def _dismiss_now_playing(self):
        """Back button on NowPlayingPage — walks the unified nav
        history. Falls back to the home destination if there's nothing
        earlier to return to (only happens at app launch with no
        other surface recorded yet)."""
        if not self._go_back():
            self._route_home()

    def _show_library_grid(
        self, kind: str, parent_id: str = "", genre_id: str = "", year: str = ""
    ):
        """Lazy-build + swap to a native LibraryGrid of the given kind.
        Browse clicks route to NowPlayingPage(preview, kind) for
        playable items, or the ArtistPage for artist tiles; play-
        overlay clicks install the item as the live queue and start it.

        `genre_id` filters the grid to a single genre (Jellyfin's
        ?GenreIds= param) — used by the Genres view's tile-click path
        to drop the user into an album grid scoped by genre."""
        from jellytoast.library_grid import LibraryGrid

        if kind == "playlist":
            if self.playlist_grid is None:
                self.playlist_grid = LibraryGrid(kind="playlist", parent=self)
                self.playlist_grid.browse_requested.connect(self._browse_playlist)
                self.playlist_grid.play_requested.connect(self._on_grid_play_playlist)
                self.content_stack.addWidget(self.playlist_grid)
            grid = self.playlist_grid
        elif kind == "artist":
            if self.artist_grid is None:
                self.artist_grid = LibraryGrid(kind="artist", parent=self)
                # Artist tiles open the dedicated ArtistPage instead
                # of NowPlayingPage's preview — "browse this artist"
                # means see all their albums, not preview a specific
                # collection of tracks.
                self.artist_grid.browse_requested.connect(self._show_artist_page)
                # play_requested is wired but the tile suppresses the
                # play-overlay for kind="artist" (no canonical "play
                # an artist" action — they pick an album from the page).
                self.content_stack.addWidget(self.artist_grid)
            grid = self.artist_grid
        else:
            if self.album_grid is None:
                self.album_grid = LibraryGrid(kind="album", parent=self)
                self.album_grid.browse_requested.connect(self._browse_album)
                self.album_grid.play_requested.connect(self._on_grid_play_album)
                # Subtitle-click on an album tile → ArtistPage. Year-
                # click → re-load the album grid filtered to that year.
                self.album_grid.artist_browse_requested.connect(self._show_artist_page)
                self.album_grid.year_browse_requested.connect(self._show_albums_by_year)
                self.content_stack.addWidget(self.album_grid)
            grid = self.album_grid

        # Re-fetch when scoping changes (parent_id / genre_id / year)
        # — otherwise reuse the loaded tiles to avoid thrashing covers
        # when the user toggles back to the grid from another view.
        prev_year = getattr(grid, "_year", "")
        if (
            not grid._tiles
            or grid._parent_id != parent_id
            or grid._genre_id != genre_id
            or prev_year != year
        ):
            grid.load_items(parent_id, genre_id, year)
        self.content_stack.setCurrentWidget(grid)
        # Deliberately do NOT auto-setFocus on the grid here — that
        # would light up the first album's focus ring on every
        # mouse navigation. The grid's focusProxy makes Tab into
        # the content section land on the inner view, so keyboard
        # users still get the ring when they ask for it.
        # The grid is its own browse surface — no need to also surface
        # the bottom-left now-playing cluster since the grid IS the
        # browsing context. Show it so the user can still see what's
        # playing while they browse.
        self.np_bar.set_left_cluster_visible(True)
        # Surface the library controls (Shuffle / View / Sort) cluster
        # in the top bar — they apply to the native grid only.
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(
            lambda k=kind, pid=parent_id, gid=genre_id, y=year: self._show_library_grid(
                k, pid, gid, y
            )
        )

    def _on_library_sort_changed(self, sort_by: str, sort_order: str):
        # Apply to whichever native surface honors sort and is currently
        # visible. Genres view has no sort (no album-style metadata to
        # sort by).
        current = self.content_stack.currentWidget()
        sortables = (
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
        )
        for surface in sortables:
            if surface is not None and surface is current:
                surface.set_sort(sort_by, sort_order)
                return

    def _show_songs_view(self):
        """Lazy-build + swap to the native Songs list view."""
        if self.songs_view is None:
            from jellytoast.songs_view import SongsView

            self.songs_view = SongsView(self)
            self.songs_view.play_requested.connect(self._on_songs_play_requested)
            self.songs_view.album_browse_requested.connect(self._browse_album)
            self.content_stack.addWidget(self.songs_view)
            self._kick_load_when_ready(
                lambda: self.songs_view.load_songs(self._music_fetch_plan())
            )
        self.content_stack.setCurrentWidget(self.songs_view)
        self.np_bar.set_left_cluster_visible(True)
        # Sort applies to songs; shuffle/view-toggle don't (yet).
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(lambda: self._show_songs_view())

    def _on_songs_play_requested(self, start_idx: int, items: list):
        """Songs view row click → install the visible song list as the
        live queue and start at the clicked index. The QueueContext is
        MANUAL since this isn't an album/playlist/artist source — the
        user is browsing flat tracks."""
        if not items or not (0 <= start_idx < len(items)):
            return
        from jellytoast.player_state import PlayerBus

        ctx = QueueContext(kind=QueueKind.MANUAL, source_label="Songs")
        PlayerBus.get().queue_play_now.emit(list(items), start_idx, ctx)

    def _show_genres_view(self):
        """Lazy-build + swap to the native Genres grid."""
        if self.genres_view is None:
            from jellytoast.genres_view import GenresView

            self.genres_view = GenresView(self)
            self.genres_view.genre_selected.connect(self._on_genre_selected)
            self.content_stack.addWidget(self.genres_view)
            self.genres_view.load_genres()
        self.content_stack.setCurrentWidget(self.genres_view)
        self.np_bar.set_left_cluster_visible(True)
        # Genres don't have a meaningful sort axis; hide the cluster.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_genres_view())

    def _show_suggestions_view(self):
        """Lazy-build + swap to the native Suggestions ("Discover")
        view. Three album rails (Latest / Recently played / Frequently
        played); tile clicks reuse the same browse + play paths as the
        main album grid."""
        if self.suggestions_view is None:
            from jellytoast.suggestions_view import SuggestionsView

            self.suggestions_view = SuggestionsView(self)
            self.suggestions_view.browse_requested.connect(self._browse_album)
            self.suggestions_view.play_requested.connect(self._on_grid_play_album)
            self.suggestions_view.artist_browse_requested.connect(self._show_artist_page)
            self.content_stack.addWidget(self.suggestions_view)
            self._kick_load_when_ready(
                lambda: self.suggestions_view.load(self._music_fetch_plan())
            )
        self.content_stack.setCurrentWidget(self.suggestions_view)

        # Qt's auto-focus on a freshly-shown stacked-widget page can
        # land on a rail view inside suggestions, painting the focus
        # ring on the first album at launch ("the app picked an album
        # for you"). The transfer happens deferred — on the next
        # event-loop tick, AFTER setCurrentWidget returns — so a
        # synchronous clearFocus here is too early. Queue it on
        # singleShot(0) instead so we run AFTER Qt has done its thing.
        def _drop_initial_focus(_self=self):
            focused = QApplication.focusWidget()
            if (
                focused is not None
                and _self.suggestions_view is not None
                and _self.suggestions_view.isAncestorOf(focused)
            ):
                focused.clearFocus()

        QTimer.singleShot(0, _drop_initial_focus)
        self.np_bar.set_left_cluster_visible(True)
        # Suggestions is a curated surface — sort/view-toggle controls
        # don't apply, so hide the top-bar cluster.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_suggestions_view())

    def _tab_anchors(self):
        """Three section anchors for Tab cycling, in display order:
        top bar (home button) → content (current surface) → bottom
        bar (play button). Home is the right anchor for the top bar
        because back/forward are conditionally disabled (nothing in
        nav history at boot), and a disabled widget rejects setFocus,
        which would silently swallow the Tab cycle."""
        anchors = []
        tb = getattr(self, "top_bar", None)
        if tb is not None and getattr(tb, "home_btn", None) is not None:
            anchors.append(tb.home_btn)
        cs = getattr(self, "content_stack", None)
        cur = cs.currentWidget() if cs is not None else None
        if cur is not None:
            anchors.append(cur)
        npb = getattr(self, "np_bar", None)
        if npb is not None and getattr(npb, "play_btn", None) is not None:
            anchors.append(npb.play_btn)
        return anchors

    def _current_section_index(self, focused, anchors) -> int:
        """Identify which Tab anchor currently owns focus by walking
        the parent chain from the focused widget. Falls back to -1
        when focus is outside all known sections so the next Tab
        press snaps to anchor 0 (the top bar)."""
        if focused is None:
            return -1
        # Each anchor's "section" is rooted at one of: top_bar,
        # content_stack, np_bar. Walk up from focused and match.
        section_roots = []
        tb = getattr(self, "top_bar", None)
        if tb is not None:
            section_roots.append((tb, 0))
        cs = getattr(self, "content_stack", None)
        if cs is not None:
            section_roots.append((cs, 1))
        npb = getattr(self, "np_bar", None)
        if npb is not None:
            section_roots.append((npb, 2))
        w = focused
        while w is not None:
            for root, idx in section_roots:
                if w is root:
                    # Clamp to len(anchors)-1 in case a section is
                    # missing its anchor (e.g., back_btn not built yet).
                    return min(idx, len(anchors) - 1)
            w = w.parentWidget()
        return -1

    def _push_nav(self, thunk):
        """Append a 'show this surface again' thunk to the history. If
        the user navigated from a back state (pos < end), trim the
        forward branch first — same model as a browser history. The
        suppress flag short-circuits this during back/forward replay
        so the replay doesn't itself create a new history entry.
        Capped at _NAV_HISTORY_CAP to keep a long-running session
        from growing the list unbounded."""
        if self._suppress_nav_push:
            return
        # Trim forward history when branching from a back state.
        if self._nav_pos < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[: self._nav_pos + 1]
        self._nav_history.append(thunk)
        # Cap the history — drop oldest entries first. Adjust
        # _nav_pos to stay valid relative to the new (trimmed)
        # list so back/forward still anchors to the right surface.
        if len(self._nav_history) > self._NAV_HISTORY_CAP:
            drop = len(self._nav_history) - self._NAV_HISTORY_CAP
            self._nav_history = self._nav_history[drop:]
        self._nav_pos = len(self._nav_history) - 1
        self._refresh_nav_buttons()

    def _go_back(self) -> bool:
        """Step one entry backward in history. Returns True if the
        replay actually moved; False if there's nothing earlier to go
        to (e.g. the user is at the first entry)."""
        if self._nav_pos <= 0:
            return False
        self._nav_pos -= 1
        self._suppress_nav_push = True
        try:
            self._nav_history[self._nav_pos]()
        finally:
            self._suppress_nav_push = False
        self._refresh_nav_buttons()
        return True

    def _go_forward(self) -> bool:
        if self._nav_pos + 1 >= len(self._nav_history):
            return False
        self._nav_pos += 1
        self._suppress_nav_push = True
        try:
            self._nav_history[self._nav_pos]()
        finally:
            self._suppress_nav_push = False
        self._refresh_nav_buttons()
        return True

    def _refresh_nav_buttons(self):
        """Sync the top-bar back/forward buttons' enabled state with
        the actual reachability in the history stack. Called after
        every push and every back/forward replay."""
        self.top_bar.set_back_enabled(self._nav_pos > 0)
        self.top_bar.set_forward_enabled(self._nav_pos + 1 < len(self._nav_history))

    def _route_home(self):
        """Top-bar Home button. Reads home_destination from Settings
        and swaps to the matching native music surface. Falls back to
        the Albums grid for unknown values (e.g. legacy keys after a
        rename) so Home is always functional."""
        self._apply_music_chrome()
        dest = get_settings().home_destination or "albums"
        if dest == "playlists":
            self._show_native_music_grid("playlist")
            active_tab = "Playlists"
        elif dest == "artists":
            self._show_native_music_grid("artist")
            active_tab = "Artists"
        elif dest == "songs":
            self._show_songs_view()
            active_tab = "Songs"
        elif dest == "genres":
            self._show_genres_view()
            active_tab = "Genres"
        elif dest == "suggestions":
            self._show_suggestions_view()
            active_tab = "Suggestions"
        else:
            self._show_native_music_grid("album")
            active_tab = "Albums"
        # Set after the content swap so set_active_tab runs while
        # the top bar is back in library mode (its guard early-returns
        # while _now_playing_mode is still True).
        self.top_bar.set_active_tab(active_tab)

    def _apply_music_chrome(self):
        """Set the top bar's title + collection so the View dropdown
        appears and the section label reflects the active library
        selection ("Music", or e.g. "Discover" / "Music + Discover" when a
        subset is loaded). Used whenever a native music surface becomes
        the active content widget."""
        from jellytoast import library_selection as _ls

        self.top_bar.set_title(_ls.selection_title_forms("Music"))
        self.top_bar.set_collection("music")

    def _show_search_view(self):
        """Lazy-build + swap to the native Search surface. Remembers the
        surface the user was on so dismiss returns there. The input is
        focused on every open so the user can type immediately."""
        if self.search_view is None:
            from jellytoast.search_view import SearchView

            self.search_view = SearchView(self)
            self.search_view.songs_play_requested.connect(self._on_search_songs_play)
            self.search_view.album_play_requested.connect(self._on_grid_play_album)
            self.search_view.album_browse_requested.connect(self._browse_album)
            self.search_view.artist_browse_requested.connect(self._show_artist_page)
            self.search_view.dismiss_requested.connect(self._dismiss_search_view)
            self.content_stack.addWidget(self.search_view)
        self.content_stack.setCurrentWidget(self.search_view)
        self.np_bar.set_left_cluster_visible(True)
        # Search is its own surface — no library controls apply.
        self.top_bar.set_library_controls_visible(False)
        self.search_view.focus_input()
        self._push_nav(lambda: self._show_search_view())

    def _dismiss_search_view(self):
        """Esc / close button on the SearchView — walks the unified
        nav history back to the previous surface. Falls back to the
        web view only if there's nothing earlier (shouldn't happen in
        practice since search is opened from another surface)."""
        if not self._go_back():
            self._route_home()

    def _on_search_songs_play(self, start_idx: int, items: list):
        """Search → song row click. Installs the visible song results
        as a MANUAL queue starting at the clicked index. Source label
        carries 'Search' so the now-playing kicker reads honestly
        (vs. inheriting an album/playlist label that doesn't match)."""
        if not items or not (0 <= start_idx < len(items)):
            return
        from jellytoast.player_state import PlayerBus

        ctx = QueueContext(kind=QueueKind.MANUAL, source_label="Search")
        PlayerBus.get().queue_play_now.emit(list(items), start_idx, ctx)

    def _on_genre_selected(self, genre_id: str, genre_name: str):
        """Genre tile click → swap to the album grid filtered by genre.
        Uses Jellyfin's ?GenreIds= filter (passed via load_items's
        genre_id arg). ParentId is left empty — the genre filter is
        sufficient and Jellyfin doesn't model genres as parents."""
        self._show_library_grid("album", parent_id="", genre_id=genre_id)

    def _show_albums_by_year(self, year: int):
        """Album-tile year click → swap to the album grid filtered to
        that single ProductionYear. Uses Jellyfin's ?Years= filter on
        Jellyfin (load_items year=...) and Subsonic's byYear/from-toYear
        on Subsonic (handled in SubsonicProvider._get_albums)."""
        if not year:
            return
        self._show_library_grid("album", parent_id="", year=str(year))

    def _show_artist_page(self, artist_id: str):
        """Lazy-build + swap to ArtistPage for the given artist. Click
        an album from there → existing browse path; back button walks
        the unified nav history."""
        if not artist_id:
            return
        if self.artist_page is None:
            from jellytoast.artist_page import ArtistPage

            self.artist_page = ArtistPage(self)
            self.artist_page.dismiss_requested.connect(self._dismiss_artist_page)
            self.artist_page.album_browse_requested.connect(self._browse_album)
            self.artist_page.album_play_requested.connect(self._on_grid_play_album)
            self.artist_page.year_browse_requested.connect(self._show_albums_by_year)
            self.content_stack.addWidget(self.artist_page)
        self.artist_page.load_artist(artist_id)
        self.content_stack.setCurrentWidget(self.artist_page)
        self.np_bar.set_left_cluster_visible(True)
        # Top-bar library controls don't apply to a single-artist page.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda aid=artist_id: self._show_artist_page(aid))

    def _dismiss_artist_page(self):
        """Back button on ArtistPage — walks the unified nav history."""
        if not self._go_back():
            self._route_home()

    def _on_library_view_mode_changed(self, mode: str):
        """Top-bar grid/list toggle → propagate to every native grid
        that's been built. Each LibraryGrid persists the choice via
        `library_view_mode` and re-renders its loaded items in place
        (no re-fetch). The toggle applies globally across albums /
        playlists / artists since one toolbar drives them all."""
        for g in (self.album_grid, self.playlist_grid, self.artist_grid):
            if g is not None:
                g.set_view_mode(mode)

    def _on_grid_play_album(self, album_id: str):
        """Play-overlay click on an album tile — install the full album
        as the live queue, start from track 0."""
        self._grid_play_collection(
            album_id,
            "album",
            self.provider.get_album_tracks,
        )

    def _on_grid_play_playlist(self, playlist_id: str):
        """Play-overlay click on a playlist tile — install the full
        playlist as the live queue, start from track 0."""
        self._grid_play_collection(
            playlist_id,
            "playlist",
            self.provider.get_playlist_items,
        )

    def _grid_play_collection(self, item_id: str, kind: str, fetch_fn):
        """Shared install-and-play path for album/playlist tile play
        clicks. `kind` maps to the QueueKind installed; `fetch_fn` is
        the API call that returns the track list."""
        if not item_id:
            return
        from jellytoast.async_io import run_async
        from jellytoast.player_state import PlayerBus

        queue_kind = QueueKind.PLAYLIST if kind == "playlist" else QueueKind.ALBUM

        def _on_tracks(tracks):
            if not tracks:
                return
            meta = self.provider.get_item(item_id) or {}
            ctx = QueueContext(
                kind=queue_kind,
                source_id=item_id,
                source_label=meta.get("Name", ""),
            )
            PlayerBus.get().queue_play_now.emit(list(tracks), 0, ctx)

        run_async(fetch_fn, item_id, on_result=_on_tracks)

    def _open_currently_playing_album(self):
        """JT_NATIVE_ALBUM shortcut handler — open the *currently-playing*
        track's album in NowPlayingPage's preview mode. Doesn't disrupt
        playback; the user can hit Play in the preview to install + play
        that album as a fresh queue."""
        from jellytoast.player_state import get_now_playing

        np = get_now_playing()
        album_id = (np.raw or {}).get("AlbumId", "") if np else ""
        if album_id:
            self._show_now_playing(preview_id=album_id)
