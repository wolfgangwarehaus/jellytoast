"""macOS blur backend — NSVisualEffectView vibrancy (the frosted backdrop).

Replaces the deferred stub with the real vibrancy path. Behind a translucent
Qt window we install an ``NSVisualEffectView`` (blending mode "behind window")
as the window's *content view* and re-parent Qt's own view on top of it. Qt
already paints Frosted-mode chrome with ``WA_TranslucentBackground``, so the
system vibrancy shows through the transparent body — the macOS-native
equivalent of KWin's blur-behind on Linux. ``probe()`` therefore reports
``ACTIVE`` (a real, verified backdrop) so frosted surfaces ride it at full
glass alpha instead of the near-opaque fallback.

Reversible: ``apply(widget, False)`` restores Qt's view as the content view and
drops the effect view. ``corner_radius`` rounds the effect layer to match a
frameless rounded window (the mini player) so the frost doesn't square off the
corners. ``elevated`` popups get the lighter ``.popover`` material.

NOTE: this re-parents the NSWindow content view under Qt. The structural path
(insert / refresh / remove, no crash) is exercised headlessly, but the VISUAL
result and window-resize behaviour must be judged on a real display — a remote
framebuffer (VNC) misrepresents vibrancy. See docs/research/portable_blur.md §6.
"""

from __future__ import annotations

import logging

from jellytoast.blur import BlurStatus

logger = logging.getLogger(__name__)

# id(widget) -> (window, effect_view, qt_view). Lets apply(enabled=False)
# reverse the content-view swap, and is cleared on the widget's destroyed
# signal so we never touch a freed NSWindow.
_active: dict = {}


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
        )
    except Exception as e:  # pragma: no cover — macOS-only
        logger.debug("AppKit vibrancy import failed: %s", e)
        return False

    key = id(widget)
    try:
        view = _ns_view(widget)
        if view is None:
            return False
        window = view.window()
        if window is None:
            return False

        # Honor the macOS "Reduce transparency" accessibility setting (HIG):
        # treat it as a request to drop the vibrancy for the solid fallback.
        if not enabled or _reduce_transparency():
            return _remove(key, window)

        state = _active.get(key)
        if state is None:
            qt_view = window.contentView()
            # Capture the window's pre-vibrancy opacity/background so removal
            # restores EXACTLY that — a frosted Qt window is already
            # non-opaque/clear, so we must not force it back to opaque.
            orig_opaque = bool(window.isOpaque())
            orig_bg = window.backgroundColor()
            effect = NSVisualEffectView.alloc().initWithFrame_(qt_view.frame())
            effect.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            effect.setState_(NSVisualEffectStateActive)
            # Swap: effect becomes the content view, Qt's translucent view
            # rides on top so the vibrancy shows through the body.
            window.setContentView_(effect)
            effect.addSubview_(qt_view)
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            _active[key] = (window, effect, qt_view, orig_opaque, orig_bg)
            # Forget this widget when it dies so we never touch a freed window.
            try:
                widget.destroyed.connect(lambda *_: _active.pop(key, None))
            except Exception:
                pass
        else:
            _window, effect, _qt, _o, _b = state

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
        _win, effect, qt_view, orig_opaque, orig_bg = state
        # Restore Qt's view as the content view; the effect view drops out.
        window.setContentView_(qt_view)
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
    """Report UNSUPPORTED — native vibrancy is DISABLED on macOS.

    The NSVisualEffectView content-view swap that backs it does not reliably
    composite Qt's content onto the SCREEN after a window resize / activation
    state change — Qt's internal render is correct but the OS surface shows a
    blank or mis-drawn window (main window, mini player, dialogs). So jellytoast
    paints its own faux-frost / near-opaque body everywhere on macOS instead
    (status UNSUPPORTED → the theme's fallback body). The vibrancy code below is
    kept for reference / a future robust revival, but apply() is gated off in
    jellytoast/blur/__init__.py on macOS."""
    return BlurStatus.UNSUPPORTED


def reason(status):
    if status is BlurStatus.ACTIVE:
        return "macOS vibrancy (NSVisualEffectView) active"
    if _reduce_transparency():
        return "Reduce Transparency is on — using a near-opaque body"
    return "macOS vibrancy unavailable — using a near-opaque body"
