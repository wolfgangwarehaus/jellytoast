"""Cross-platform desktop notifications. Public API is platform-agnostic;
the actual implementation lives in a per-OS backend module.

Public API:
    is_supported() -> bool   # backend can actually display notifications
    notify(title, body="", icon=None, app_name="jellytoast") -> None

Linux: shells out to `notify-send` (libnotify), which routes through the
freedesktop.org `org.freedesktop.Notifications` D-Bus service. Picked up
by KDE Plasma, GNOME Shell, dunst, mako, swaync, etc.

Windows / macOS: not yet implemented — the unsupported backend's
`notify()` is a silent no-op and `is_supported()` returns False so call
sites can gate UX cleanly.

Failures from the active backend (D-Bus down, no notification daemon
running, notify-send missing mid-process) never raise to the caller —
notifications are best-effort by design.
"""

from __future__ import annotations

import sys
from types import ModuleType


_backend: ModuleType | None = None


def _select_backend() -> ModuleType:
    """Resolve the backend module once per process and memoize. Mirrors
    the dispatch shape used by `modules.autostart`."""
    global _backend
    if _backend is not None:
        return _backend

    if sys.platform.startswith("linux"):
        from modules.notifications import _linux as backend
    else:
        from modules.notifications import _unsupported as backend

    _backend = backend
    return _backend


def is_supported() -> bool:
    return _select_backend().is_supported()


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
) -> None:
    """Show a desktop notification. Silent no-op on unsupported
    platforms; never raises."""
    try:
        _select_backend().notify(title, body, icon, app_name)
    except Exception:
        pass
