"""Multi-library selection controller for the main window.

The top-bar music-library dropdown glue, extracted from ``jellytoast/app.py``:
resolve a collection id, compute the active music ``parent_id`` honouring the
selection, list/sync the server's libraries into the selection state, and
reload the music browse surfaces when the selection changes.

``_LibrarySelectionMixin`` is mixed into ``JellytoastWindow`` — it is *not*
standalone. Its methods reference window-owned state/widgets (``self.provider``,
``self.top_bar``, ``self._library_ids``, ``self.album_grid``/``artist_grid``/
``songs_view``/``suggestions_view``) set up by ``JellytoastWindow.__init__``;
all resolve on the combined instance.

NB ``run_async`` is imported here, so a test that stubs the library-listing
fetch must patch ``library_selection_controller.run_async`` (the moved code
resolves it in this module's namespace), not ``jellytoast.run_async``.
"""

import logging

from PySide6.QtCore import Slot

from jellytoast.async_io import run_async
from jellytoast.player_state import PlayerBus
from jellytoast.settings import get_settings

logger = logging.getLogger("jellytoast")


class _LibrarySelectionMixin:
    """Multi-library selection glue, mixed into ``JellytoastWindow``.
    Plain-``object`` mixin so the window keeps a single Qt base
    (``QMainWindow``)."""

    def _resolve_library_id(self, collection_type: str) -> str:
        # Only return the cache when it actually resolved to an id —
        # caching an empty string would poison the lookup if the very
        # first call landed before credentials were bridged (the
        # request would 401 and we'd remember "" forever, even after
        # auth becomes available).
        cached = self._library_ids.get(collection_type)
        if cached:
            return cached
        try:
            libs = self.provider.get_libraries()
            match = next(
                (lib for lib in libs if lib.get("CollectionType") == collection_type), None
            )
            lib_id = match.get("Id") if match else ""
        except Exception as e:
            logger.warning(
                "couldn't resolve %s library: %s", collection_type, e
            )
            lib_id = ""
        if lib_id:
            self._library_ids[collection_type] = lib_id
        return lib_id or ""

    def _music_parent_id(self) -> str:
        """The ``parent_id`` the music browse surfaces should load right
        now, honouring the multi-library selection (top-bar dropdown).

        Phase 1 resolves the *single-parent* cases:
          * 'all' / nothing selected → the whole music library (``""`` on
            Subsonic = every folder; the music view id on Jellyfin).
          * exactly one library selected → that library's id.
        A partial subset of 3+ libraries would need the client-side
        ``library_selection.merge_paged`` merge wired through the grid's
        async pagination (Phase 2, GUI-gated); until then it degrades to
        'all' and logs, rather than silently showing a wrong subset.

        Known Phase-1 gap (Phase 2): on a *Jellyfin* server with 2+ music
        collection folders there is no single union parent — the 'all' case
        below resolves to the FIRST music view only, so 'all' shows just
        that view's content. Subsonic ('' = union of all folders) and the
        common single-music-view Jellyfin server are unaffected."""
        from jellytoast import library_selection as _ls

        ids = _ls.selected_ids()
        if len(ids) == 1:
            return ids[0]
        if len(ids) >= 2:
            logger.info(
                "multi-library subset (%d) selected; grid merge is Phase 2 — "
                "loading all music for now",
                len(ids),
            )
        # 'all' (or the not-yet-merged subset): whole music library.
        if not getattr(self.provider, "scopes_music_by_library", True):
            return ""  # Subsonic: empty parent = union of all folders
        # Jellyfin: scope to the music view id. On a 2+-music-view server
        # this is only the FIRST view (no union parent) — see the docstring;
        # the multi-view union is the Phase 2 follow-up.
        return self._resolve_library_id("music")

    def _refresh_library_selection(self):
        """Re-read the server's music libraries into the selection state
        and sync the top bar's dropdown + title. Called on BOTH entry
        paths — fresh sign-in (``_on_native_signed_in``) and a saved-
        session relaunch (``_do_boot_auth_check``) — so the dropdown
        reflects the server however the user arrived. ``get_libraries`` is
        a network call, so it runs off the GUI thread; the result is
        applied back on the GUI thread via the queued ``run_async``
        callback. Best-effort: a provider that can't list libraries leaves
        the feature dormant (single-library behaviour, plain label)."""
        run_async(
            self.provider.get_libraries,
            on_result=self._on_libraries_listed,
            on_error=self._on_libraries_list_failed,
        )

    def _on_libraries_listed(self, libs):
        from jellytoast import library_selection as _ls

        # The boot/relaunch home load runs BEFORE this async result lands, so
        # it resolved the selection while _available was still empty — a
        # stored id that went stale server-side (library deleted/recreated
        # between sessions) is trusted verbatim then, scoping the grid to a
        # ghost parent that renders empty. Capture the effective selection,
        # populate the real list (which filters stale ids), and if it
        # changed, re-issue the load so the user isn't stranded on a blank
        # grid that never heals (this path reloads nothing otherwise). The
        # grids' _load_gen guard makes the re-issue safe against the
        # just-fixed double-load.
        prev_sel = _ls.selected_ids()
        _ls.set_available_libraries(_ls.music_libraries(libs or []))
        if hasattr(self, "top_bar"):
            self.top_bar.set_available_libraries(_ls.available_libraries())
            self.top_bar.set_selected_libraries(_ls.selected_ids())
            self._sync_library_title()
        if _ls.selected_ids() != prev_sel:
            self._reload_music_surfaces()

    def _on_libraries_list_failed(self, e):
        logger.warning("couldn't list libraries for selection: %s", e)

    def _sync_library_title(self):
        """Push the active selection's title to the top bar (e.g. "Music",
        "Discover", "Music + Discover"). No-op off a music surface — the
        host's own ``set_title`` calls own non-music titles."""
        from jellytoast import library_selection as _ls

        if hasattr(self, "top_bar"):
            self.top_bar.set_title(_ls.selection_title("Music"))

    @Slot(list)
    def _on_libraries_selected(self, ids: list):
        """The top-bar dropdown reported a new selection. Persist it via
        the selection state (which normalizes 'all'/unknown ids) and, only
        if the effective selection actually changed, fan the reload out via
        the bus. Keeping the persist + emit here (not in the widget) means
        the widget stays a dumb view and the cache/reload policy lives in
        one place."""
        from jellytoast import library_selection as _ls

        if _ls.set_selected_ids(ids):
            # Flush so a hard tray-Quit right after the change can't lose it
            # — the QSettings destructor flush is unreliable on KDE Plasma
            # (see known_issue_qsettings_flush; matches the authenticate /
            # sign-out flush sites).
            get_settings().flush()
            PlayerBus.get().libraries_changed.emit()

    @Slot()
    def _on_libraries_changed(self):
        """The user changed the loaded-libraries selection in the top-bar
        dropdown. Re-title, push the normalized selection back to the
        dropdown (the host collapses 'every library' → 'all', so its
        checkmarks must be re-synced to match the title), and force every
        built music browse surface to reload against the new scope."""
        from jellytoast import library_selection as _ls

        self._sync_library_title()
        if hasattr(self, "top_bar"):
            self.top_bar.set_selected_libraries(_ls.selected_ids())
        self._reload_music_surfaces()

    def _reload_music_surfaces(self):
        """Force every built music browse surface to reload against the
        current ``_music_parent_id()`` scope. Mirrors the
        offline_mode_changed reload pattern — force-reload (not
        just-if-empty) so a selection change always re-scopes, and each
        grid's ``_load_gen`` guard makes the re-issue safe (no double-load)."""
        pid = self._music_parent_id()
        # Albums + Artists grids: clear cached scope so load_items re-fetches.
        for grid in (self.album_grid, self.artist_grid):
            if grid is not None:
                grid.load_items(pid, "")
        if self.songs_view is not None:
            self.songs_view.load_songs(pid)
        if self.suggestions_view is not None:
            self.suggestions_view.load(pid)
        # Genres list is server-global on Subsonic; its drill-down already
        # scopes by genre. Search scoping + the 3+-library grid merge are
        # the documented Phase 2 follow-ups (see library_selection).
