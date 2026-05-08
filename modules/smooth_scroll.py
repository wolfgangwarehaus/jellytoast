"""App-wide smooth scrolling for mouse-wheel input.

Mouse wheels arrive as discrete notches (120 angleDelta units per
click on a standard wheel). Without animation, each notch jumps the
content by a chunk of pixels — janky on long lists and especially
bad on horizontal album rails where the per-notch jump is huge
relative to a tile.

This filter installs at the QApplication level and intercepts every
QWheelEvent. For each event it:

  1. Walks up the receiver's parent chain to find the closest
     QAbstractScrollArea that has scroll *range* on the wheel's
     dominant axis. This naturally bubbles past inner scroll areas
     that can't move on the requested axis (e.g. the Suggestions
     rails, which have no vertical range, so a vertical wheel rolls
     straight up to the page's outer scroll).
  2. Animates that scrollbar's value toward the new target via
     QPropertyAnimation. Successive wheel notches coalesce into one
     moving target so spinning the wheel quickly produces a single
     smooth glide rather than a stack of conflicting tweens.

Trackpad input (non-zero pixelDelta) bypasses animation — the OS
already delivers smooth motion at the native input rate, so adding
an animation layer would lag behind the gesture.

QWebEngineView is skipped — the embedded web page handles its own
scrolling and intercepting wheel here would break it.
"""

from PySide6.QtCore import (
    Qt, QObject, QEvent, QEasingCurve, QPropertyAnimation,
)
from PySide6.QtWidgets import QAbstractScrollArea


# Pixels of bar movement per wheel notch (120 angleDelta units).
# Tuned for a comfortable pace on both vertical lists (~3-4 lines per
# notch) and horizontal album rails (~half a tile per notch).
WHEEL_NOTCH_PIXELS = 90
# Animation duration. Long enough to look smooth, short enough that
# rapid wheel spins still feel responsive — the coalescing logic
# means duration sets the *catch-up* time, not the per-notch lag.
WHEEL_DURATION_MS = 240


def _is_webengine(widget) -> bool:
    """Duck-type check for QWebEngineView. We avoid importing the
    QtWebEngine module here so users on a minimal install don't pay
    its startup cost just to register the smooth-scroll filter."""
    cls = type(widget)
    while cls is not None and cls is not object:
        if cls.__name__ in ("QWebEngineView", "QQuickWidget"):
            return True
        cls = cls.__base__
    return False


class SmoothScrollFilter(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        # bar -> (animation, target_value). The target is what we
        # animate toward; subsequent notches add to it instead of
        # restarting the animation from the bar's current (mid-tween)
        # position.
        self._anims: dict = {}

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel:
            return False

        angle = event.angleDelta()
        pixel = event.pixelDelta()
        # Pick the axis with the larger motion. Trackpad (pixelDelta)
        # takes priority since it carries finer information; mouse
        # wheels have zero pixelDelta on most platforms.
        if pixel.x() or pixel.y():
            vertical = abs(pixel.y()) >= abs(pixel.x())
        else:
            vertical = abs(angle.y()) >= abs(angle.x())

        bar = self._find_scrollable_bar(obj, vertical)
        if bar is None:
            return False

        # Trackpad: apply directly. The OS gesture is already smooth.
        if pixel.x() or pixel.y():
            d = pixel.y() if vertical else pixel.x()
            if d:
                bar.setValue(bar.value() - d)
            return True

        # Mouse wheel: animate.
        d_units = angle.y() if vertical else angle.x()
        if d_units == 0:
            return False
        d_pixels = int(d_units * WHEEL_NOTCH_PIXELS / 120)
        self._animate(bar, -d_pixels)
        return True

    def _find_scrollable_bar(self, widget, vertical: bool):
        while widget is not None:
            if (isinstance(widget, QAbstractScrollArea)
                    and not _is_webengine(widget)):
                # Honor ScrollBarAlwaysOff as a declared "this axis
                # doesn't scroll here" hint — even if the bar reports
                # a few pixels of range from layout rounding, the
                # surface owner has explicitly opted out, so walk up
                # to the next ancestor instead. (The Suggestions
                # rails rely on this so vertical wheel bubbles to the
                # page's outer scroll.)
                policy = (widget.verticalScrollBarPolicy() if vertical
                          else widget.horizontalScrollBarPolicy())
                if policy != Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
                    bar = (widget.verticalScrollBar() if vertical
                           else widget.horizontalScrollBar())
                    if bar is not None and bar.maximum() > bar.minimum():
                        return bar
            widget = widget.parent() if hasattr(widget, "parent") else None
        return None

    def _animate(self, bar, delta_pixels: int):
        existing = self._anims.get(bar)
        if existing is not None:
            anim, target = existing
            anim.stop()
            new_target = target + delta_pixels
        else:
            new_target = bar.value() + delta_pixels
            anim = QPropertyAnimation(bar, b"value")
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        new_target = max(bar.minimum(), min(bar.maximum(), new_target))
        anim.setStartValue(bar.value())
        anim.setEndValue(new_target)
        anim.setDuration(WHEEL_DURATION_MS)
        anim.start()
        self._anims[bar] = (anim, new_target)
