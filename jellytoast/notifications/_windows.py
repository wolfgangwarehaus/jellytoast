"""Windows desktop-notification backend.

Posts Action-Center toasts via the maintained ``windows_toasts`` package
(a thin wrapper over the modern WinRT ``ToastNotificationManager``).
Ties to jellytoast's stamped AppUserModelID so toasts carry the right
name/icon, and uses a Tag/Group when a ``tag`` is supplied so a stream of
now-playing toasts *replaces* in place rather than stacking.

All imports are lazy + guarded: the module imports on any platform and
every call degrades to a silent no-op if the package or the WinRT runtime
is missing (``is_supported`` returns False, ``notify`` swallows errors).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Reuse the taskbar/start-menu identity so toasts show "jellytoast".
_AUMID = "wolfgangwarehaus.jellytoast"
_GROUP = "jellytoast"

_toaster = None
_toaster_failed = False


def _get_toaster():
    """Memoized WindowsToaster bound to our AUMID, or None if unavailable."""
    global _toaster, _toaster_failed
    if _toaster is not None or _toaster_failed:
        return _toaster
    try:
        from windows_toasts import WindowsToaster

        try:
            # Newer windows_toasts accepts a notifier AUMID so the toast
            # inherits our stamped name + icon.
            _toaster = WindowsToaster(_AUMID)
        except TypeError:  # pragma: no cover — older signature
            _toaster = WindowsToaster("jellytoast")
    except Exception as e:  # pragma: no cover — Windows-only
        logger.info("windows_toasts unavailable, toasts disabled: %s", e)
        _toaster_failed = True
        _toaster = None
    return _toaster


def is_supported() -> bool:
    return _get_toaster() is not None


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
    tag: str | None = None,
) -> None:
    toaster = _get_toaster()
    if toaster is None:
        return
    try:
        from windows_toasts import Toast

        toast = Toast()
        toast.text_fields = [title] + ([body] if body else [])
        if tag:
            # Same tag+group → the new toast supersedes the previous one
            # in Action Center instead of piling up (now-playing stream).
            toast.tag = tag
            toast.group = _GROUP
        if icon:
            try:
                from windows_toasts import ToastDisplayImage

                toast.AddImage(ToastDisplayImage.fromPath(icon))
            except Exception:  # pragma: no cover — image best-effort
                pass
        toaster.show_toast(toast)
    except Exception as e:  # pragma: no cover — Windows-only
        logger.debug("toast failed: %s", e)
