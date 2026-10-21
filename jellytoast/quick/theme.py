"""The active jellytoast theme as properties QML binds to (re-read live on
``PlayerBus.theme_changed``)."""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, Signal
from PySide6.QtGui import QColor

from jellytoast import ui_helpers as _u
from jellytoast.player_state import PlayerBus


def _qcolor(css: str) -> QColor:
    """QML wants a colour, our tokens are CSS strings (``#rrggbb`` or
    ``rgba(r, g, b, a)``)."""
    r, g, b, a = _u._parse_qss_color(css)
    c = QColor(r, g, b)
    c.setAlphaF(a)
    return c


class Theme(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        PlayerBus.get().theme_changed.connect(self.changed)

    def _bg(self) -> QColor:
        r, g, b, _a = _u.body_color_tuple("main")
        return QColor(r, g, b)  # opaque on a phone

    background = Property(QColor, _bg, notify=changed)
    text = Property(QColor, lambda self: _qcolor(_u.TEXT), notify=changed)
    textDim = Property(QColor, lambda self: _qcolor(_u.TEXT_DIM), notify=changed)
    textFaint = Property(QColor, lambda self: _qcolor(_u.TEXT_FAINT), notify=changed)
    accent = Property(QColor, lambda self: _qcolor(_u.ACCENT), notify=changed)
    surface = Property(QColor, lambda self: _qcolor(_u.POPUP_OPAQUE_FILL), notify=changed)
