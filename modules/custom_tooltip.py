"""Custom frosted tooltip popup that replaces Qt's QTipLabel entirely.

Same motivation as ``_Selector`` replacing QComboBox: Qt's internal popup
(QTipLabel) misbehaves under KDE Wayland in ways we can't fix from outside.

Two concrete failures drove this:

* Qt reuses ONE QTipLabel instance for the whole app. After a live theme
  swap a re-polish leaves that reused surface OPAQUE, so its square corners
  show as a theme-coloured black/white box behind the rounded pill — but
  only on a live swap, since a process restart rebuilds it translucent (the
  "correct on restart, wrong on swap" tell the user reported).
* ``QToolTip.showText(pos, …)`` hard-codes a ``+(2, 16)`` offset inside
  ``QTipLabel::placeTip()``, and Wayland's xdg_popup positioner won't honour
  a post-show ``move()`` — so we can neither place it where we ask nor correct
  it after the fact.

Owning a plain top-level translucent QWidget sidesteps both: we ``adjustSize()``
to learn the real width BEFORE positioning, ``move()`` to the centred-under-
target spot before ``show()`` (Wayland honours it because this is a regular
surface, not an xdg_popup), Source-paint the frosted body ourselves and ride
KWin blur — the exact pattern the volume popup uses.

Install once at startup via :func:`install`; drive the live sleep-timer
countdown via ``ToolTipPopup.instance().refresh_text(btn, text)``.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from modules.settings import get_settings


def _active_text_colour() -> str:
    """Read the current theme TEXT each call so colour stays live across
    theme swaps without rebuilding the popup widget."""
    from modules import ui_helpers as _uh

    return _uh.TEXT


class ToolTipPopup(QWidget):
    """Top-level frosted tooltip widget — see module docstring."""

    _instance: "ToolTipPopup | None" = None
    _RADIUS = 6
    _GAP = 4  # vertical gap between target widget bottom and tooltip top
    _DURATION_MS = 10000  # Qt's native tooltip lifetime is ~10s; mirror it.

    @classmethod
    def instance(cls) -> "ToolTipPopup":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Qt.ToolTip + FramelessWindowHint keeps us above other windows and
        # prevents focus-steal. WA_ShowWithoutActivating is belt-and-braces
        # against focus theft on activation.
        super().__init__(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        from PySide6.QtWidgets import QToolTip as _QToolTip

        self._label = QLabel(self)
        self._label.setFont(_QToolTip.font())
        layout = QHBoxLayout(self)
        # Matches the old QSS `padding: 4px 8px`.
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)
        layout.addWidget(self._label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        # Track the target so we can re-centre on a sibling-tooltip request
        # without races, and hide when the cursor leaves it.
        self._target: QWidget | None = None

    def _apply_text_colour(self) -> None:
        from PySide6.QtGui import QPalette

        # Pull the current theme text colour each show — theme can change
        # between two hovers and the QPalette is the simplest portable path
        # that doesn't need a QSS reflow.
        pal = self._label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, QColor(_active_text_colour()))
        self._label.setPalette(pal)

    def _position_under(self, target: QWidget) -> None:
        tr = target.rect()
        cx = target.mapToGlobal(tr.center()).x()
        by = target.mapToGlobal(tr.bottomLeft()).y()
        self.move(cx - self.width() // 2, by + self._GAP)

    def show_under(self, target: QWidget, text: str) -> None:
        from modules.theme import get_active_theme

        self._target = target
        self._label.setText(text)
        self._apply_text_colour()

        # Resize to the laid-out content BEFORE positioning so we know the
        # real width when computing the centred-x. This is the whole point of
        # owning the widget — QTipLabel doesn't expose a "size first, position
        # second" flow.
        self.adjustSize()
        self._position_under(target)

        self.show()
        self._hide_timer.start(self._DURATION_MS)

        # Frosted themes get compositor blur behind the rounded body. Deferred
        # a tick so the QWindow surface exists and KWin can find it to install
        # the blur region.
        if get_active_theme().blur:
            try:
                from modules import blur as _blur

                QTimer.singleShot(
                    0,
                    lambda: _blur.apply(self, True, corner_radius=self._RADIUS),
                )
            except Exception:
                pass

    def refresh_text(self, target: QWidget, text: str) -> None:
        """Live-update the text of an already-visible tooltip without a full
        re-show (no re-blur, no flicker). Used by the sleep-timer countdown,
        which ticks the remaining time once a second while hovered.

        Falls back to :meth:`show_under` if we're not currently showing for
        ``target`` (e.g. the very first tick), so the caller doesn't have to
        track visibility itself."""
        if self._target is target and self.isVisible():
            self._label.setText(text)
            self.adjustSize()
            self._position_under(target)
            # Keep the auto-hide window rolling while the countdown updates.
            self._hide_timer.start(self._DURATION_MS)
        else:
            self.show_under(target, text)

    def hide_for(self, target: QWidget) -> None:
        """Hide if currently shown for ``target``. No-op otherwise — keeps a
        Leave on a different widget from yanking an unrelated tooltip."""
        if self._target is target:
            self._hide_timer.stop()
            self.hide()
            self._target = None

    def paintEvent(self, event):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter as _QP
        from PySide6.QtGui import QPainterPath

        from modules.ui_helpers import popup_paint_qcolor

        p = _QP(self)
        try:
            p.setRenderHint(_QP.RenderHint.Antialiasing, True)
            # Source composition so the painted alpha REPLACES whatever Qt
            # cleared the surface to — the same trick the volume popup uses to
            # keep frosted-theme blur visible through the translucent body.
            p.setCompositionMode(_QP.CompositionMode.CompositionMode_Source)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(self.rect()),
                float(self._RADIUS),
                float(self._RADIUS),
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(popup_paint_qcolor())
            p.drawPath(path)
        finally:
            p.end()


class ToolTipFilter(QObject):
    """Intercepts ``QEvent.ToolTip`` globally to drive :class:`ToolTipPopup`
    instead of letting Qt show its native QTipLabel.

    Tooltips are set per-widget via ``setToolTip()``; Qt fires a single
    ``QEvent.ToolTip`` on hover-pause (~700 ms) to ask the widget where to
    place the popup. We consume that event, show our own popup centred-and-
    flush under the widget, and enforce the user's "Show tooltips" setting on
    the same path."""

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.ToolTip:
            if not get_settings().show_tooltips:
                return True
            if isinstance(obj, QWidget):
                tip = obj.toolTip()
                if tip:
                    try:
                        ToolTipPopup.instance().show_under(obj, tip)
                    except Exception:
                        return False  # let Qt's native path try
                    return True
            return False
        # Hide our popup when the cursor leaves the widget it was shown for —
        # mirrors Qt's native tooltip auto-hide behaviour.
        if et == QEvent.Type.Leave and isinstance(obj, QWidget):
            popup = ToolTipPopup._instance
            if popup is not None and popup.isVisible():
                popup.hide_for(obj)
        return False


def install(app) -> ToolTipFilter:
    """Create the global tooltip filter and install it on ``app``. Returns the
    filter so the caller can keep a reference (Qt doesn't own event filters)."""
    flt = ToolTipFilter(app)
    app.installEventFilter(flt)
    return flt
