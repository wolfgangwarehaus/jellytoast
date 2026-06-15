"""Linux keep-awake backend.

Inhibits the session's idle/suspend behaviour via the freedesktop
``org.freedesktop.ScreenSaver`` service while audio plays. Cookie-based:
``Inhibit`` returns a uint token that ``UnInhibit`` releases. This is the
interface mpv/browsers use for "don't idle while media plays"; on
KDE/GNOME it suppresses idle screen-lock and idle-suspend. Best-effort —
on a minimal WM with no provider it silently no-ops (``is_supported`` is
False and the controller never calls through).

Uses QtDBus (synchronous, GUI-thread-safe) rather than the async
dbus-next loop the MPRIS backend runs, so a one-shot inhibit needs no
extra event loop.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SERVICE = "org.freedesktop.ScreenSaver"
_PATH = "/org/freedesktop/ScreenSaver"
_cookie: Optional[int] = None


def _iface():
    """A live QDBusInterface to the ScreenSaver service, or None if the
    session bus / service is unavailable."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        iface = QDBusInterface(_SERVICE, _PATH, _SERVICE, bus)
        return iface if iface.isValid() else None
    except Exception as e:  # pragma: no cover — QtDBus absent / odd build
        logger.debug("ScreenSaver iface unavailable: %s", e)
        return None


def is_supported() -> bool:
    return _iface() is not None


def inhibit() -> bool:
    global _cookie
    if _cookie is not None:
        return True
    iface = _iface()
    if iface is None:
        return False
    try:
        reply = iface.call("Inhibit", "jellytoast", "Playing music")
        args = reply.arguments()
        if not args:
            return False
        _cookie = int(args[0])
        return True
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("ScreenSaver Inhibit failed: %s", e)
        return False


def release() -> None:
    global _cookie
    if _cookie is None:
        return
    iface = _iface()
    if iface is not None:
        try:
            iface.call("UnInhibit", _cookie)
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("ScreenSaver UnInhibit failed: %s", e)
    _cookie = None
