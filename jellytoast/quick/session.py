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

    QC_POLL_MS = 3000
    QC_TIMEOUT_S = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QTimer

        from jellytoast.providers import get_provider

        self._signed_in = bool(get_provider().is_authenticated)
        self._busy = False
        self._error = ""
        self._gen = 0
        # Quick Connect (Jellyfin): show a code, poll until approved.
        self._qc_code = ""
        self._qc_server = self._qc_secret = ""
        self._qc_deadline = 0.0
        self._qc_inflight = False
        self._qc_timer = QTimer(self)
        self._qc_timer.setInterval(self.QC_POLL_MS)
        self._qc_timer.timeout.connect(self._qc_poll)

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

    quickConnectCode = Property(str, lambda self: self._qc_code, notify=changed)

    def _fail(self, msg: str) -> None:
        self._qc_stop()
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
        from jellytoast import server_connect

        self._gen += 1
        gen = self._gen
        self._busy, self._error = True, ""
        self.changed.emit()
        # The shared path (jellytoast/server_connect.py): clean up the
        # address, find the server — either kind; the chip only breaks a tie —
        # then authenticate.
        preferred = (kind or "").lower() or None

        def _done(found):
            if gen != self._gen:
                return
            self._signed_in_ok()

        def _err(e):
            if gen != self._gen:
                return
            self._fail(server_connect.message_for(e))

        async_io.run_async(
            server_connect.sign_in, url, username, password, preferred,
            on_result=_done, on_error=_err,
        )

    def _signed_in_ok(self) -> None:
        try:
            from jellytoast.scrobble import refresh_server_scrobble_flags

            refresh_server_scrobble_flags()
        except Exception as e:
            logger.warning("scrobble flag refresh failed: %s", e)
        self._qc_stop()
        self._busy, self._error, self._signed_in = False, "", True
        self.changed.emit()
        self.signedInNow.emit()

    # ── Quick Connect (Jellyfin) ──
    @Slot(str)
    def quickConnect(self, url: str) -> None:  # noqa: N802
        """Ask the server for a code to approve from a signed-in device."""
        if self._busy:
            return
        if _rig():
            self._fail(
                self.tr("Signing in is off in the dev rig — it would replace your real login.")
            )
            return
        server = _normalized(url)
        if not server:
            self._fail(self.tr("Please fill in the server URL."))
            return
        from jellytoast.providers import get_provider, reset_provider

        if get_provider().kind != "jellyfin":
            self._settings().provider_kind = "jellyfin"
            reset_provider()
        prov = get_provider()
        self._gen += 1
        gen = self._gen
        self._busy, self._error = True, ""
        self.changed.emit()

        def _start():
            if prov.probe(server) is None:
                raise LookupError("not-jellyfin")
            if not prov.quick_connect_available(server):
                raise PermissionError("quick-connect-off")
            return prov.quick_connect_start(server)

        def _started(result):
            if gen != self._gen:
                return
            import time

            self._qc_secret, self._qc_code = result
            self._qc_server = server
            self._qc_deadline = time.monotonic() + self.QC_TIMEOUT_S
            self._qc_inflight = False
            self._qc_timer.start()
            self.changed.emit()

        async_io.run_async(
            _start, on_result=_started, on_error=lambda e, g=gen: self._qc_failed(g, e)
        )

    def _qc_poll(self) -> None:
        import time

        if self._qc_inflight:
            return
        gen = self._gen
        if time.monotonic() > self._qc_deadline:
            self._qc_failed(gen, TimeoutError())
            return
        from jellytoast.providers import get_provider

        prov = get_provider()
        self._qc_inflight = True

        def _polled(ok):
            if gen != self._gen:
                return
            self._qc_inflight = False
            if not ok:
                return
            self._qc_timer.stop()
            async_io.run_async(
                prov.quick_connect_finish,
                self._qc_server,
                self._qc_secret,
                on_result=lambda _r, g=gen: g == self._gen and self._signed_in_ok(),
                on_error=lambda e, g=gen: self._qc_failed(g, e),
            )

        async_io.run_async(
            prov.quick_connect_approved,
            self._qc_server,
            self._qc_secret,
            on_result=_polled,
            on_error=lambda e, g=gen: self._qc_failed(g, e),
        )

    def _qc_failed(self, gen: int, err) -> None:
        if gen != self._gen:
            return
        if isinstance(err, TimeoutError):
            msg = self.tr("The code expired. Try again.")
        elif isinstance(err, LookupError):
            msg = self.tr("Couldn't find a Jellyfin server at that address.")
        elif isinstance(err, PermissionError):
            msg = self.tr("Quick Connect is turned off on this server.")
        else:
            msg = self.tr("Quick Connect failed: {0}").format(str(err) or err.__class__.__name__)
        self._fail(msg)

    def _qc_stop(self) -> None:
        self._qc_timer.stop()
        self._qc_code = self._qc_server = self._qc_secret = ""
        self._qc_inflight = False

    @Slot()
    def cancelQuickConnect(self) -> None:  # noqa: N802
        self._gen += 1
        self._qc_stop()
        self._busy = False
        self.changed.emit()

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
