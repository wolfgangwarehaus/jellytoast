"""macOS blur backend — NSVisualEffectView vibrancy (the frosted backdrop).

Behind a translucent Qt window we install an ``NSVisualEffectView`` (blending
mode "behind window") as a SIBLING of Qt's content view, ordered strictly
*below* it. Qt already paints Frosted-mode chrome with
``WA_TranslucentBackground``, so the system vibrancy shows through the
transparent body — the macOS-native equivalent of KWin's blur-behind on Linux.
``probe()`` therefore reports ``ACTIVE`` (a real, verified backdrop) so frosted
surfaces ride it at full glass alpha instead of the near-opaque fallback.

Why sibling-below and not a content-view swap: the earlier implementation made
the effect view the window's *content view* and re-parented Qt's view on top of
it. That demotes ``QNSView`` from the content-view slot, and macOS then stops
auto-sizing it (blank margins on resize) while ``QCocoaWindow`` keeps re-asserting
``QNSView`` as the content view on recreate/state changes (QTBUG-69302) — ripping
the effect view back out and blanking the window on activation. Keeping QNSView as
the content view and inserting the effect view into its superview (the private
theme frame) via ``addSubview:positioned:NSWindowBelow relativeTo:`` sidesteps
both: Qt never loses content-view ownership, so resize, hit-testing, cursors and
recreate all keep working. This is Electron's vibrancy pattern, hoisted one level
because in Qt the QNSView itself is the content view.

Reversible: ``apply(widget, False)`` drops the effect view and restores the
window's original opacity/background. ``corner_radius`` rounds the effect layer to
match a frameless rounded window (the mini player) so the frost doesn't square off
the corners. ``elevated`` popups get the lighter ``.popover`` material.

NOTE: the structural path (insert / refresh / remove, no crash) is exercised
headlessly, but the VISUAL result and window-resize/activation behaviour must be
judged on a real display — a remote framebuffer (VNC) misrepresents vibrancy.
"""

from __future__ import annotations

import logging

from jellytoast.blur import BlurStatus

logger = logging.getLogger(__name__)

# id(widget) -> (window, effect_view, qt_view, orig_opaque, orig_bg). Lets
# apply(enabled=False) drop the effect view and restore the window, and is
# cleared on the widget's destroyed signal so we never touch a freed NSWindow.
_active: dict = {}

# Retains the NSWorkspace notification token for the live accessibility-display
# observer (see install_accessibility_observer). Module-global so the
# observation outlives the call and stays alive for the app's lifetime.
_ax_observer = None


def _ns_view(widget):
    """The widget's backing NSView (Qt's winId IS the NSView on macOS)."""
    import objc

    wid = int(widget.winId())
    return objc.objc_object(c_void_p=wid) if wid else None


def is_supported() -> bool:
    """NSVisualEffectView ships on every macOS we target (10.10+)."""
    try:
        from AppKit import NSVisualEffectView  # noqa: F401

        return True
    except Exception:
        return False


def _reduce_transparency() -> bool:
    """True when the user's macOS *Reduce transparency* accessibility setting
    is on (System Settings → Accessibility → Display). HIG requires honoring
    it — drop the vibrancy for a solid fill. Best-effort; defaults to False."""
    try:
        from AppKit import NSWorkspace

        return bool(
            NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency()
        )
    except Exception:
        return False


def _set_layer_clear(qt_view):
    """Force Qt's backing layer transparent so the behind-window vibrancy shows
    through instead of an opaque fill painting over it. QNSView is layer-backed
    under Qt6 RHI, so the layer is present; best-effort either way."""
    try:
        from AppKit import NSColor

        layer = qt_view.layer()
        if layer is not None:
            layer.setOpaque_(False)
            layer.setBackgroundColor_(NSColor.clearColor().CGColor())
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("vibrancy clear-layer failed: %s", e)


def apply(widget, enabled, corner_radius=0, dark=True, elevated=False) -> bool:
    """Install (``enabled=True``) or remove vibrancy behind ``widget``'s
    window. The QWindow must already exist (call after ``show()``). Returns
    True if the request was applied. Never raises."""
    try:
        from AppKit import (
            NSColor,
            NSViewHeightSizable,
            NSViewWidthSizable,
            NSVisualEffectBlendingModeBehindWindow,
            NSVisualEffectMaterialPopover,
            NSVisualEffectMaterialUnderWindowBackground,
            NSVisualEffectStateActive,
            NSVisualEffectView,
            NSWindowBelow,
        )
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("AppKit vibrancy import failed: %s", e)
        return False

    key = id(widget)
    try:
        qt_view = _ns_view(widget)
        if qt_view is None:
            return False
        window = qt_view.window()
        if window is None:
            return False

        # Honor the macOS "Reduce transparency" accessibility setting (HIG):
        # treat it as a request to drop the vibrancy for the solid fallback.
        if not enabled or _reduce_transparency():
            return _remove(key, window)

        # The view that hosts the content view (the private theme frame). We
        # insert the effect view here, ordered BELOW Qt's view, so Qt keeps
        # ownership of the content-view slot. superview() is None until the
        # window is realized.
        host = qt_view.superview()
        if host is None:
            return False

        state = _active.get(key)
        if state is None:
            # Capture the window's pre-vibrancy opacity/background so removal
            # restores EXACTLY that — a frosted Qt window is already
            # non-opaque/clear, so we must not force it back to opaque.
            orig_opaque = bool(window.isOpaque())
            orig_bg = window.backgroundColor()
            effect = NSVisualEffectView.alloc().initWithFrame_(host.bounds())
            effect.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            # Constant blur — the default (FollowsWindowActiveState) washes the
            # material out whenever the window isn't key (an activation symptom).
            effect.setState_(NSVisualEffectStateActive)
            # Insert STRICTLY below Qt's content view (never a bare addSubview,
            # which orders it on top and hides Qt's content).
            host.addSubview_positioned_relativeTo_(effect, NSWindowBelow, qt_view)
            # The vibrancy only shows through if the window + Qt's layer are
            # genuinely clear; Qt paints Frosted chrome translucent, so force
            # the window + backing layer transparent.
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            _set_layer_clear(qt_view)
            _active[key] = (window, effect, qt_view, orig_opaque, orig_bg)
            # Forget this widget when it dies so we never touch a freed window.
            try:
                widget.destroyed.connect(lambda *_: _active.pop(key, None))
            except Exception:
                pass
        else:
            _window, effect, _qt, _o, _b = state
            # Re-assert the below-Qt order on every re-apply (mini-player resize,
            # theme switch) — cheap insurance against AppKit reordering the
            # foreign subview on a state change.
            host.addSubview_positioned_relativeTo_(effect, NSWindowBelow, qt_view)

        effect.setMaterial_(
            NSVisualEffectMaterialPopover
            if elevated
            else NSVisualEffectMaterialUnderWindowBackground
        )
        _set_appearance(effect, dark)
        if corner_radius > 0:
            effect.setWantsLayer_(True)
            layer = effect.layer()
            if layer is not None:
                layer.setCornerRadius_(float(corner_radius))
                layer.setMasksToBounds_(True)
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("vibrancy apply failed: %s", e)
        return False


def _set_appearance(effect, dark: bool):
    """Pin the effect view to the vibrant dark/light appearance matching the
    active theme. Best-effort — older naming or a missing symbol just leaves
    the system default."""
    try:
        from AppKit import NSAppearance

        name = (
            "NSAppearanceNameVibrantDark" if dark else "NSAppearanceNameVibrantLight"
        )
        import AppKit

        appr = NSAppearance.appearanceNamed_(getattr(AppKit, name, name))
        if appr is not None:
            effect.setAppearance_(appr)
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("vibrancy appearance failed: %s", e)


def _remove(key, window) -> bool:
    state = _active.pop(key, None)
    if state is None:
        return False
    try:
        _win, effect, _qt_view, orig_opaque, orig_bg = state
        # The effect view is a sibling — just drop it out; Qt's content view was
        # never touched, so there is nothing to restore there.
        effect.removeFromSuperview()
        # Restore the window's pre-vibrancy opacity/background exactly.
        window.setOpaque_(orig_opaque)
        if orig_bg is not None:
            window.setBackgroundColor_(orig_bg)
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("vibrancy remove failed: %s", e)
        return False


def probe():
    """Report ACTIVE — a real NSVisualEffectView backdrop sits behind the
    window (sibling-below the Qt content view), so frosted surfaces ride it at
    full glass alpha.

    UNSUPPORTED when AppKit is absent (non-macOS / headless CI) or the user has
    turned on the *Reduce Transparency* accessibility setting — both fall back
    to the theme's near-opaque body."""
    if not is_supported():
        return BlurStatus.UNSUPPORTED
    if _reduce_transparency():
        return BlurStatus.UNSUPPORTED
    return BlurStatus.ACTIVE


def reason(status):
    if status is BlurStatus.ACTIVE:
        return "macOS vibrancy (NSVisualEffectView) active"
    if _reduce_transparency():
        return "Reduce Transparency is on — using a near-opaque body"
    return "macOS vibrancy unavailable — using a near-opaque body"


def install_accessibility_observer(on_change) -> bool:
    """Install a one-time observer for live macOS accessibility-display changes
    (notably *Reduce transparency*), invoking ``on_change`` on the GUI thread
    whenever it flips.

    The verified blur ``status()`` is cached for the whole session, but
    ``apply()`` reads *Reduce transparency* LIVE and removes the vibrancy when
    it's on. So a runtime toggle otherwise strips the backdrop while the frosted
    body keeps painting at its glass alpha — a see-through window. ``on_change``
    should re-probe (``status(force=True)``) and re-stamp the app so the body
    falls back to its near-opaque alpha (and back). Idempotent; returns True
    once installed. Never raises."""
    global _ax_observer
    if _ax_observer is not None:
        return True
    try:
        import AppKit
        from AppKit import NSWorkspace
        from Foundation import NSOperationQueue

        # The constant exists in AppKit but isn't always bound as a Python
        # symbol — fall back to the documented notification name string.
        name = getattr(
            AppKit,
            "NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification",
            "NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification",
        )

        def _handler(_note):
            try:
                on_change()
            except Exception as e:  # pragma: no cover — macOS-only
                logger.debug("accessibility on_change failed: %s", e)

        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        # Deliver on the main queue → the GUI thread, so on_change can touch Qt
        # objects + emit signals safely. The returned token must be retained
        # (stored module-global) to keep the observation alive.
        _ax_observer = nc.addObserverForName_object_queue_usingBlock_(
            name, None, NSOperationQueue.mainQueue(), _handler
        )
        logger.debug("installed accessibility-display observer")
        return True
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("accessibility observer install failed: %s", e)
        return False
