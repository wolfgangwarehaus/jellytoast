"""Read the desktop's accent colour (0.1.7 P1b; Windows backend added later).

Linux: ``org.freedesktop.portal.Settings.ReadOne("org.freedesktop.appearance",
"accent-color")`` returns a ``(ddd)`` sRGB triple in [0,1] (or a negative
sentinel when unset). Read via **jeepney** on an ``async_io`` worker — QtDBus
can't demarshal the struct in this PySide6 build (same reason as the eyedropper,
see ``color_picker``). KDE Plasma + GNOME 47+ expose the key; older / other DEs
return None and the caller just leaves the accent as-is.

Windows: the accent is ``HKCU\\Software\\Microsoft\\Windows\\DWM\\AccentColor``
(a 0xAABBGGRR DWORD); live changes broadcast ``WM_DWMCOLORIZATIONCOLORCHANGED``,
caught with an app-wide native event filter (same pattern as ``taskbar``).
UNVERIFIED on a real Windows box yet — ships behind the needs:windows checklist.

macOS: ``NSColor.controlAccentColor`` (10.14+) is the live accent; changes post
``AppleColorPreferencesChangedNotification`` on the distributed notification
center. Read + observed via pyobjc (already a mac dependency — see
``macos_window``). UNVERIFIED on a real Mac yet — ships behind needs:mac.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Slot

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"

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


def _abgr_to_hex(dword: int) -> str | None:
    """Windows DWM ``AccentColor`` DWORD (0xAABBGGRR) → ``#rrggbb``."""
    try:
        v = int(dword)
    except (TypeError, ValueError):
        return None
    if not 0 <= v <= 0xFFFFFFFF:
        return None
    r = v & 0xFF
    g = (v >> 8) & 0xFF
    b = (v >> 16) & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def _read_windows_accent() -> str | None:
    """HKCU DWM AccentColor → ``#rrggbb``, or None. Never raises."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM"
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AccentColor")
        return _abgr_to_hex(value)
    except Exception:
        return None


def _read_macos_accent() -> str | None:
    """macOS ``NSColor.controlAccentColor`` → ``#rrggbb``, or None. Reads AppKit,
    so call on the GUI/main thread (see ``_sync_now``). Never raises."""
    try:
        from AppKit import NSColor, NSColorSpace

        c = NSColor.controlAccentColor()
        rgb = c.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb is None:
            return None
        r = int(round(rgb.redComponent() * 255))
        g = int(round(rgb.greenComponent() * 255))
        b = int(round(rgb.blueComponent() * 255))
        return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"
    except Exception:
        return None


def read_system_accent() -> str | None:
    """Read the OS accent → ``#rrggbb`` or None (unset / unsupported). Never
    raises. Linux/Windows do a blocking read (D-Bus / registry) → call on a
    worker; macOS reads AppKit → call on the GUI thread (``_sync_now`` routes
    each correctly)."""
    if _IS_MACOS:
        return _read_macos_accent()
    if _IS_WINDOWS:
        return _read_windows_accent()
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


def follow_accent_active() -> bool:
    """Whether the OS accent should drive the app accent RIGHT NOW: the toggle
    is on AND the built-in jellytoast family is active. Preset / imported
    families supply their own accent — letting the OS accent through would
    silently mismatch their palette, and the Settings toggle is hidden for
    those families so the user couldn't even see why."""
    from jellytoast.settings import get_settings

    s = get_settings()
    # The built-in family is stored as EITHER "" or the "jellytoast" sentinel
    # (the family dropdown persists the latter) — treat both as built-in, same
    # as family_label()/family_has_both().
    return bool(s.follow_system_accent) and s.theme_family in ("", "jellytoast")


def apply_accent_now(hex_color: str) -> None:
    """Apply a hex accent app-wide from a NON-dialog context (launch re-read /
    the live watcher): persist it to ``ui/accent_color``, drop any stale
    accent-family override so the cascade re-derives cleanly, then refresh the
    theme + icons and broadcast ``theme_changed``. GUI thread only. Mirrors the
    accent picker's apply path (``_on_accent_picked``) minus the dialog bits."""
    if not hex_color:
        return
    try:
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih
        from jellytoast.player_state import PlayerBus
        from jellytoast.settings import get_settings
        from jellytoast.settings_migration import open_qsettings

        get_settings().accent_color = hex_color
        qs = open_qsettings()
        for tok in ("ACCENT", "ACCENT_DEEP", "BORDER_ACCENT"):
            qs.remove(f"debug/colors/{tok}")
        _uih.refresh_theme()
        _icons.refresh_theme()
        PlayerBus.get().theme_changed.emit()
    except Exception:
        pass


# Broadcast to every top-level window when the DWM colorization (accent)
# changes — Settings → Personalization → Colors, or an accent-syncing tool.
_WM_DWMCOLORIZATIONCOLORCHANGED = 0x0320


def _make_accent_filter(on_changed):
    """App-wide native event filter firing ``on_changed`` on
    ``WM_DWMCOLORIZATIONCOLORCHANGED`` — the Windows analogue of the portal's
    SettingChanged signal (same filter shape as ``taskbar._ButtonCreatedFilter``)."""
    from PySide6.QtCore import QAbstractNativeEventFilter

    class _Filter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):
            # Runs for EVERY Windows message — bail before touching MSG unless
            # it's the generic channel.
            if event_type != b"windows_generic_MSG":
                return False, 0
            try:
                import ctypes.wintypes as wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == _WM_DWMCOLORIZATIONCOLORCHANGED:
                    on_changed()
            except Exception:
                pass
            return False, 0

    return _Filter()


def resync_system_accent() -> None:
    """Re-read the OS accent and apply it (platform-correct read path). GUI
    thread only. Shared by the follower's launch/live sync and by the Settings
    dialog when switching back to the built-in family with Follow on (the live
    watcher only fires on OS-side changes, so an in-app family switch needs an
    explicit resync or the preset accent lingers until restart)."""
    if _IS_MACOS:
        # AppKit reads must stay on the GUI/main thread, and the read is cheap
        # — do it inline instead of on a worker.
        h = read_system_accent()
        if h:
            apply_accent_now(h)
        return
    from jellytoast.async_io import run_async

    run_async(
        read_system_accent,
        on_result=lambda h: apply_accent_now(h) if h else None,
        on_error=lambda _e: None,
    )


class SystemAccentFollower(QObject):
    """Keeps jellytoast's accent in sync with the desktop's while
    ``settings.follow_system_accent`` is on: applies it once at :meth:`start`
    and again whenever the portal's ``accent-color`` changes.

    The change NOTIFICATION uses QtDBus (event-loop-integrated, no worker
    thread — so it respects the no-raw-threads rule); the actual value is
    RE-READ via jeepney on an ``async_io`` worker, because QtDBus can't
    demarshal the ``(ddd)`` struct in this PySide6 build. Everything is
    best-effort and wrapped — a DE without the portal (or a build where the
    signal doesn't deliver) simply never fires, and the launch-time read still
    covers "follow on every launch"."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._subscribed = False

    def start(self) -> None:
        if follow_accent_active():
            self._sync_now()
        self._subscribe()  # always listen; the handler gates on the live setting

    def _sync_now(self) -> None:
        resync_system_accent()

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        if _IS_MACOS:
            self._subscribe_macos()
            return
        if _IS_WINDOWS:
            self._subscribe_windows()
            return
        try:
            from PySide6.QtCore import SLOT
            from PySide6.QtDBus import QDBusConnection

            # QtDBus needs the receiver + a SLOT() signature string (6 args). The
            # bound-method form (5 args) raises TypeError in this PySide6 build —
            # which the except below used to swallow, silently leaving the live
            # accent watch UNSUBSCRIBED (only the launch re-read worked). The
            # signature must match _on_setting_changed's
            # ``@Slot(str, str, "QDBusVariant")``. Verified live on KDE Plasma:
            # changing the system accent delivers this signal and we re-read it.
            ok = QDBusConnection.sessionBus().connect(
                _PORTAL_SERVICE,
                _PORTAL_PATH,
                _SETTINGS_IFACE,
                "SettingChanged",
                self,
                SLOT("_on_setting_changed(QString,QString,QDBusVariant)"),
            )
            self._subscribed = bool(ok)
        except Exception:
            self._subscribed = False

    def _subscribe_macos(self) -> None:
        """Observe ``AppleColorPreferencesChangedNotification`` on the distributed
        notification center (posted when the user changes the accent in System
        Settings). Delivered on the main run loop, which is Qt's event loop on
        macOS, so the handler lands on the GUI thread. Gated like the others."""
        try:
            from Foundation import NSDistributedNotificationCenter

            center = NSDistributedNotificationCenter.defaultCenter()
            # Block-based observer → no NSObject subclass needed. Keep the
            # returned token alive (pinned on self) or the observation drops.
            self._mac_observer = center.addObserverForName_object_queue_usingBlock_(
                "AppleColorPreferencesChangedNotification",
                None,
                None,
                lambda _note: self._on_macos_accent_changed(),
            )
            self._subscribed = True
        except Exception:
            self._subscribed = False

    def _on_macos_accent_changed(self) -> None:
        try:
            if follow_accent_active():
                self._sync_now()
        except Exception:
            pass

    def _subscribe_windows(self) -> None:
        """Install the app-wide DWM-colorization filter. The handler gates on
        the live setting + family (``follow_accent_active``), same contract as
        the portal path; the re-read goes through the registry on a worker."""
        try:
            from PySide6.QtWidgets import QApplication

            self._win_filter = _make_accent_filter(self._on_windows_accent_changed)
            app = QApplication.instance()
            if app is None:
                return
            app.installNativeEventFilter(self._win_filter)
            self._subscribed = True
        except Exception:
            self._subscribed = False

    def _on_windows_accent_changed(self) -> None:
        try:
            if follow_accent_active():
                self._sync_now()
        except Exception:
            pass

    @Slot(str, str, "QDBusVariant")
    def _on_setting_changed(self, namespace, key, _value=None) -> None:
        try:
            if (
                namespace == _APPEARANCE
                and key == _ACCENT_KEY
                and follow_accent_active()
            ):
                self._sync_now()  # re-read the value via jeepney (handles the struct)
        except Exception:
            pass
