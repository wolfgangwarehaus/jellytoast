"""Session / auth lifecycle for the main window.

The boot auth-check, heartbeat, provider-ref refresh, sign-out, server-change,
auth-failure, hotkey reinstall, host-switch, session-verify, and
native-sign-in glue, extracted from ``jellytoast/app.py``.

``_SessionMixin`` is mixed into ``JellytoastWindow`` — not standalone. Its
methods reference window state (``self.provider``, ``self.api``,
``self.queue_mgr``, ``self._hotkey_shortcuts``, ``self._library_ids`` …, the
widget roster in ``_refresh_provider_refs``) and call into the Nav core
(``self._route_home``) and the LibrarySelection mixin
(``self._refresh_library_selection``); all resolve on the combined instance.
The many heavy / cycle-prone deps (LoginView, reset_provider, hotkeys, toast,
offline, image_cache, disk_cache, get_qnam, QUrl) stay as in-method imports
exactly as before.
"""

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from jellytoast.async_io import run_async
from jellytoast.settings import get_settings

logger = logging.getLogger("jellytoast")


class _SessionMixin:
    """Session / auth lifecycle, mixed into ``JellytoastWindow``.
    Plain-``object`` mixin (single Qt base on the window)."""

    def _do_boot_auth_check(self):
        """Run the boot-time `is_authenticated` check after the event
        loop is alive — see the deferral comment in __init__. Builds
        the right initial surface (home destination on success, login
        on failure) and *then* shows the window so first paint is
        already populated."""
        from jellytoast.boot_timing import mark as _boot_mark

        _boot_mark("boot auth check entered")
        authed = self.provider.is_authenticated
        _boot_mark("credentials read (is_authenticated)")
        if not authed:
            # No credentials — any view-cache JSON left over from a
            # prior signed-in session would render as ghost data on
            # an unauthenticated app (playlists, genres, etc. served
            # straight from disk before the network fetch could
            # fail-401). Drop the cache here so the user lands on
            # empty states instead of stale rows.
            try:
                from jellytoast import disk_cache as _disk_cache

                _disk_cache.clear_all()
            except Exception:
                pass
            self.content_stack.setCurrentWidget(self.login_view)
            self._reveal_window()
            return
        # We have a persisted token from a previous session. Verify
        # it's still valid against the server (the device session may
        # have been revoked by an admin). If it fails, fall back to
        # the LoginView. The verify is async because verify_session
        # is a network call.
        run_async(
            self.provider.verify_session,
            on_result=self._on_verify_session_done,
            on_error=lambda _e: self._on_verify_session_done(False),
        )
        # Render the user's home destination immediately — verify
        # runs in the background; if it fails, _on_verify_session_done
        # swaps to the LoginView.
        self._route_home()
        _boot_mark("home surface routed")
        # Populate the multi-library dropdown on the relaunch path too.
        # The home route above goes straight here (NOT through
        # _on_native_signed_in, which only fires on a fresh login), so
        # without this the "Music" title never learns it has a dropdown
        # after a saved-session relaunch. Async + best-effort: if the
        # persisted token is actually dead, the list call just fails and
        # the verify above drops us to LoginView anyway.
        self._refresh_library_selection()
        self._reveal_window()

    def _retry_empty_native_views(self):
        """Re-trigger the load for any native surface that exists but
        has no items. Called after fresh credentials arrive — the
        prior fetch likely 401'd against a stale persisted token."""
        pid = self._music_parent_id()
        if self.songs_view is not None and not self.songs_view._items:
            self.songs_view.load_songs(pid)
        if self.suggestions_view is not None:
            # SuggestionsView always reloads cleanly (rails handle
            # empty payloads themselves).
            self.suggestions_view.load(pid)
        if self.album_grid is not None and not self.album_grid._tiles:
            self.album_grid.load_items(pid, "")
        if self.playlist_grid is not None and not self.playlist_grid._tiles:
            self.playlist_grid.load_items("", "")
        if self.artist_grid is not None and not self.artist_grid._tiles:
            self.artist_grid.load_items(pid, "")
        if self.genres_view is not None and not self.genres_view._tiles:
            self.genres_view.load_genres()

    def _heartbeat(self):
        """Periodic no-op GET to keep QNAM's TCP+TLS connection to the
        server warm. Cheap (50-byte response), silent on failure (the
        keepalive is best-effort — if it fails, the next real request
        will just pay the handshake cost it would've paid anyway).
        ``keep_alive_url`` is part of the provider ABC (default "" = no
        keepalive), so no AttributeError guard is needed."""
        url = self.provider.keep_alive_url()
        if not url:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkRequest

        from jellytoast.async_io import get_qnam

        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(5000)
        reply = get_qnam().get(req)
        # Drain the response so the connection is freed back to the
        # pool — without finished+deleteLater Qt eventually GCs the
        # reply but holds the socket longer than needed.
        reply.finished.connect(reply.deleteLater)

    def _refresh_provider_refs(self):
        """Re-read the active provider singleton and push it to every
        widget that cached a reference at construction time. Required
        whenever ``reset_provider()`` runs (sign-out, server kind
        switch in LoginView) — without it, surfaces built under the
        previous provider keep dispatching against the discarded
        instance and silently 401, so the user sees an empty grid
        until they restart the app."""
        from jellytoast.providers import get_provider as _gp

        self.provider = _gp()
        for w in (
            self.queue_mgr,
            self.np_bar,
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
            self.genres_view,
            self.suggestions_view,
            self.search_view,
            self.np_page,
            self.artist_page,
            getattr(self, "mpv_ctrl", None),
            getattr(self, "mini_player", None),
        ):
            if w is not None:
                w.api = self.provider

    def _on_sign_out_requested(self):
        # Halt playback first — without this, mpv keeps streaming the
        # current track using the credentials we're about to revoke,
        # the bottom now-playing bar keeps showing the previous user's
        # track, and any next-track advance hits 401. stop_requested
        # makes player_backend stop mpv (which then emits
        # playback_stopped, clearing the bar's cover/title to "Nothing
        # playing"); queue_clear empties the queue so a future sign-in
        # doesn't restore the prior user's queue.
        self.bus.stop_requested.emit()
        self.bus.queue_clear.emit()
        # Drain the download queue + cancel in-flight jobs BEFORE the token
        # is revoked — otherwise a download planned under this user keeps
        # running on the about-to-be-revoked credentials (next chunk 401s)
        # and, worse, an in-flight job that finishes would commit into the
        # offline library under whoever signs in next.
        from jellytoast import offline as _offline

        _offline.reset_download_queue()
        # Tell the server to revoke this device's session BEFORE we
        # clear the token locally — without this the row lingers in
        # the admin Devices dashboard until the user manually deletes
        # it. Synchronous (5s timeout, errors swallowed inside
        # server_logout) so we know the call completed before tearing
        # down credentials.
        self.provider.server_logout()
        settings = get_settings()
        settings.access_token = ""
        settings.user_id = ""
        settings.username = ""
        # Force a QSettings flush — tray Quit hard-shuts via os._exit and
        # bypasses the destructor; without this the cleared credentials
        # can be lost (per known_issue_qsettings_flush).
        settings.flush()
        # Rebuild the provider singleton so its in-memory credential
        # state matches the now-cleared settings — without this the
        # SubsonicProvider would still return is_authenticated=True
        # from cached _username + _password fields. JellyfinAPI's
        # credentials get cleared too because get_api()'s singleton
        # re-reads settings on next access through any new code path.
        from jellytoast.providers import reset_provider

        try:
            self.api.token = ""
            self.api.user_id = ""
        except Exception:
            pass
        reset_provider()
        self._refresh_provider_refs()
        # Drop any cached library ids resolved against the old user.
        self._library_ids = {}
        # Wipe the cover-art disk cache: the next user / server may
        # have different artwork for items that happen to share an
        # id (Subsonic IDs are short strings; collisions are realistic
        # across servers).
        from jellytoast import image_cache as _img_cache

        _img_cache.clear()
        # Also drop the in-memory pixmap / raw-image caches in
        # ui_helpers — same id-collision risk, same fix.
        from jellytoast.ui_helpers import clear_image_caches

        clear_image_caches()
        # Wipe view-cache JSON blobs (library_*, genres, songs,
        # suggestions_*, preview). Without this, navigating to
        # Playlists / Genres / Songs while signed out would render
        # the previous session's data from cache instead of the
        # "no items" empty state.
        from jellytoast import disk_cache as _disk_cache

        _disk_cache.clear_all()
        # Force every lazy-built native view to drop its in-memory
        # model so the next visit re-fetches from the (now empty)
        # cache + the (now unauthenticated) server, landing in the
        # empty state instead of showing the previous session's
        # rows that were last set into the model.
        for surface in (
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
            self.genres_view,
            self.suggestions_view,
        ):
            if surface is None:
                continue
            try:
                surface._clear()
            except AttributeError:
                pass
        # Show the native sign-in surface.
        self.content_stack.setCurrentWidget(self.login_view)

    def _on_server_change_requested(self, new_url: str):
        """Settings now does the prompting inline — the URL field
        above the button doubles as the input. This slot just
        validates + commits + signs out. The empty / unchanged
        bail-outs already happened on the settings side, but we
        re-check here so any future caller (programmatic, hotkey,
        test) gets the same guarantees."""
        new_url = (new_url or "").strip().rstrip("/")
        current = (self.provider.server_url or "").rstrip("/")
        if not new_url or new_url == current:
            return
        get_settings().server_url = new_url
        # Switching servers means the old auth is invalid; clear it
        # and fall back to LoginView (now pre-filled with the new URL).
        self._on_sign_out_requested()

    def _on_auth_failed(self):
        """Connectivity tracker tripped the auth-failure threshold —
        the persisted credentials are being rejected by the server.
        Drop to LoginView so the user has a path to re-enter creds
        instead of staring at silent empty states. Idempotent: if
        we're already on LoginView (user is actively typing creds
        and getting them wrong) this is a no-op visually."""
        if self.content_stack.currentWidget() is self.login_view:
            return
        logger.info("auth_failed handler — switching to LoginView")
        try:
            from jellytoast import disk_cache as _disk_cache

            _disk_cache.clear_all()
        except Exception:
            pass
        self.content_stack.setCurrentWidget(self.login_view)

    def _reinstall_hotkeys(self):
        """Tear down the current QShortcuts and rebuild them from the
        (now-updated) hotkey registry — called on PlayerBus.hotkeys_changed
        so a rebind in Settings takes effect immediately."""
        from jellytoast import hotkeys as _hotkeys

        for sc in getattr(self, "_hotkey_shortcuts", []):
            try:
                sc.setEnabled(False)
                sc.deleteLater()
            except Exception:
                pass
        self._hotkey_shortcuts = _hotkeys.install_shortcuts(self)

    def _on_host_switched(self, label: str):
        """The connectivity engine failed over to (or recovered from) an
        alternate server URL — flash a toast so the user knows which
        address they're on. Lifted clear of the now-playing bar."""
        from jellytoast.toast import show_toast

        name = (label or "").strip() or "Primary"
        if name == "Primary":
            message = "Reconnected to the primary server"
        else:
            message = f"Switched to alternate server · {name}"
        show_toast(self, message, bottom_margin=128)

    def _on_verify_session_done(self, ok: bool):
        """Result of the boot-time verify. If the persisted token was
        rejected, drop the user on the LoginView so they can re-auth.
        Pre-existing settings (server URL, last username) stay so the
        form is partially pre-filled. The token itself stays in
        keyring; api.authenticate will overwrite it on success."""
        if ok:
            return
        logger.info("persisted token rejected — showing login view")
        # The persisted token is dead; any cached view payloads from
        # the prior session would now render as ghost data on an
        # unauthenticated app. Drop them so the user lands on empty
        # states across the board instead of stale playlists / genres.
        try:
            from jellytoast import disk_cache as _disk_cache

            _disk_cache.clear_all()
        except Exception:
            pass
        self.content_stack.setCurrentWidget(self.login_view)

    def _on_native_signed_in(self):
        """Called when the LoginView's authenticate round-trip
        succeeded. Credentials are already persisted (provider.
        authenticate wrote them to QSettings + keyring); just route
        to the user's home destination and let the native surfaces
        take over. Library lookups are cleared so they re-resolve
        against the new credentials, and any built native surface
        that's empty gets retried."""
        # Rebuild the provider singleton from the just-persisted (and
        # flushed) credentials. authenticate() wrote username / user_id /
        # token to settings, but the live singleton may be the empty
        # instance that sign-out's reset_provider() rebuilt — LoginView
        # only reset it again when the *kind* changed (it authenticates
        # its own construction-time provider, which isn't the singleton).
        # So a SAME-kind re-login (e.g. Navidrome→Navidrome) otherwise
        # leaves the singleton with username="" → is_authenticated False →
        # "No albums yet" until a restart rebuilds it from disk. Resetting
        # here makes the next get_provider() re-read the fresh settings.
        from jellytoast.providers import reset_provider

        reset_provider()
        # LoginView may also have called reset_provider() (e.g. user picked
        # a different server kind in the dropdown). Either way the provider
        # singleton is now rebuilt — push it to every cached widget BEFORE
        # _route_home triggers any fetches, otherwise surfaces built under
        # the old provider keep using the discarded reference and silently
        # 401.
        self._refresh_provider_refs()
        # Reset the connectivity monitor for the new server BEFORE any
        # fetch fires. Otherwise the previous server's leftover failure
        # state — and the burst of parallel requests the home grid kicks
        # off against the freshly-swapped server — can trip auto-offline
        # mode and flap the UI in/out of offline during the initial load
        # (the partial-album-art + janky-login symptom on a Jellyfin →
        # Navidrome swap). A user-set offline mode is preserved.
        from jellytoast import offline as _offline

        _offline.reset_after_server_change()
        # Reset the multi-library selection for the new server (clears any
        # stale per-server selection) BEFORE the home route fetches, then
        # re-read the new server's music libraries into the dropdown.
        from jellytoast import library_selection as _ls

        _ls.reset_after_server_change()
        logger.info(
            "native sign-in succeeded (user=%s…)",
            self.provider.user_id[:8],
        )
        self._library_ids = {}
        self._refresh_library_selection()
        # Route to home destination (Albums grid by default). Lazily
        # builds the surface and kicks off its load.
        self._route_home()
        self._retry_empty_native_views()
        # Qt's auto-focus after the post-login route can land on the first
        # focusable top-bar chrome button (the back arrow), painting a
        # keyboard focus ring the user never asked for. The suggestions
        # surface drops its own internal auto-focus, but the grid route has
        # no such drop and the stray focus lands on the chrome. Clear a
        # stranded top-bar focus on the next tick — AFTER Qt assigns it
        # (a synchronous clear is too early; see _show_suggestions). A real
        # Tab press still focuses the nav anchors deliberately
        # (TabFocusReason), so keyboard users keep the ring when they ask.
        def _drop_stray_topbar_focus(_self=self):
            focused = QApplication.focusWidget()
            tb = getattr(_self, "top_bar", None)
            if (
                focused is not None
                and tb is not None
                and (focused is tb or tb.isAncestorOf(focused))
            ):
                focused.clearFocus()

        QTimer.singleShot(0, _drop_stray_topbar_focus)
