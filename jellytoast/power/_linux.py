"""Linux keep-awake backend: hold off system *sleep* while audio plays.

Tries, in order:

1. ``org.freedesktop.PowerManagement.Inhibit`` (KDE PowerDevil — incl.
   Plasma Mobile — and Xfce): a sleep inhibitor; PowerDevil maps it to
   ``InterruptSession``.
2. The XDG portal ``org.freedesktop.portal.Inhibit`` with the *suspend*
   flag: GNOME, and the only route inside the flatpak sandbox.

NOT ``org.freedesktop.ScreenSaver.Inhibit``, which this module used to call:
on KDE that is a ``ChangeScreenSettings`` inhibition — it keeps the *screen*
on (no dim, no DPMS, no auto-lock) and does not block suspend at all. Music
needs the opposite. On a phone (Plasma Mobile) the old call meant the screen
never turned off while playing.

Both holds are released by DISCONNECTING a connection that exists only to
carry the hold — the inhibit services drop a caller's inhibitions when it
leaves the bus. That sidesteps calling ``UnInhibit(u)``: PySide6's QtDBus
can't marshal a uint32 (a Python int goes out as int32, the call resolves to
a non-existent ``UnInhibit(i)``, and the hold silently never lifts — the old
ScreenSaver release had exactly that bug). The portal needs a uint32 in the
*call* (``flags``), so it goes through dbus-next, which types it properly.

PowerDevil activates an inhibition only after it has been held ~5 s, so a
quick pause/resume never flickers it. Best-effort throughout: with no
provider ``is_supported`` is False and the controller never calls through.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PM_SERVICE = "org.freedesktop.PowerManagement.Inhibit"
_PM_PATH = "/org/freedesktop/PowerManagement/Inhibit"
_PM_IFACE = "org.freedesktop.PowerManagement.Inhibit"
_PM_CONN = "jellytoast-keepawake"

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_PORTAL_IFACE = "org.freedesktop.portal.Inhibit"
_PORTAL_SUSPEND = 4  # flags: 1 logout, 2 user switch, 4 suspend, 8 idle

_REASON = "Playing music"
_TIMEOUT_S = 5.0

# "pm" or "portal" while a hold is live.
_hold: Optional[str] = None
_portal = None  # _PortalHold while the portal path holds


def _session_has(name: str) -> bool:
    try:
        from PySide6.QtDBus import QDBusConnection

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        reply = bus.interface().isServiceRegistered(name)
        return bool(reply.value()) if reply.isValid() else False
    except Exception as e:  # pragma: no cover — QtDBus absent / odd build
        logger.debug("session bus unavailable: %s", e)
        return False


def is_supported() -> bool:
    return _session_has(_PM_SERVICE) or _session_has(_PORTAL_SERVICE)


# ── PowerManagement.Inhibit (QtDBus, private connection) ───────────────


def _inhibit_pm() -> bool:
    if not _session_has(_PM_SERVICE):
        return False
    from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

    conn = QDBusConnection.connectToBus(QDBusConnection.BusType.SessionBus, _PM_CONN)
    if not conn.isConnected():
        QDBusConnection.disconnectFromBus(_PM_CONN)
        return False
    reply = QDBusInterface(_PM_SERVICE, _PM_PATH, _PM_IFACE, conn).call(
        "Inhibit", "jellytoast", _REASON
    )
    if reply.type() != QDBusMessage.MessageType.ReplyMessage:
        logger.debug("PowerManagement Inhibit failed: %s", reply.errorMessage())
        del conn
        QDBusConnection.disconnectFromBus(_PM_CONN)
        return False
    return True


def _release_pm() -> None:
    from PySide6.QtDBus import QDBusConnection

    # Closes the private connection (no QDBusConnection objects survive
    # _inhibit_pm), and PowerDevil drops the hold with it.
    QDBusConnection.disconnectFromBus(_PM_CONN)


# ── Portal Inhibit (dbus-next on its own small loop thread) ────────────


class _PortalHold:
    """One portal inhibition on a dedicated connection + asyncio thread.
    Created only when PowerManagement isn't on the bus, so KDE never
    spins this thread up."""

    def __init__(self):
        import asyncio
        import threading

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="jt-keepawake", daemon=True
        )
        self._thread.start()
        self._bus = None
        self._handle = ""

    def _run(self, coro):
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(_TIMEOUT_S)

    async def _inhibit(self) -> bool:
        from dbus_next import BusType, Message, MessageType, Variant
        from dbus_next.aio import MessageBus

        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        reply = await self._bus.call(
            Message(
                destination=_PORTAL_SERVICE,
                path=_PORTAL_PATH,
                interface=_PORTAL_IFACE,
                member="Inhibit",
                signature="sua{sv}",
                body=["", _PORTAL_SUSPEND, {"reason": Variant("s", _REASON)}],
            )
        )
        if reply.message_type != MessageType.METHOD_RETURN:
            logger.debug("portal Inhibit failed: %s", reply.body)
            return False
        self._handle = reply.body[0]
        return True

    async def _close(self):
        from dbus_next import Message

        try:
            if self._bus is not None and self._handle:
                await self._bus.call(
                    Message(
                        destination=_PORTAL_SERVICE,
                        path=self._handle,
                        interface="org.freedesktop.portal.Request",
                        member="Close",
                    )
                )
        finally:
            if self._bus is not None:
                self._bus.disconnect()

    def inhibit(self) -> bool:
        try:
            return self._run(self._inhibit())
        except Exception as e:
            logger.debug("portal Inhibit failed: %s", e)
            self.close()
            return False

    def close(self):
        try:
            self._run(self._close())
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("portal release failed: %s", e)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


def _inhibit_portal() -> bool:
    global _portal
    if not _session_has(_PORTAL_SERVICE):
        return False
    hold = _PortalHold()
    if hold.inhibit():
        _portal = hold
        return True
    return False


def _release_portal() -> None:
    global _portal
    hold, _portal = _portal, None
    if hold is not None:
        hold.close()


# ── Facade ─────────────────────────────────────────────────────────────


def inhibit() -> bool:
    global _hold
    if _hold is not None:
        return True
    for kind, attempt in (("pm", _inhibit_pm), ("portal", _inhibit_portal)):
        try:
            ok = attempt()
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("%s keep-awake failed: %s", kind, e)
            ok = False
        if ok:
            _hold = kind
            return True
    return False


def release() -> None:
    global _hold
    kind, _hold = _hold, None
    try:
        if kind == "pm":
            _release_pm()
        elif kind == "portal":
            _release_portal()
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("keep-awake release failed: %s", e)
