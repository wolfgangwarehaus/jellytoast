"""Flatpak autostart backend — the XDG Background portal.

Inside the sandbox, writing ``~/.config/autostart`` lands in the app's
private ``~/.var/app/<id>/config`` (host login never reads it), and
Flathub rejects a real ``xdg-config/autostart`` filesystem grant. The
sanctioned route is ``org.freedesktop.portal.Background.RequestBackground``
— portal access is automatic, no finish-args needed
(docs/research/flatpak_manifest_2026-06-11.md §permission table).

Shape mirrors ``jellytoast.color_picker``: jeepney over the session bus
(this PySide6 build's QtDBus can't demarshal portal ``a{sv}`` Responses),
dispatched on a worker via ``async_io.run_async`` because the portal may
show an interactive consent prompt — blocking the GUI thread on that is
not acceptable.

Contract drift vs the filesystem backend, by design:

- ``enable()``/``disable()`` return True when the request was
  *dispatched*; the grant arrives later on the Response signal. On a
  denial the worker flips ``settings.autostart`` back so the next
  Settings open shows the truth.
- ``is_enabled()`` reports the persisted intent — the portal offers no
  read-back API.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_BACKGROUND_IFACE = "org.freedesktop.portal.Background"
_REQUEST_IFACE = "org.freedesktop.portal.Request"
# The portal may show a consent prompt; the Response only arrives after
# the user answers. Generous cap so the worker can't hang forever.
_RESPONSE_TIMEOUT_S = 120


def is_supported() -> bool:
    try:
        import jeepney  # noqa: F401
        import jeepney.io.blocking  # noqa: F401

        return True
    except Exception:
        return False


def is_enabled() -> bool:
    from jellytoast.settings import get_settings

    return bool(get_settings().autostart)


def enable() -> bool:
    return _dispatch(True)


def disable() -> bool:
    return _dispatch(False)


def _dispatch(autostart: bool) -> bool:
    """Fire the portal request on a worker. True = dispatched (not yet
    granted); False = could not even dispatch."""
    if not is_supported():
        return False
    try:
        from jellytoast.async_io import run_async

        run_async(
            lambda: _request_background(autostart),
            on_result=lambda granted: _on_response(autostart, granted),
            on_error=lambda e: log.warning("autostart portal request failed: %s", e),
        )
        return True
    except Exception:
        log.exception("autostart: could not dispatch portal request")
        return False


def _on_response(requested: bool, granted: bool | None) -> None:
    """Reconcile persisted intent with the portal's answer. ``None``
    means timeout/cancel — leave intent alone (retried next toggle)."""
    if granted is None:
        log.info("autostart portal: no response (timeout or cancel)")
        return
    log.info("autostart portal: requested=%s granted=%s", requested, granted)
    if requested and not granted:
        from jellytoast.settings import get_settings

        get_settings().autostart = False


def build_options(autostart: bool) -> dict:
    """The ``a{sv}`` options for RequestBackground, jeepney-style
    (``(signature, value)`` variant tuples). Split out for tests."""
    return {
        "reason": ("s", "Launch jellytoast at login"),
        "autostart": ("b", autostart),
        "commandline": ("as", ["jellytoast"]),
        "dbus-activatable": ("b", False),
    }


def _request_background(autostart: bool) -> bool | None:
    """Blocking portal round-trip (worker thread). Returns the granted
    autostart state, or None on timeout/cancel."""
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    try:
        background = DBusAddress(
            _PORTAL_PATH, bus_name=_PORTAL_SERVICE, interface=_BACKGROUND_IFACE
        )
        reply = conn.send_and_get_reply(
            new_method_call(
                background,
                "RequestBackground",
                "sa{sv}",
                ("", build_options(autostart)),
            )
        )
        request_path = reply.body[0]
        rule = MatchRule(
            type="signal",
            interface=_REQUEST_IFACE,
            member="Response",
            path=request_path,
        )
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule) as queue:
            msg = conn.recv_until_filtered(queue, timeout=_RESPONSE_TIMEOUT_S)
        code, results = msg.body
        if code != 0:  # 0 = success; 1 = user cancelled; 2 = other
            return None
        granted = results.get("autostart") if hasattr(results, "get") else None
        if isinstance(granted, tuple) and len(granted) == 2:
            granted = granted[1]  # ('b', value) variant shape
        return bool(granted)
    finally:
        conn.close()
