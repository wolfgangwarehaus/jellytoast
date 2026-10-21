"""Last.fm and ListenBrainz sign-in for the Qt Quick Settings page — the
same steps and settings keys as the desktop Settings → Scrobbling page.

Last.fm: get a request token, open Last.fm's approval page in the browser,
then poll for the session every 3 s (up to 3 minutes). ListenBrainz: paste
the user token from listenbrainz.org/settings and validate it."""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from jellytoast import async_io

logger = logging.getLogger(__name__)

_POLL_MS = 3000
_POLL_FOR_S = 180


def _settings():
    from jellytoast.settings import get_settings

    return get_settings()


class Scrobbling(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lf_status = ""
        self._lb_status = ""
        self._lf_token = ""
        self._lf_deadline = 0.0
        self._lf_busy = False
        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_MS)
        self._poll.timeout.connect(self._lf_poll)

    # ── Last.fm ──
    lastfmUser = Property(str, lambda self: _settings().lastfm_username or "", notify=changed)
    lastfmConnected = Property(bool, lambda self: bool(_settings().lastfm_session_key), notify=changed)
    lastfmWaiting = Property(bool, lambda self: self._poll.isActive(), notify=changed)
    lastfmStatus = Property(str, lambda self: self._lf_status, notify=changed)

    def _set_lf(self, status: str) -> None:
        self._lf_status = status
        self.changed.emit()

    @Slot()
    def connectLastfm(self) -> None:  # noqa: N802
        from jellytoast.scrobble import lastfm as _lf

        self._set_lf(self.tr("Asking Last.fm…"))

        def _token(token):
            if not token:
                self._set_lf(self.tr("Couldn't reach Last.fm — try again later."))
                return
            self._lf_token = token
            self._lf_deadline = time.monotonic() + _POLL_FOR_S
            QDesktopServices.openUrl(QUrl(_lf.auth_url(token)))
            self._poll.start()
            self._set_lf(self.tr("Approve jellytoast in the browser, then come back here."))

        async_io.run_async(_lf.get_token, on_result=_token, on_error=lambda _e: _token(None))

    def _lf_poll(self) -> None:
        from jellytoast.scrobble import lastfm as _lf

        if self._lf_busy:
            return
        if time.monotonic() > self._lf_deadline:
            self._poll.stop()
            self._set_lf(self.tr("Timed out waiting for Last.fm. Try again."))
            return
        self._lf_busy = True

        def _session(sess):
            self._lf_busy = False
            if not sess or not sess.get("key"):
                return
            self._poll.stop()
            st = _settings()
            st.lastfm_session_key = sess["key"]
            st.lastfm_username = sess.get("name", "")
            st.lastfm_enabled = True
            self._set_lf("")

        def _err(_e):
            self._lf_busy = False

        async_io.run_async(_lf.get_session, self._lf_token, on_result=_session, on_error=_err)

    @Slot()
    def cancelLastfm(self) -> None:  # noqa: N802
        self._poll.stop()
        self._set_lf("")

    @Slot()
    def disconnectLastfm(self) -> None:  # noqa: N802
        st = _settings()
        st.lastfm_session_key = ""
        st.lastfm_username = ""
        st.lastfm_enabled = False
        self._set_lf("")

    # ── ListenBrainz ──
    listenbrainzUser = Property(
        str, lambda self: _settings().listenbrainz_username or "", notify=changed
    )
    listenbrainzConnected = Property(
        bool,
        lambda self: bool(_settings().listenbrainz_token and _settings().listenbrainz_username),
        notify=changed,
    )
    listenbrainzStatus = Property(str, lambda self: self._lb_status, notify=changed)

    @Slot(str)
    def connectListenbrainz(self, token: str) -> None:  # noqa: N802
        from jellytoast.scrobble import listenbrainz as _lb

        token = (token or "").strip()
        if not token:
            self._lb_status = self.tr("Paste your token first.")
            self.changed.emit()
            return
        st = _settings()
        url = st.listenbrainz_url or _lb.DEFAULT_BASE_URL
        self._lb_status = self.tr("Checking…")
        self.changed.emit()

        def _done(username):
            if username:
                st.listenbrainz_token = token
                st.listenbrainz_username = username
                st.listenbrainz_enabled = True
                self._lb_status = ""
            else:
                self._lb_status = self.tr("That token didn't work — check it and try again.")
            self.changed.emit()

        async_io.run_async(_lb.validate_token, token, url, on_result=_done, on_error=lambda _e: _done(None))

    @Slot()
    def disconnectListenbrainz(self) -> None:  # noqa: N802
        st = _settings()
        st.listenbrainz_token = ""
        st.listenbrainz_username = ""
        st.listenbrainz_enabled = False
        self._lb_status = ""
        self.changed.emit()
