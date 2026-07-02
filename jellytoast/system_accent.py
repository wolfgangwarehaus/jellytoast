"""Read the desktop's accent colour from the XDG portal (0.1.7 P1b).

``org.freedesktop.portal.Settings.ReadOne("org.freedesktop.appearance",
"accent-color")`` returns a ``(ddd)`` sRGB triple in [0,1] (or a negative
sentinel when unset). Read via **jeepney** on an ``async_io`` worker — QtDBus
can't demarshal the struct in this PySide6 build (same reason as the eyedropper,
see ``color_picker``). This feeds the accent path so jellytoast can *follow the
OS accent*; light/dark already rides ``QStyleHints`` via the ``auto`` theme mode.

Cross-DE: KDE Plasma + GNOME 47+ expose this key; older / other DEs return None
and the caller just leaves the accent as-is. Windows/macOS accent-follow needs
per-OS backends and is gated behind the needs:windows / needs:mac boxes.
"""

from __future__ import annotations

_APPEARANCE = "org.freedesktop.appearance"
_ACCENT_KEY = "accent-color"
_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SETTINGS_IFACE = "org.freedesktop.portal.Settings"


def _jeepney_available() -> bool:
    try:
        import jeepney  # noqa: F401
        import jeepney.io.blocking  # noqa: F401

        return True
    except Exception:
        return False


def _accent_from_variant(v) -> str | None:
    """jeepney parses the ``ReadOne`` reply variant as ``("(ddd)", (r, g, b))``;
    tolerate a bare ``(r, g, b)`` too. Returns ``#rrggbb``, or None when unset —
    the portal spec uses an all-negative triple (e.g. ``(-1, -1, -1)``) for
    "no accent configured"."""
    comps = (
        v[1]
        if (isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], (tuple, list)))
        else v
    )
    if not (isinstance(comps, (tuple, list)) and len(comps) == 3):
        return None
    try:
        r, g, b = float(comps[0]), float(comps[1]), float(comps[2])
    except (TypeError, ValueError):
        return None
    if min(r, g, b) < 0.0 or max(r, g, b) > 1.0:  # unset / out-of-range sentinel
        return None
    from jellytoast.color_picker import rgb01_to_hex

    return rgb01_to_hex(r, g, b)


def read_system_accent() -> str | None:
    """Blocking portal read — call on a worker via ``async_io.run_async``.
    Returns ``#rrggbb`` or None (unset / no portal / no jeepney). Never raises."""
    if not _jeepney_available():
        return None
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        conn = open_dbus_connection(bus="SESSION")
        try:
            addr = DBusAddress(
                _PORTAL_PATH, bus_name=_PORTAL_SERVICE, interface=_SETTINGS_IFACE
            )
            reply = conn.send_and_get_reply(
                new_method_call(addr, "ReadOne", "ss", (_APPEARANCE, _ACCENT_KEY))
            )
            body = reply.body
            return _accent_from_variant(body[0] if isinstance(body, tuple) and body else body)
        finally:
            conn.close()
    except Exception:
        return None
