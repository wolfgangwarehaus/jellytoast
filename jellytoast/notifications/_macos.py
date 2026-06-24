"""macOS desktop notifications — real Notification Center banners.

Posts ``UNUserNotificationCenter`` banners attributed to **jellytoast** (the
proper, reliable API). That framework needs a *bundle identifier*, so it only
works inside the signed ``.app`` (whose ``CFBundleIdentifier`` is jellytoast's
own); a from-source ``python -m jellytoast`` run is hosted by the *interpreter's*
bundle instead (e.g. Homebrew Python's ``org.python.python``), so we gate on
jellytoast's exact id and otherwise fall back to an ``osascript`` banner (which
the OS attributes to "Script Editor" and may suppress — hence the upgrade to the
native API for the shipped app).

The title/body in the osascript fallback are passed as AppleScript *run
arguments* — never interpolated into the script source — so arbitrary
track/album text can neither break nor inject into the script. ``icon`` and
``tag`` aren't expressible (Notification Center groups by app itself); ``tag``
is reused as the request identifier so a new track replaces the previous
banner rather than stacking. Best-effort by the module contract — never raises.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Stable "not yet resolved" sentinel. MUST be a module-level constant — an
# inline ``object()`` in the guard mints a fresh object each call, so the
# identity check never matches and _get_center() would return unresolved.
_UNRESOLVED = object()
_center = _UNRESOLVED
_auth_requested = False


# CFBundleIdentifier of the shipped .app — see packaging/pyinstaller/jellytoast.spec.
_BUNDLE_ID = "io.github.wolfgangwarehaus.jellytoast"


def _is_bundled() -> bool:
    """True only inside jellytoast's OWN signed ``.app``.

    A from-source ``python -m jellytoast`` run is hosted by the interpreter's
    bundle — Homebrew/python.org Python ships a ``Python.app`` whose identifier
    is ``org.python.python`` — so a bare ``is not None`` check false-positives
    and routes UN banners through *that* bundle (attributed to "Python", and
    needing a separate Python notification authorization). Gate on jellytoast's
    exact id so only the shipped app takes the UN path; everything else falls
    through to the osascript banner."""
    try:
        from Foundation import NSBundle

        return NSBundle.mainBundle().bundleIdentifier() == _BUNDLE_ID
    except Exception:
        return False


def _get_center():
    """The shared UNUserNotificationCenter, or None when unavailable
    (not bundled / framework missing). Resolved once and cached."""
    global _center
    if _center is not _UNRESOLVED:
        return _center
    _center = None
    if not _is_bundled():
        return None
    try:
        from UserNotifications import UNUserNotificationCenter

        _center = UNUserNotificationCenter.currentNotificationCenter()
    except Exception as e:  # framework not bundled / unavailable
        logger.debug("UNUserNotificationCenter unavailable: %s", e)
        _center = None
    return _center


def _ensure_auth(center) -> None:
    """Request alert+sound authorization once (the OS prompts on first call,
    then remembers). Async; an early banner may be dropped before the user
    answers, but every later one lands."""
    global _auth_requested
    if _auth_requested:
        return
    _auth_requested = True
    try:
        from UserNotifications import (
            UNAuthorizationOptionAlert,
            UNAuthorizationOptionSound,
        )

        center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
            lambda _granted, _err: None,
        )
    except Exception as e:
        logger.debug("notification auth request failed: %s", e)


def is_supported() -> bool:
    return _get_center() is not None or shutil.which("osascript") is not None


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
    tag: str | None = None,
) -> None:
    center = _get_center()
    if center is not None:
        try:
            from UserNotifications import (
                UNMutableNotificationContent,
                UNNotificationRequest,
            )

            _ensure_auth(center)
            content = UNMutableNotificationContent.alloc().init()
            content.setTitle_(str(title or app_name))
            if body:
                content.setBody_(str(body))
            req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                str(tag or "jellytoast-notify"), content, None
            )
            center.addNotificationRequest_withCompletionHandler_(req, None)
            return
        except Exception as e:  # fall through to osascript
            logger.debug("UN notify failed (%s) — falling back to osascript", e)

    # Fallback (from-source run, or UN unavailable): an osascript banner.
    osa = shutil.which("osascript")
    if not osa:
        return
    try:
        subprocess.run(
            [
                osa,
                "-e", "on run {t, b}",
                "-e", "display notification b with title t",
                "-e", "end run",
                str(title or app_name),
                str(body or ""),
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass
