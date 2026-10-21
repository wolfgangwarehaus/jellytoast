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


def _env_num(name: str, default: float) -> float:
    import os

    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# Scroll feel, shared by every list (logical px). Lower deceleration = a
# longer glide after a flick; max velocity caps a hard fling. Override with
# JT_SCROLL_DECEL / JT_SCROLL_MAXV to experiment without editing QML.
SCROLL_DECELERATION = 1400.0
SCROLL_MAX_VELOCITY = 7000.0


class Theme(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        PlayerBus.get().theme_changed.connect(self.changed)

    def _bg(self) -> QColor:
        r, g, b, _a = _u.body_color_tuple("main")
        return QColor(r, g, b)  # opaque on a phone

    def _on_accent(self) -> QColor:
        """Text / icon colour that reads on an accent fill."""
        c = _qcolor(_u.ACCENT)
        lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
        return QColor("#111111") if lum > 0.6 else QColor("#ffffff")

    background = Property(QColor, _bg, notify=changed)
    text = Property(QColor, lambda self: _qcolor(_u.TEXT), notify=changed)
    textDim = Property(QColor, lambda self: _qcolor(_u.TEXT_DIM), notify=changed)
    textFaint = Property(QColor, lambda self: _qcolor(_u.TEXT_FAINT), notify=changed)
    accent = Property(QColor, lambda self: _qcolor(_u.ACCENT), notify=changed)
    onAccent = Property(QColor, _on_accent, notify=changed)
    groove = Property(QColor, lambda self: _qcolor(_u.SLIDER_GROOVE), notify=changed)
    surface = Property(QColor, lambda self: _qcolor(_u.POPUP_OPAQUE_FILL), notify=changed)
    scrollDeceleration = Property(
        float, lambda self: _env_num("JT_SCROLL_DECEL", SCROLL_DECELERATION), constant=True
    )
    scrollMaxVelocity = Property(
        float, lambda self: _env_num("JT_SCROLL_MAXV", SCROLL_MAX_VELOCITY), constant=True
    )
