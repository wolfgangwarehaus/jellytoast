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
        # The frosted body is painted by Qt (faux-frost), so make the NSWindow
        # itself clear + non-opaque — otherwise its alpha reveals the opaque
        # system windowBackgroundColor and the whole window reads SOLID. Native
        # vibrancy (which used to supply the clear backdrop) is off on macOS
        # (see blur/_macos.py); without this the main window looks fully opaque
        # while the frameless mini player — already clear — shows the desktop
        # through at the same body alpha. This is the fix for "main + mini frost
        # don't match".
        try:
            from AppKit import NSColor

            nswin.setOpaque_(False)
            nswin.setBackgroundColor_(NSColor.clearColor())
        except Exception:
            pass
        _install_position_sync(window, nswin)
        logger.info("macOS native chrome: transparent titlebar + full-size content")
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS native chrome failed: %s", e)
        return False


def _install_position_sync(window, nswin) -> None:
    """Keep Qt's window position synced with the real NSWindow — **debounced**.

    ``setMovableByWindowBackground_`` lets the user drag the window by its
    frosted body, but AppKit moves the window WITHOUT Qt's QWindow learning
    about it — so Qt's geometry goes stale, and everything positioned via
    ``mapToGlobal`` / the window geometry (dropdown menus, centered dialogs)
    lands hundreds of px off (menu pops to the side, dialogs open on the
    desktop).

    A drag fires ``NSWindowDidMove`` ~60×/s. Syncing on every one — calling
    ``window.move()`` back into an ACTIVE AppKit drag — fights the drag and
    floods the main thread, freezing the UI. So we DEBOUNCE: each move just
    (re)arms a short timer, and we sync once the window has been still for a
    beat (drag released). Best-effort; never raises."""
    try:
        from AppKit import NSScreen, NSWindowDidMoveNotification
        from Foundation import NSNotificationCenter
        from PySide6.QtCore import QTimer

        def _do_sync():
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
                    window.move(tl_x, tl_y)
            except Exception:
                pass

        timer = QTimer(window)
        timer.setSingleShot(True)
        timer.timeout.connect(_do_sync)

        def _on_move(_note):
            try:
                timer.start(140)  # restart on each move; fires after the drag
            except Exception:
                pass

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSWindowDidMoveNotification, nswin, None, _on_move
        )
        # Keep refs so the observer token + closure + timer survive GC.
        window._jt_macos_move_observer = token
        window._jt_macos_move_cb = _on_move
        window._jt_macos_move_timer = timer
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS position-sync install failed: %s", e)
