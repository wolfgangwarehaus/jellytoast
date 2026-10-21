"""Sign in / sign out for the Qt Quick front end — the same provider calls
and settings writes as the Widgets LoginView / SessionController.

Credentials live in ONE keyring entry shared by every profile, so a dev rig
(``JT_RIG=1``) refuses to sign in or out unless it runs on the null keyring
(dev/quick_rig.sh sets that) — otherwise it would overwrite the developer's
real login."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Property, QObject, Signal, Slot

from jellytoast import async_io

logger = logging.getLogger(__name__)


def _rig() -> bool:
    """A dev rig that could still reach the real keyring."""
    return os.environ.get("JT_RIG") == "1" and "null" not in os.environ.get(
        "PYTHON_KEYRING_BACKEND", ""
    )


def _normalized(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url  # both servers 404 on a bare host
    return url


class Session(QObject):
    changed = Signal()
    signedInNow = Signal()  # a sign-in just completed (reload the library)
    signedOutNow = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from jellytoast.providers import get_provider

        self._signed_in = bool(get_provider().is_authenticated)
        self._busy = False
        self._error = ""
        self._gen = 0

    # ── properties ──
    signedIn = Property(bool, lambda self: self._signed_in, notify=changed)
    busy = Property(bool, lambda self: self._busy, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)

    def _settings(self):
        from jellytoast.settings import get_settings

        return get_settings()

    serverUrl = Property(str, lambda self: self._settings().server_url or "", notify=changed)
    serverKind = Property(
        str, lambda self: (self._settings().provider_kind or "jellyfin").lower(), notify=changed
    )
    username = Property(str, lambda self: self._settings().username or "", notify=changed)

    def _fail(self, msg: str) -> None:
        self._busy = False
        self._error = msg
        self.changed.emit()

    # ── sign in ──
    @Slot(str, str, str, str)
    def signIn(self, kind: str, url: str, username: str, password: str) -> None:  # noqa: N802
        if self._busy:
            return
        if _rig():
            self._fail(
                self.tr("Signing in is off in the dev rig — it would replace your real login.")
            )
            return
        server, username = _normalized(url), (username or "").strip()
        if not server or not username:
            self._fail(self.tr("Please fill in the server URL and username."))
            return
        from jellytoast.providers import get_provider, reset_provider

        kind = (kind or "jellyfin").lower()
        settings = self._settings()
        if kind != get_provider().kind:
            settings.provider_kind = kind
            reset_provider()
        prov = get_provider()
        self._gen += 1
        gen = self._gen
        self._busy, self._error = True, ""
        self.changed.emit()

        def _go():
            info = prov.probe(server)
            if info is None:
                return ("not-server", None)
            prov.authenticate(server, username, password)
            return ("ok", (getattr(info, "product_name", "") or "").strip())

        def _done(result):
            if gen != self._gen:
                return
            status, product = result
            if status != "ok":
                self._fail(
                    self.tr("Couldn't find a {0} server at that address.").format(
                        "Jellyfin" if kind == "jellyfin" else "Navidrome / Subsonic"
                    )
                )
                return
            settings.server_is_navidrome = "navidrome" in (product or "").lower()
            try:
                from jellytoast.scrobble import refresh_server_scrobble_flags

                refresh_server_scrobble_flags()
            except Exception as e:
                logger.warning("scrobble flag refresh failed: %s", e)
            self._busy, self._error, self._signed_in = False, "", True
            self.changed.emit()
            self.signedInNow.emit()

        def _err(e):
            if gen != self._gen:
                return
            msg = str(e) or e.__class__.__name__
            if "Connection" in msg or "Max retries" in msg or "timed out" in msg:
                msg = self.tr("Couldn't reach the server. Check the URL and your network.")
            elif "401" in msg or "Unauthorized" in msg or "Wrong username" in msg:
                msg = self.tr("Wrong username or password.")
            else:
                msg = self.tr("Couldn't sign in: {0}").format(msg)
            self._fail(msg)

        async_io.run_async(_go, on_result=_done, on_error=_err)

    @Slot(str, result="QVariantMap")
    def demo(self, kind: str) -> dict:  # noqa: N802
        """The public demo server for a server type (url / username /
        password), or an empty map."""
        from jellytoast.demo_servers import demo_for_kind

        d = demo_for_kind(kind)
        return {"url": d.url, "username": d.username, "password": d.password} if d else {}

    # ── sign out ──
    @Slot()
    def signOut(self) -> None:  # noqa: N802
        """Same steps as the Widgets sign-out: stop, clear the queue and the
        download queue, end the server session, forget the token, drop the
        caches."""
        if _rig():
            self._fail(
                self.tr("Signing out is off in the dev rig — it would clear your real login.")
            )
            return
        from jellytoast import disk_cache, image_cache, offline
        from jellytoast.player_state import PlayerBus
        from jellytoast.providers import get_provider, reset_provider

        bus = PlayerBus.get()
        bus.stop_requested.emit()
        bus.queue_clear.emit()
        for what, fn in (
            ("download queue", offline.reset_download_queue),
            ("server logout", lambda: get_provider().server_logout()),
        ):
            try:
                fn()
            except Exception as e:
                logger.warning("sign out: %s failed: %s", what, e)
        settings = self._settings()
        settings.access_token = ""
        settings.user_id = ""
        settings.username = ""
        settings.flush()
        try:
            from jellytoast.jellyfin_api import get_api

            api = get_api()
            api.token = ""
            api.user_id = ""
        except Exception:
            pass
        reset_provider()
        for fn in (image_cache.clear, disk_cache.clear_all):
            try:
                fn()
            except Exception:
                pass
        self._signed_in, self._error = False, ""
        self.changed.emit()
        self.signedOutNow.emit()
