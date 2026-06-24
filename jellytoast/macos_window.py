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

# EXTRA top inset (pt) added below the native titlebar. Qt's QMainWindow
# already reserves the native titlebar height (~28–32pt — where the traffic
# lights live) for its central widget, so the top bar already sits just under
# the stoplights. We add NOTHING on top of that: 0 keeps the row tight against
# the titlebar (no "forehead"). The frosted vibrancy still flows up behind the
# transparent titlebar so the traffic lights float over glass.
TITLEBAR_INSET = 0

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
        _install_position_sync(window, nswin)
        logger.info("macOS native chrome: transparent titlebar + full-size content")
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS native chrome failed: %s", e)
        return False


def _install_position_sync(window, nswin) -> None:
    """Keep Qt's window position synced with the real NSWindow.

    ``setMovableByWindowBackground_`` lets the user drag the window by its
    frosted body, but AppKit moves the window WITHOUT Qt's QWindow learning
    about it — so Qt's geometry goes stale, and everything positioned via
    ``mapToGlobal`` / the window geometry (dropdown menus, centered dialogs)
    lands hundreds of px off (the menu pops to the side, dialogs open on the
    desktop). Observe ``NSWindowDidMove`` and push the real top-left back into
    Qt so those stay aligned. Best-effort; never raises."""
    try:
        from AppKit import NSScreen, NSWindowDidMoveNotification
        from Foundation import NSNotificationCenter

        def _resync(_note):
            try:
                screens = NSScreen.screens()
                if not screens:
                    return
                f = nswin.frame()
                # AppKit frames are bottom-left origin; Qt is top-left from the
                # primary screen. Convert via the primary screen's height.
                main_h = screens[0].frame().size.height
                tl_x = int(round(f.origin.x))
                tl_y = int(round(main_h - f.origin.y - f.size.height))
                if abs(window.x() - tl_x) > 1 or abs(window.y() - tl_y) > 1:
                    # Moves the NSWindow to where it already is (no-op natively)
                    # while updating Qt's stale position — no loop, no jump.
                    window.move(tl_x, tl_y)
            except Exception:
                pass

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSWindowDidMoveNotification, nswin, None, _resync
        )
        # Keep refs so the observer token + closure survive GC.
        window._jt_macos_move_observer = token
        window._jt_macos_move_cb = _resync
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS position-sync install failed: %s", e)
