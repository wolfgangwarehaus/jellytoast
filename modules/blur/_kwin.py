"""KWindowSystem blur backend — `KWindowEffects::enableBlurBehind()`
reached through ctypes against ``libKF6WindowSystem``.

Why ctypes: KF6 ships no Python binding, and `QtWaylandClient` (which
would let us marshal the blur protocol ourselves) isn't bundled with
PySide6. ``enableBlurBehind`` is a plain — if mangled — C++ symbol, so
ctypes is the clean route. KWindowSystem itself does the hard parts:
it speaks `ext-background-effect-v1` where the compositor advertises it
and falls back to the legacy `org_kde_kwin_blur`, translates an empty
QRegion to "blur the whole window", and re-applies blur via its own
event filter whenever the Wayland surface is recreated — so callers
just call once per show / theme change.

Everything is best-effort: a missing library or symbol, a window with
no platform surface yet, a compositor with no blur protocol — all
resolve to a silent no-op. Blur is pure progressive enhancement.
"""

from __future__ import annotations

import ctypes

# KWindowEffects::enableBlurBehind(QWindow *, bool, QRegion const &)
# Itanium-mangled. ABI-stable for KF6's lifetime; guarded anyway.
_SYMBOL = "_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion"
_SONAMES = ("libKF6WindowSystem.so.6", "libKF6WindowSystem.so")

_fn = None  # resolved ctypes callable, or None if unavailable
_resolved = False  # resolution attempted yet?


def _resolve():
    """Load libKF6WindowSystem and bind the enableBlurBehind symbol.
    Cached — the result is stable for the process lifetime."""
    global _fn, _resolved
    if _resolved:
        return _fn
    _resolved = True
    for soname in _SONAMES:
        try:
            lib = ctypes.CDLL(soname)
        except OSError:
            continue
        try:
            fn = lib[_SYMBOL]
        except (AttributeError, KeyError):
            continue
        # (QWindow*, bool, QRegion*) — pointers passed as void*.
        fn.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]
        fn.restype = None
        _fn = fn
        return _fn
    return None


def is_supported() -> bool:
    return _resolve() is not None


def _rounded_region(widget, radius: int):
    """A QRegion shaped to a rounded rect matching ``widget``'s current
    (logical) size. Rasterised through a monochrome QBitmap mask —
    QRegion has no rounded-rect constructor. KWindowSystem scales the
    region by the window's DPR, so logical coordinates are correct."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QBitmap, QPainter, QPainterPath, QRegion

    w, h = widget.width(), widget.height()
    if w <= 0 or h <= 0:
        return QRegion()  # not laid out yet — fall back to whole-window
    bmp = QBitmap(w, h)
    bmp.fill(Qt.GlobalColor.color0)  # color0 = outside the region
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p = QPainter(bmp)
    # No AA — a region is a hard 1-bit mask; antialiased edge pixels
    # would just become ragged region boundary.
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.fillPath(path, Qt.GlobalColor.color1)  # color1 = inside the region
    p.end()
    return QRegion(bmp)


def apply(widget, enabled: bool, corner_radius: int = 0) -> bool:
    """Issue enableBlurBehind for ``widget``'s QWindow. ``corner_radius``
    > 0 shapes the blur region to a rounded rect; 0 = whole window.
    Returns False (no-op) if the lib is missing or the widget has no
    platform window yet."""
    fn = _resolve()
    if fn is None:
        return False
    try:
        import shiboken6
        from PySide6.QtGui import QRegion

        qwindow = widget.windowHandle()
        if qwindow is None:
            return False  # not shown yet — no platform window to blur
        if corner_radius > 0:
            region = _rounded_region(widget, corner_radius)
        else:
            region = QRegion()  # empty == KWindowSystem blurs whole window
        win_ptr = shiboken6.getCppPointer(qwindow)[0]
        reg_ptr = shiboken6.getCppPointer(region)[0]
        fn(ctypes.c_void_p(win_ptr), bool(enabled), ctypes.c_void_p(reg_ptr))
        return True
    except Exception:
        return False
