"""KDE titlebar double-click integration.

KWin lets users pick what double-clicking a titlebar does — Maximize,
Maximize (vertical only), Minimize, Shade, etc. — via the
``TitlebarDoubleClickCommand`` key in ``~/.config/kwinrc``. Because
jellytoast's main window is borderless (KWin's titlebar is stripped via
a ``noborder`` rule, the top bar is the titlebar), the compositor
doesn't see double-clicks and so the user's chosen action never fires.

This module bridges that gap: read the kwinrc setting, then trigger the
matching KWin global shortcut via ``org.kde.kglobalaccel``. The
shortcut targets the active window — clicking the top bar focuses the
main window first, so we're addressing the right window.

For the one case where the user has no KWin shortcut bound and we can
still do something useful locally — vertical max — there's a Qt-level
fallback. Everything else (Shade, OnAllDesktops, …) can only be
performed by the compositor, so without D-Bus we silently no-op.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QRect, QTimer
from PySide6.QtWidgets import QWidget

from jellytoast.platform_compat import is_kde_desktop

# kwinrc -> kglobalaccel shortcut name. None means "no remote action";
# the caller may apply a local fallback (e.g. vertical-max via Qt).
_DISPATCH: dict[str, str | None] = {
    "Maximize": "Window Maximize",
    "Maximize (vertical only)": "Window Maximize Vertical",
    "Maximize (horizontal only)": "Window Maximize Horizontal",
    "Minimize": "Window Minimize",
    "Shade": "Window Shade",
    "Lower": "Window Lower",
    "Close": "Window Close",
    "OnAllDesktops": "Window On All Desktops",
    "Nothing": None,
}


def _kreadconfig_bin() -> str | None:
    for cand in ("kreadconfig6", "kreadconfig5"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _qdbus_bin() -> str | None:
    for cand in ("qdbus6", "qdbus-qt6", "qdbus"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _read_kwinrc_double_click_command() -> str:
    """Return the user's configured TitlebarDoubleClickCommand string.
    Defaults to ``Maximize`` (KWin's own default) when the key is unset
    or kreadconfig isn't available. Falls back to parsing kwinrc directly
    if kreadconfig is missing — keeps things working on minimal installs."""
    bin_ = _kreadconfig_bin()
    if bin_:
        try:
            out = subprocess.run(
                [
                    bin_,
                    "--file",
                    "kwinrc",
                    "--group",
                    "Windows",
                    "--key",
                    "TitlebarDoubleClickCommand",
                    "--default",
                    "Maximize",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            value = (out.stdout or "").strip()
            if value:
                return value
        except Exception:
            pass

    # Direct INI read fallback. kwinrc is a plain config file; we look
    # for `TitlebarDoubleClickCommand=…` under the `[Windows]` section.
    try:
        path = Path.home() / ".config" / "kwinrc"
        if path.is_file():
            in_section = False
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_section = stripped == "[Windows]"
                    continue
                if in_section and stripped.startswith("TitlebarDoubleClickCommand="):
                    return stripped.split("=", 1)[1].strip()
    except Exception:
        pass

    return "Maximize"


def _invoke_kwin_shortcut(name: str) -> bool:
    """Trigger a KWin global shortcut by name. Returns True on success.
    The action lands on whichever window is currently active."""
    bin_ = _qdbus_bin()
    if not bin_:
        return False
    try:
        result = subprocess.run(
            [
                bin_,
                "org.kde.kglobalaccel",
                "/component/kwin",
                "org.kde.kglobalaccel.Component.invokeShortcut",
                name,
            ],
            check=False,
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


def _vertical_max_toggle(window: QWidget) -> None:
    """Local fallback: toggle vertical-max via setGeometry. Reliable on
    X11; on Wayland the y-component of setGeometry is a no-op (only the
    compositor positions windows) so it expands height-only downward."""
    if window.isMaximized() or window.isFullScreen():
        window.showNormal()
        return
    screen = window.screen() if hasattr(window, "screen") else None
    if screen is None:
        return
    avail = screen.availableGeometry()
    cur = window.geometry()
    is_vmaxed = cur.y() == avail.y() and cur.height() == avail.height()
    if is_vmaxed:
        prev = getattr(window, "_vmax_prev_geo", None)
        if isinstance(prev, QRect):
            window.setGeometry(prev)
        window._vmax_prev_geo = None
    else:
        window._vmax_prev_geo = QRect(cur)
        window.setGeometry(cur.x(), avail.y(), cur.width(), avail.height())


def handle_titlebar_double_click(window: QWidget) -> None:
    """Mirror KWin's TitlebarDoubleClickCommand for our borderless top
    bar. On KDE we read kwinrc and invoke the matching KWin shortcut
    (the compositor is the only thing that can move/resize Wayland
    windows freely). Off KDE — or if D-Bus is unreachable — we fall
    back to a local vertical-max toggle, which is the action this app
    historically used."""
    command = _read_kwinrc_double_click_command() if is_kde_desktop() else None
    shortcut = _DISPATCH.get(command, None) if command else None

    if shortcut and _invoke_kwin_shortcut(shortcut):
        return

    # Local fallbacks. We only have a sensible Qt-level implementation
    # for the maximize family; the others (Shade, OnAllDesktops, …) are
    # compositor-only concepts.
    if command == "Nothing":
        return
    if command in ("Maximize",):
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        return
    if command == "Minimize":
        window.showMinimized()
        return
    if command == "Close":
        QTimer.singleShot(0, window.close)
        return
    # Compositor-only concepts — we tried via D-Bus and failed (qdbus
    # missing / shortcut not bound). No sensible Qt-level fallback, so
    # honour the user's choice by doing nothing rather than maximizing.
    if command in ("Shade", "Lower", "OnAllDesktops"):
        return
    # Default (and "Maximize (vertical only)" fallback): vertical-max.
    _vertical_max_toggle(window)
