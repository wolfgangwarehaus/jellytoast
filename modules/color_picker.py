"""Screen colour picker (eyedropper).

Samples a colour from anywhere on screen and hands it back as ``#rrggbb``.

Wayland clients can't read the screen directly, so on Wayland this drives the
XDG desktop portal ``org.freedesktop.portal.Screenshot.PickColor`` — the same
native one-click eyedropper KDE / GNOME ship. The portal is a D-Bus
request/response round-trip that blocks until the user clicks a pixel, so it
runs on a worker thread (via ``modules.async_io.run_async``) and the result is
delivered back on the GUI thread through ``on_picked``.

We use **jeepney** for the D-Bus work rather than QtDBus: this PySide6 build's
``QDBusConnection.connect`` only fires a ``QDBusMessage`` slot and then hands
back a ``QDBusArgument`` whose ``asVariant()`` is unreliable, so the portal
``a{sv}`` Response couldn't be demarshalled. jeepney (already present as a
keyring/secretstorage transitive dep) parses it cleanly. On X11 / no portal /
no jeepney it falls back to ``QColorDialog``.

Public API::

    pick_screen_color(on_picked, parent=None)

``on_picked`` is called with a ``"#rrggbb"`` string once the user clicks a
pixel; it is NOT called if the pick is cancelled or unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtGui import QColor

log = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
_REQUEST_IFACE = "org.freedesktop.portal.Request"
# Safety cap so a worker thread can't block forever if the portal never
# answers (it normally returns the moment the picker closes).
_PICK_TIMEOUT_S = 120


def pick_screen_color(on_picked: Callable[[str], None], parent=None) -> None:
    """Open the OS screen eyedropper. Calls ``on_picked('#rrggbb')`` once the
    user clicks a pixel; does nothing if cancelled or unavailable.

    Non-blocking: the portal round-trip runs on a worker thread; on_picked
    fires on the GUI thread."""
    if _jeepney_available():
        try:
            from modules.async_io import run_async

            run_async(
                _portal_pick,
                on_result=lambda hex_color: on_picked(hex_color) if hex_color else None,
                on_error=lambda e: log.warning("colorpick: portal pick failed: %s", e),
            )
            return
        except Exception:
            log.exception("colorpick: could not dispatch portal pick")
    _pick_via_dialog(on_picked, parent)


def rgb01_to_hex(r: float, g: float, b: float) -> str:
    """Portal components are 0..1 doubles — clamp + scale to ``#rrggbb``."""

    def _ch(x: float) -> int:
        return max(0, min(255, round(float(x) * 255)))

    return QColor(_ch(r), _ch(g), _ch(b)).name()


def _color_from_results(results) -> str | None:
    """Pull ``color`` out of a jeepney-parsed portal Response results dict and
    return ``#rrggbb``. jeepney represents the ``(ddd)`` variant as a
    ``(signature, (r, g, b))`` tuple; tolerate a bare ``(r, g, b)`` too."""
    color = results.get("color") if hasattr(results, "get") else None
    if not color:
        return None
    comps = color[1] if (isinstance(color, tuple) and len(color) == 2) else color
    try:
        return rgb01_to_hex(comps[0], comps[1], comps[2])
    except (TypeError, IndexError, ValueError):
        log.warning("colorpick: unexpected color shape %r", color)
        return None


def _jeepney_available() -> bool:
    try:
        import jeepney  # noqa: F401
        import jeepney.io.blocking  # noqa: F401

        return True
    except Exception:
        return False


def _portal_pick() -> str | None:
    """Blocking: drive Screenshot.PickColor over D-Bus and return the picked
    ``#rrggbb`` (or None if cancelled). Runs on a worker thread."""
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    try:
        screenshot = DBusAddress(
            _PORTAL_PATH, bus_name=_PORTAL_SERVICE, interface=_SCREENSHOT_IFACE
        )
        reply = conn.send_and_get_reply(
            new_method_call(screenshot, "PickColor", "sa{sv}", ("", {}))
        )
        request_path = reply.body[0]
        log.info("colorpick: PickColor dispatched, request=%s", request_path)
        rule = MatchRule(
            type="signal",
            interface=_REQUEST_IFACE,
            member="Response",
            path=request_path,
        )
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule) as queue:
            msg = conn.recv_until_filtered(queue, timeout=_PICK_TIMEOUT_S)
        code, results = msg.body
        log.info("colorpick: Response code=%s", code)
        if code != 0:  # 0 = success; 1 = cancelled; 2 = other
            return None
        return _color_from_results(results)
    finally:
        conn.close()


def _pick_via_dialog(on_picked: Callable[[str], None], parent) -> None:
    """Fallback for X11 / no-portal / no-jeepney: QColorDialog."""
    try:
        from PySide6.QtWidgets import QColorDialog

        log.info("colorpick: portal unavailable, falling back to QColorDialog")
        c = QColorDialog.getColor(parent=parent)
        if c.isValid():
            on_picked(c.name())
    except Exception:
        log.exception("colorpick: QColorDialog fallback failed")
