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

from types import ModuleType

_backend: ModuleType | None = None


def _select_backend() -> ModuleType:
    """Resolve the backend module once per process and memoize. Mirrors
    the dispatch shape used by `jellytoast.autostart`."""
    global _backend
    if _backend is not None:
        return _backend

    from jellytoast.platform_compat import IS_LINUX, IS_WINDOWS

    if IS_LINUX:
        from jellytoast.notifications import _linux as backend
    elif IS_WINDOWS:
        try:
            from jellytoast.notifications import _windows as backend
        except Exception:
            from jellytoast.notifications import _unsupported as backend
    else:
        from jellytoast.notifications import _unsupported as backend

    _backend = backend
    return _backend


def is_supported() -> bool:
    return _select_backend().is_supported()


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
    tag: str | None = None,
) -> None:
    """Show a desktop notification. Silent no-op on unsupported
    platforms; never raises. ``tag`` groups successive notifications so a
    new one *replaces* the prior (used by the now-playing stream so
    track-change toasts don't pile up)."""
    try:
        _select_backend().notify(title, body, icon, app_name, tag)
    except Exception:
        pass
