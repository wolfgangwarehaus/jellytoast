"""Indeterminate loading spinner + a centered "Loading…" page.

A cold library fetch (first sign-in, no disk cache yet) can take a while
against a big server, and a blank grid in the meantime reads as "did the
login fail?". ``LoadingState`` fills that gap: a spinning accent arc over a
short caption, dropped into a surface's content stack until items land.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from jellytoast import ui_helpers as _u


class BusySpinner(QWidget):
    """A rotating accent arc. Only ticks while visible."""

    TICK_MS = 16
    STEP_DEG = 6
    ARC_DEG = 100

    def __init__(self, size: int = 36, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt naming
        return QSize(self._size, self._size)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + self.STEP_DEG) % 360
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(2.0, self._size / 10)
        inset = width / 2 + 1
        rect = QRectF(inset, inset, self._size - 2 * inset, self._size - 2 * inset)
        # Faint full track, accent arc on top. Tokens are read live so a
        # theme / accent switch shows on the next frame.
        track = QColor(_u.ACCENT)
        track.setAlphaF(0.18)
        pen = QPen(track, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawEllipse(rect)
        pen.setColor(QColor(_u.ACCENT))
        p.setPen(pen)
        # Qt angles: 1/16 degree, counter-clockwise from 3 o'clock.
        p.drawArc(rect, int(-self._angle * 16), int(-self.ARC_DEG * 16))
        p.end()


class LoadingState(QWidget):
    """Centered spinner over a one-line caption."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)
        outer.addStretch(1)
        self._spinner = BusySpinner(36, self)
        outer.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        outer.addWidget(self._label)
        outer.addStretch(1)
        self._apply_styling()
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._apply_styling)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def _apply_styling(self) -> None:
        from jellytoast.design_tokens import TYPE_CAPTION, type_qss

        self._label.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
