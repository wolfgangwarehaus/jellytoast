"""macOS native window chrome — transparent titlebar + full-size content view.

The native macOS pattern for a custom-chrome app (Music.app, Safari, Spotify):
keep the real NSWindow — traffic lights, native resize/zoom/fullscreen/tiling
all keep working (never go frameless on Mac) — but make the titlebar
TRANSPARENT and let the content fill the whole window
(NSWindowStyleMaskFullSizeContentView). The window's frosted backdrop then
flows up to the native rounded top corners with the traffic-light cluster
floating over it; no separate dark titlebar strip, no app-drawn top corners.

The app reserves a thin top inset in its chrome layout (see app.py, gated on
IS_MACOS) so the top bar clears the traffic lights while the vibrancy shows
through behind them.

pyobjc; macOS-only; called once after the window exists.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Reserved height (pt) of the transparent-titlebar strip the app's chrome
# layout leaves clear so the top bar doesn't collide with the traffic lights.
TITLEBAR_INSET = 28

# AppKit constants (stable ABI)
_NSWindowStyleMaskFullSizeContentView = 1 << 15
_NSWindowTitleHidden = 1
_NSTitlebarSeparatorStyleNone = 3


def apply(window) -> bool:
    """Transparent titlebar + full-size content view on ``window``'s NSWindow,
    so the frosted chrome flows under it to the native top corners. Best-effort;
    never raises."""
    try:
        import objc

        wid = int(window.winId())
        if not wid:
            return False
        nswin = objc.objc_object(c_void_p=wid).window()
        if nswin is None:
            return False
        nswin.setStyleMask_(
            nswin.styleMask() | _NSWindowStyleMaskFullSizeContentView
        )
        nswin.setTitlebarAppearsTransparent_(True)
        nswin.setTitleVisibility_(_NSWindowTitleHidden)
        try:
            nswin.setTitlebarSeparatorStyle_(_NSTitlebarSeparatorStyleNone)  # macOS 11+
        except Exception:
            pass
        # The transparent titlebar no longer offers a grab strip of its own,
        # so let the user drag the window by the (frosted) chrome background.
        nswin.setMovableByWindowBackground_(True)
        logger.info("macOS native chrome: transparent titlebar + full-size content")
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS native chrome failed: %s", e)
        return False
