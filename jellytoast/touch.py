"""Touchscreen support for the widget UI (Plasma Mobile, touch laptops).

Qt Widgets gives a finger only mouse emulation: a drag on a list is a
press-and-drag (no flick), a long-press does nothing, and a press lands the
instant the finger does. jellytoast's grids act on *press*, so touching a
tile to start scrolling opened it. This module fixes that app-wide with one
QApplication event filter — mouse / trackpad input is never affected, every
branch keys off a TouchScreen device:

* **Kinetic scrolling** — every ``QAbstractScrollArea`` viewport grabs
  ``QScroller``'s *touch* gesture the first time it is shown (``TouchGesture``
  only reacts to touch, so desktop wheel / drag behaviour is unchanged).
* **Tap on release** — inside a scrollable viewport a touch press is held
  back; at release it is replayed as press + release (a tap) only if the
  finger barely moved, didn't stop a running flick, and didn't long-press.
* **Long-press → context menu** — holding still ~500 ms sends the same
  ``QContextMenuEvent`` a right-click does, so every existing right-click
  menu (``contextMenuEvent`` overrides and ``CustomContextMenu`` signals
  alike) works by touch; the rest of that touch is swallowed.
* :func:`last_input_was_touch` / :func:`is_touch_event` let delegates skip
  hover-only affordances (corner buttons revealed on mouse hover) on touch.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QInputDevice, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QScroller,
    QScrollerProperties,
    QWidget,
)

logger = logging.getLogger(__name__)

LONG_PRESS_MS = 500
# Movement (px) that turns a touch into a scroll rather than a tap /
# long-press. Fingers wobble far more than a mouse; Qt's mouse drag distance
# (~10) is too twitchy for a fat-finger tap.
TAP_SLOP_PX = 18

_KINETIC_PROP = "jtKineticScroll"
_last_input_touch = False


def is_touch_event(event) -> bool:
    """True when a (mouse) event came from a touchscreen — real touch or
    Qt's mouse emulation of one."""
    try:
        dev = event.device()
        return dev is not None and dev.type() == QInputDevice.DeviceType.TouchScreen
    except Exception:
        return False


def last_input_was_touch() -> bool:
    """Whether the most recent pointer input was a finger (flips back on the
    next real mouse movement). Delegates use it to hide hover-revealed
    affordances, which a finger can't reveal."""
    return _last_input_touch


def enable_kinetic_scrolling(area: QAbstractScrollArea) -> None:
    """Flick-scroll ``area`` by touch. Idempotent."""
    vp = area.viewport()
    if vp is None or vp.property(_KINETIC_PROP):
        return
    vp.setProperty(_KINETIC_PROP, True)
    QScroller.grabGesture(vp, QScroller.ScrollerGestureType.TouchGesture)
    props = QScroller.scroller(vp).scrollerProperties()
    # No rubber-band overshoot: desktop-styled views (and the alphabet rail's
    # cell math) expect the scroll value to stay inside the range.
    off = QScrollerProperties.OvershootPolicy.OvershootAlwaysOff
    props.setScrollMetric(QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy, off)
    props.setScrollMetric(QScrollerProperties.ScrollMetric.VerticalOvershootPolicy, off)
    QScroller.scroller(vp).setScrollerProperties(props)


def _kinetic_viewport_of(widget: Optional[QWidget]) -> Optional[QWidget]:
    while widget is not None:
        if widget.property(_KINETIC_PROP):
            return widget
        widget = widget.parentWidget()
    return None


class _PendingPress:
    """A touch press we held back, recorded by value — Qt reuses the event."""

    def __init__(self, target: QWidget, event: QMouseEvent, viewport: Optional[QWidget]):
        self.target = target
        self.local = QPointF(event.position())
        self.global_ = QPointF(event.globalPosition())
        self.button = event.button()
        self.buttons = event.buttons()
        self.modifiers = event.modifiers()
        self.device = event.pointingDevice()
        self.viewport = viewport
        self.deferred = viewport is not None
        self.consumed = False  # long-press fired / flick stopped → no tap

    def mouse_event(self, kind: QEvent.Type, buttons) -> QMouseEvent:
        return QMouseEvent(
            kind, self.local, self.global_, self.button, buttons, self.modifiers, self.device
        )


class TouchFilter(QObject):
    """The app-wide filter. Install once on the QApplication."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending: Optional[_PendingPress] = None
        self._replaying = False
        self._long_press = QTimer(self)
        self._long_press.setSingleShot(True)
        self._long_press.setInterval(LONG_PRESS_MS)
        self._long_press.timeout.connect(self._fire_long_press)

    # Touch-point tracking happens on the TOUCH events (they reach the app
    # filter even when a QScroller swallows them for the widget), the
    # tap / long-press decisions on the emulated mouse events.

    def eventFilter(self, obj, event):  # noqa: N802 — Qt naming
        global _last_input_touch
        et = event.type()
        if et == QEvent.Type.Show and isinstance(obj, QAbstractScrollArea):
            try:
                enable_kinetic_scrolling(obj)
            except Exception as e:  # pragma: no cover — defensive
                logger.debug("kinetic scrolling unavailable: %s", e)
            return False
        if self._replaying:
            return False
        if et == QEvent.Type.TouchUpdate:
            self._track_movement(event)
            return False
        if et == QEvent.Type.MouseMove and not is_touch_event(event):
            _last_input_touch = False
            return False
        if not isinstance(obj, QWidget) or not is_touch_event(event):
            return False
        if et == QEvent.Type.MouseButtonPress:
            _last_input_touch = True
            return self._on_press(obj, event)
        if et == QEvent.Type.MouseMove:
            # Held-back or consumed sequences don't drag-select underneath.
            p = self._pending
            return p is not None and (p.deferred or p.consumed)
        if et == QEvent.Type.MouseButtonRelease:
            return self._on_release(obj, event)
        if et == QEvent.Type.MouseButtonDblClick:
            return self._pending is not None and self._pending.deferred
        return False

    def _on_press(self, target: QWidget, event) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        viewport = _kinetic_viewport_of(target)
        pending = _PendingPress(target, event, viewport)
        if viewport is not None:
            state = QScroller.scroller(viewport).state()
            if state == QScroller.State.Scrolling:
                # This touch is stopping a flick — never also a tap.
                pending.consumed = True
        self._pending = pending
        self._start = event.globalPosition()
        self._long_press.start()
        return pending.deferred

    def _track_movement(self, event) -> None:
        p = self._pending
        if p is None or p.consumed:
            return
        points = event.points()
        if not points:
            return
        delta = points[0].globalPosition() - self._start
        if abs(delta.x()) > TAP_SLOP_PX or abs(delta.y()) > TAP_SLOP_PX:
            # It's a scroll / drag now: no tap at release, no long-press.
            self._long_press.stop()
            if p.deferred:
                p.consumed = True

    def _on_release(self, target: QWidget, event) -> bool:
        p, self._pending = self._pending, None
        self._long_press.stop()
        if p is None:
            return False
        if p.consumed:
            if not p.deferred and p.target is not None:
                # The press already reached this (non-scrolling) widget — a
                # button is sitting "down". Release it somewhere it can't
                # count as a click, so it resets without firing.
                self._replaying = True
                try:
                    p.local = QPointF(-1e6, -1e6)
                    QApplication.sendEvent(
                        p.target,
                        p.mouse_event(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
                    )
                finally:
                    self._replaying = False
            return True  # long-press menu shown / scroll / flick stop
        if not p.deferred:
            return False  # ordinary tap on a non-scrolling widget
        # A tap inside a scrollable view: replay the held-back press now,
        # then let the real release through to the same widget.
        if p.target is not None:
            self._replaying = True
            try:
                QApplication.sendEvent(
                    p.target, p.mouse_event(QEvent.Type.MouseButtonPress, p.buttons)
                )
                QApplication.sendEvent(
                    p.target,
                    p.mouse_event(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
                )
            finally:
                self._replaying = False
        return True

    def _fire_long_press(self) -> None:
        p = self._pending
        if p is None or p.consumed or p.target is None:
            return
        if p.viewport is not None:
            state = QScroller.scroller(p.viewport).state()
            if state in (QScroller.State.Dragging, QScroller.State.Scrolling):
                return
        local = QPoint(int(p.local.x()), int(p.local.y()))
        glob = QPoint(int(p.global_.x()), int(p.global_.y()))
        # Starts accepted; widgets with no menu ignore() it as Qt propagates
        # it up the parents, so "still accepted" == a menu was shown.
        menu_event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, glob, p.modifiers)
        self._replaying = True
        try:
            QApplication.sendEvent(p.target, menu_event)
        finally:
            self._replaying = False
        # Only a menu that something actually showed turns the touch into a
        # long-press; with no context menu here the release stays a tap.
        p.consumed = menu_event.isAccepted()


def install(app: QApplication) -> TouchFilter:
    """Install the app-wide touch filter (once) and return it."""
    existing = getattr(app, "_jt_touch_filter", None)
    if existing is not None:
        return existing
    f = TouchFilter(app)
    app.installEventFilter(f)
    app._jt_touch_filter = f
    return f
