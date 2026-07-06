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

def _ns_view(widget):
    """The widget's backing NSView (Qt's winId IS the NSView on macOS) —
    mirrors jellytoast.blur._macos._ns_view. None when unrealized."""
    try:
        import objc

        wid = int(widget.winId())
        return objc.objc_object(c_void_p=wid) if wid else None
    except Exception:  # pragma: no cover — macOS-only
        return None


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
        _install_fullscreen_restore(window, nswin)
        _install_resize_tint_sync(window, nswin)
        _update_titlebar_tint(window, nswin)
        logger.info("macOS native chrome: transparent titlebar + full-size content")
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS native chrome failed: %s", e)
        return False


def refresh_titlebar_tint(window) -> None:
    """Re-sync the titlebar tint band to the CURRENT frosted body colour.
    Call on ``theme_changed`` (covers theme/mode/accent swaps and the
    Glass-opacity slider's settle commit). Best-effort; never raises."""
    try:
        qt_view = _ns_view(window)
        if qt_view is None:
            return
        nswin = qt_view.window()
        if nswin is not None:
            _update_titlebar_tint(window, nswin)
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("titlebar tint refresh failed: %s", e)


def _update_titlebar_tint(window, nswin) -> None:
    """Paint the titlebar band with the SAME frosted body colour Qt paints
    the window with, so the top of the window blends seamlessly.

    With ``FullSizeContentView`` the vibrancy backdrop reaches the native top
    corners, but Qt's content view still stops BELOW the titlebar — that
    ~28 pt band showed bare (untinted) vibrancy, visibly lighter than the
    glass body under it, and it could never follow the Glass-opacity slider
    (found on the 0.1.8 MAS screenshot pass). A layer-backed NSView pinned
    over the band — above Qt's view, below the titlebar controls — carries
    ``theme.body_color_for(...)`` verbatim, so the strip composites exactly
    like the body and tracks the slider/theme via refresh_titlebar_tint().
    Hidden in fullscreen (no titlebar). Best-effort; never raises."""
    try:
        import AppKit
        from AppKit import NSColor, NSViewMinYMargin, NSViewWidthSizable, NSWindowAbove

        qt_view = _ns_view(window)
        if qt_view is None:
            return
        host = qt_view.superview()
        if host is None:
            return
        frame = nswin.frame()
        # Whether Qt's view reaches the top of the frame is BISTABLE across
        # launches (cocoa geometry negotiation vs FullSizeContentView timing):
        # some runs QNSView spans the full frame — Qt's own glass already
        # covers the titlebar band and an extra tint would double-darken it —
        # other runs it stops at the content-layout height, leaving the band
        # bare. So tint exactly the UNCOVERED gap, and nothing when there is
        # none. (Fullscreen also lands in the no-gap branch: no titlebar.)
        qt_h = float(qt_view.frame().size.height)
        tb_h = float(frame.size.height) - qt_h
        tint = getattr(window, "_jt_titlebar_tint", None)
        if tb_h <= 0.5:  # Qt covers the full frame (or fullscreen) — no band
            if tint is not None:
                tint.setHidden_(True)
            return
        rect = AppKit.NSMakeRect(0, qt_h, frame.size.width, tb_h)
        if tint is None:
            tint = AppKit.NSView.alloc().initWithFrame_(rect)
            # Width follows the window; MinY margin keeps it pinned to the top.
            tint.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
            tint.setWantsLayer_(True)
            # Above Qt's view (which never covers this band), below the
            # NSTitlebarContainerView holding the traffic lights — so the
            # buttons stay visible and clickable.
            host.addSubview_positioned_relativeTo_(tint, NSWindowAbove, qt_view)
            window._jt_titlebar_tint = tint
        else:
            tint.setHidden_(False)
            tint.setFrame_(rect)
        from jellytoast import blur as _blur
        from jellytoast import theme as _theme

        r, g, b, a = _theme.body_color_for(
            _theme.get_active_theme(), _blur.status(), "main"
        )
        layer = tint.layer()
        if layer is not None:
            layer.setBackgroundColor_(
                NSColor.colorWithSRGBRed_green_blue_alpha_(
                    r / 255.0, g / 255.0, b / 255.0, a / 255.0
                ).CGColor()
            )
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("titlebar tint update failed: %s", e)


def _install_resize_tint_sync(window, nswin) -> None:
    """Keep the titlebar tint band sized/positioned through window resizes.

    The band is derived from the LIVE gap between the frame and Qt's view
    (see _update_titlebar_tint) — a resize renegotiates both, and whether
    Qt covers the band can flip mid-session, so recompute on every
    ``NSWindowDidResizeNotification`` (cheap: frame math + one layer colour).
    Install-once guarded like the sibling observers. Never raises."""
    if getattr(window, "_jt_macos_resize_observer", None) is not None:
        return
    try:
        from AppKit import NSWindowDidResizeNotification
        from Foundation import NSNotificationCenter

        def _on_resize(_note):
            try:
                _update_titlebar_tint(window, nswin)
            except Exception:
                pass

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSWindowDidResizeNotification, nswin, None, _on_resize
        )
        window._jt_macos_resize_observer = token
        window._jt_macos_resize_cb = _on_resize
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS resize-tint-sync install failed: %s", e)


def _install_fullscreen_restore(window, nswin) -> None:
    """Re-assert the transparent-titlebar chrome after every native
    fullscreen EXIT.

    AppKit restores the window's pre-fullscreen styleMask when leaving
    fullscreen, wiping ``FullSizeContentView`` + ``titlebarAppearsTransparent``
    (observed styleMask 15 after one round-trip on the 0.1.8 MAS screenshot
    pass) — the titlebar regrows its own opaque material band and the top of
    the window stops blending into the glass. A Qt-side re-apply on
    ``WindowStateChange`` fires DURING the exit transition and gets clobbered
    by AppKit's own restore, so hook AppKit's authoritative
    ``NSWindowDidExitFullScreenNotification`` instead — by then the transition
    is done and the re-asserted mask sticks. Idempotent; best-effort; never
    raises."""
    if getattr(window, "_jt_macos_fs_observer", None) is not None:
        return
    try:
        from AppKit import NSWindowDidExitFullScreenNotification
        from Foundation import NSNotificationCenter

        def _on_exit_fullscreen(_note):
            try:
                apply(window)
            except Exception as e:  # pragma: no cover — macOS-only
                logger.info("post-fullscreen chrome restore failed: %s", e)

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSWindowDidExitFullScreenNotification, nswin, None, _on_exit_fullscreen
        )
        # Retain the token + closure for the window's lifetime (mirrors
        # _install_position_sync).
        window._jt_macos_fs_observer = token
        window._jt_macos_fs_cb = _on_exit_fullscreen
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS fullscreen-restore install failed: %s", e)


def _install_position_sync(window, nswin) -> None:
    # apply() re-runs on every fullscreen exit (_install_fullscreen_restore) —
    # one observer per window is enough; stacking a fresh one each pass would
    # leak observers + timers for the window's whole lifetime.
    if getattr(window, "_jt_macos_move_observer", None) is not None:
        return
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
