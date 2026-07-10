"""In-app toast — a transient, non-interactive status message.

A small pill anchored to the bottom-centre of a host widget that fades
out on its own after a short hold. Used for low-stakes "something just
happened" feedback (e.g. the failover engine switching to an alternate
server URL) where a modal dialog or an OS notification would be too
heavy.

Single entry point: ``show_toast(host, message)``. The toast parents
itself to ``host``, positions itself, shows, and self-destroys — the
caller keeps no reference.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

import jellytoast.ui_helpers as _tokens
from jellytoast.design_tokens import RADIUS_MD, TYPE_CAPTION, type_qss


class _Toast(QFrame):
    """One transient message pill. Constructed, shown, and reaped by
    ``show_toast`` — not meant to be held onto."""

    # Visible hold before the fade starts, and the fade length.
    _HOLD_MS = 3500
    _FADE_MS = 320

    def __init__(self, host: QWidget, message: str, bottom_margin: int = 28):
        super().__init__(host)
        # Gap from the host's bottom edge — callers bump this to clear
        # bottom chrome (e.g. the now-playing bar on the main window).
        self._bottom_margin = bottom_margin
        self.setObjectName("jtToast")
        # Purely informational — never eat a click meant for the
        # content underneath it.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Translucent so the rounded pill painted in paintEvent shows with
        # clear corners (no square box behind it) — matches the hover
        # tooltip's surface (custom_tooltip.ToolTipPopup).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)
        label = QLabel(message)
        # No background on the label — only the pill shows behind the text.
        label.setStyleSheet(
            f"background: transparent; color: {_tokens.TEXT}; {type_qss(TYPE_CAPTION)}"
        )
        layout.addWidget(label)

        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        QTimer.singleShot(self._HOLD_MS, self._begin_fade)

    def paintEvent(self, _e):  # noqa: N802 — Qt naming
        """Paint the frosted pill — the SAME body colour and rounded shape
        the hover tooltip uses (``popup_paint_qcolor``: theme- and
        blur-aware, dark in the dark family). Was a QSS ``BG_PANEL`` fill
        + border, which is the light card tone and read pale over blur,
        mismatching the tooltips."""
        from jellytoast.ui_helpers import popup_paint_qcolor

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), float(RADIUS_MD), float(RADIUS_MD))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(popup_paint_qcolor())
            p.drawPath(path)
        finally:
            p.end()

    def _reposition(self) -> None:
        """Centre the pill horizontally near the host's bottom edge."""
        host = self.parentWidget()
        if host is None:
            return
        x = (host.width() - self.width()) // 2
        y = host.height() - self.height() - self._bottom_margin
        self.move(max(0, x), max(0, y))

    def _begin_fade(self) -> None:
        """Fade to zero opacity, then delete. The opacity effect is
        attached only for the fade and dies with the widget — leaving a
        QGraphicsOpacityEffect attached causes half-painted repaints on
        Wayland (see feedback in project memory)."""
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(self._FADE_MS)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.finished.connect(self.deleteLater)
        # Hold a ref so the animation isn't GC'd mid-fade.
        self._fade_anim = anim
        anim.start()


def show_toast(host: QWidget, message: str, *, bottom_margin: int = 28) -> QWidget:
    """Pop a transient ``message`` over ``host``. The toast positions
    itself, fades out after a few seconds, and deletes itself — the
    return value is only useful to tests. ``bottom_margin`` lifts the
    pill clear of any bottom chrome the host carries."""
    return _Toast(host, message, bottom_margin)
