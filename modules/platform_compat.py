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


def is_kde_wayland() -> bool:
    """KDE Plasma running on Wayland. The combo we use to gate KWin
    window-rule installation (no equivalent on X11/non-KDE/non-Linux)."""
    return is_kde_desktop() and will_be_wayland()
