"""Cross-platform detection. One source of truth for OS / desktop / display-
server checks so feature gates don't drift across modules.

Two flavors of check:
  • module-level constants (`IS_LINUX`, `IS_WINDOWS`, `IS_MACOS`) — safe to
    use anywhere, evaluated once at import.
  • runtime helpers (`is_wayland`, `is_x11`, `is_kde_desktop`,
    `is_kde_wayland`) — call these because env vars or the Qt platform name
    can change between import and QApplication construction.

`will_be_wayland()` is the pre-QApplication probe used by code paths that
run before QApplication exists (env-var bootstraps, locale fixes). Once
QApplication is up, `is_wayland()` is more reliable because it reflects
what Qt actually picked, including QT_QPA_PLATFORM overrides.
"""

from __future__ import annotations

import os
import sys

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def is_msix_packaged() -> bool:
    """True iff running inside an MSIX/AppX package (the process has package
    identity). False for the Inno .exe, pip/pipx installs, and source runs —
    so it's the single gate for the handful of Windows behaviours that differ
    when packaged: the package supplies the AUMID + Start-menu entry, the
    install dir is read-only, and Run-key autostart is ignored. No-op off
    Windows.

    Detection: ``GetCurrentPackageFullName`` returns
    ``APPMODEL_ERROR_NO_PACKAGE`` (15700) when there is no package identity."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(
            ctypes.byref(length), None
        )
        return rc != 15700  # 15700 == APPMODEL_ERROR_NO_PACKAGE
    except Exception:
        return False


def will_be_wayland() -> bool:
    """Pre-QApplication Wayland probe. Honors an explicit
    QT_QPA_PLATFORM override; falls back to WAYLAND_DISPLAY. Always
    False off Linux."""
    if not IS_LINUX:
        return False
    plat = os.environ.get("QT_QPA_PLATFORM", "")
    if plat.startswith("xcb"):
        return False
    if plat.startswith("wayland"):
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def is_wayland() -> bool:
    """Runtime Wayland check. Uses QApplication.platformName() if Qt is
    up, otherwise falls back to the env-var probe."""
    if not IS_LINUX:
        return False
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            return app.platformName() == "wayland"
    except Exception:
        pass
    return will_be_wayland()


def is_x11() -> bool:
    """True iff we're on Linux and Qt is using the xcb platform plugin
    (native X11 or XWayland-via-xcb). False on Windows/macOS."""
    if not IS_LINUX:
        return False
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            return app.platformName() == "xcb"
    except Exception:
        pass
    return not will_be_wayland()


def is_kde_desktop() -> bool:
    """True if XDG_CURRENT_DESKTOP names KDE. Linux-only."""
    if not IS_LINUX:
        return False
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def desktop_name() -> str:
    """Human-ish current desktop from XDG_CURRENT_DESKTOP — the last (most
    specific) colon-component, e.g. 'KDE', 'GNOME', 'X-Cinnamon', 'XFCE'.
    Empty off Linux or when unset. Used for diagnostics / the blur reason
    string, not for feature gating (use the boolean helpers for that)."""
    if not IS_LINUX:
        return ""
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return raw.split(":")[-1].strip() if raw else ""


def is_kde_wayland() -> bool:
    """KDE Plasma running on Wayland. The combo we use to gate KWin
    window-rule installation (no equivalent on X11/non-KDE/non-Linux)."""
    return is_kde_desktop() and will_be_wayland()


def is_linux_wayland() -> bool:
    """Any Linux Wayland session. jellytoast draws its own frameless chrome
    here — KDE strips the decoration with a KWin noborder rule, GNOME / wlroots
    via the Qt FramelessWindowHint (CSD) — so the "native window border" opt-out
    is meaningful. X11 / macOS / Windows are handled by their own gates."""
    return IS_LINUX and will_be_wayland()
