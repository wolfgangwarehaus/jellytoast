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

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

import jellytoast.ui_helpers as _tokens
from jellytoast.design_tokens import TYPE_CAPTION, rad, type_qss


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
        # Read tokens live (module attribute access, not `from import`)
        # so a runtime theme/accent change is reflected.
        self.setStyleSheet(
            f"""
            QFrame#jtToast {{
                background: {_tokens.BG_PANEL};
                border: 1px solid {_tokens.BORDER};
                border-radius: {rad(8)}px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)
        label = QLabel(message)
        label.setStyleSheet(f"color: {_tokens.TEXT}; {type_qss(TYPE_CAPTION)}")
        layout.addWidget(label)

        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        QTimer.singleShot(self._HOLD_MS, self._begin_fade)

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
