"""``Selector`` — a ``QPushButton + QMenu`` replacement for
``QComboBox``.

Lifted out of ``settings_dialog`` so other surfaces (the login view,
future first-run setup screens, anywhere we want the same dropdown
visual + popup behaviour as the settings dialog) can use it without
duplicating the QSS or the menu-positioning logic.

Why not ``QComboBox``? Its popup is ``QComboBoxPrivateContainer``, a
Qt-internal widget whose rendering / sizing / dismiss behaviour we
can't override cleanly. On KDE Wayland under a translucent parent
with a noborder rule, that popup misbehaves on first show in ways
that fight every workaround we tried (first-click dropped, popup
oversized on first open, popup dismissed by any post-show resize).
``QMenu`` is a first-class Qt popup we already style via
``opaque_menu()`` — handles its own sizing, click-outside dismiss,
keyboard nav, accelerator keys, and integrates with our frosted-
popup pass.

Styling lives at the **host surface**, not on the widget. Use
``selector_qss()`` below to render the rule block; the host
(settings dialog, login view) merges it into its own
``setStyleSheet`` and refreshes on ``PlayerBus.theme_changed``.

Why not self-style? At widget-scope ``setStyleSheet`` Qt's QSS
parser handles the ``background: …; background-image: url(…);
background-position: right 10px center;`` triple differently than at
parent-scope: the chevron drops to the default ``left top`` and
overlaps the text. Living on the host's stylesheet matches how the
class worked before extraction and is byte-identical between
settings and login.

API mirrors the QComboBox subset our call-sites use:
``addItem(label, data)``, ``count()``, ``itemData(i)``,
``itemText(i)``, ``currentData()``, ``currentText()``,
``currentIndex()``, ``setCurrentIndex(i)``, ``findData(data)``,
``clear()``, and a ``currentIndexChanged`` signal that fires on
user pick.
"""

from __future__ import annotations

from typing import List, Tuple

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QPushButton

from jellytoast.design_tokens import TYPE_BODY, type_qss


def _dot_icon(hex_color: str, size: int = 12) -> QIcon:
    """A small filled circle — the menu-row category tag (see
    ``Selector.addItem``'s ``dot_color``)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(hex_color))
    inset = 2
    p.drawEllipse(inset, inset, size - 2 * inset, size - 2 * inset)
    p.end()
    return QIcon(pm)

# Inset of the chevron from the widget's right edge. Matches the
# 10-px right inset the original QSS targeted via the
# (Qt-unsupported) ``background-position: right 10px center``.
_CHEVRON_RIGHT_PAD = 10
_CHEVRON_SIZE = 12


def selector_qss(host_selector: str = "") -> str:
    """Return the QSS rule block that styles ``Selector`` instances
    (matched on ``QPushButton#jtSelector``). Pulls the active theme's
    accent + text colour at call-time, so hosts that subscribe to
    ``PlayerBus.theme_changed`` and re-apply this on every fire get
    live accent updates for free.

    ``host_selector`` (optional) prepends a parent selector to every
    rule for **specificity bumping**. Use when the host already
    carries a broad rule that would otherwise win on specificity —
    e.g. LoginView's ``QWidget#loginView QWidget { background:
    transparent; }`` would override an unprefixed
    ``QPushButton#jtSelector { background: ink_alpha(...); }`` and
    leave the Selector body see-through. Passing
    ``"QWidget#loginView"`` here gives every Selector rule the same
    parent-id weight so it wins.

    The chevron is painted by ``Selector.paintEvent``, *not* via
    ``background-image``. Qt's QSS docs only spec the keyword form
    for ``background-position`` (e.g. ``right center``); the CSS3
    four-value offset form (``right 10px center``) is silently
    ignored and the image falls back to ``top left`` — which lands
    right on top of the dropdown's first letter. The paint path
    sidesteps that entirely and gives us a 10-px inset for free.

    Designed to be **merged** into the host's existing stylesheet
    rather than set as the host's whole sheet — concatenate it with
    whatever the host already owns.
    """
    from jellytoast.theme import _hex_to_rgb
    from jellytoast.ui_helpers import ACCENT, TEXT, TEXT_FAINT, ink_alpha

    try:
        _ar, _ag, _ab = _hex_to_rgb(ACCENT)
    except Exception:
        _ar, _ag, _ab = (255, 255, 255)
    prefix = f"{host_selector} " if host_selector else ""
    return f"""
        {prefix}QPushButton#jtSelector {{
            background: {ink_alpha(0.06)};
            color: {TEXT};
            border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
            border-radius: 6px;
            padding: 6px 32px 6px 12px;
            {type_qss(TYPE_BODY)}
            text-align: left;
            outline: 0;
        }}
        {prefix}QPushButton#jtSelector:hover {{
            border-color: rgba({_ar},{_ag},{_ab},0.65);
        }}
        {prefix}QPushButton#jtSelector:focus {{
            border-color: rgba({_ar},{_ag},{_ab},0.85);
        }}
        {prefix}QPushButton#jtSelector:disabled {{
            color: {TEXT_FAINT};
        }}
    """


class Selector(QPushButton):
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("jtSelector")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # autoDefault off so the selector doesn't get the Qt-default
        # "default action" outline when its dialog is shown — matches
        # the About dialog close-button fix.
        self.setAutoDefault(False)
        self.setDefault(False)
        self._items: List[Tuple[str, object]] = []
        self._dot_colors: dict[int, str] = {}
        self._current_index = -1
        self.clicked.connect(self._show_menu)

    # ── QComboBox-compatible API ─────────────────────────────────────
    def addItem(self, label: str, data=None, dot_color: str = "") -> None:
        """``dot_color`` (``#rrggbb``) paints a small filled circle as
        the row's menu icon — a family/category tag (e.g. the audio
        output picker color-codes PipeWire vs ALSA devices)."""
        self._items.append((label, data))
        self._dot_colors[len(self._items) - 1] = dot_color
        if self._current_index < 0:
            self.setCurrentIndex(0)

    def count(self) -> int:
        return len(self._items)

    def itemData(self, i: int):
        if 0 <= i < len(self._items):
            return self._items[i][1]
        return None

    def itemText(self, i: int) -> str:
        if 0 <= i < len(self._items):
            return self._items[i][0]
        return ""

    def findData(self, data, role=None) -> int:
        """Return the index of the first item whose data matches; -1
        if none. ``role`` accepted for QComboBox API parity but
        ignored (we only store one data value per item)."""
        for i, (_label, item_data) in enumerate(self._items):
            if item_data == data:
                return i
        return -1

    def currentData(self):
        return self.itemData(self._current_index)

    def currentText(self) -> str:
        return self.itemText(self._current_index)

    def currentIndex(self) -> int:
        return self._current_index

    def setCurrentIndex(self, i: int) -> None:
        if 0 <= i < len(self._items):
            changed = i != self._current_index
            self._current_index = i
            self.setText(self._items[i][0])
            if changed:
                self.currentIndexChanged.emit(i)

    def clear(self) -> None:
        self._items.clear()
        self._dot_colors.clear()
        self._current_index = -1
        self.setText("")

    # ── Chevron paint ─────────────────────────────────────────────────
    def paintEvent(self, e):  # noqa: N802 — Qt naming
        """Paint the QPushButton chrome, then overdraw a chevron-down
        glyph at the right edge of the widget. Replaces the QSS-based
        background-image approach which Qt's QSS parser can't
        position with an offset (see ``selector_qss`` docstring)."""
        super().paintEvent(e)
        from jellytoast.icons import _SVG
        from jellytoast.ui_helpers import TEXT

        svg_template = _SVG.get("chevron_down")
        if not svg_template:
            return
        svg_bytes = svg_template.replace("currentColor", TEXT).encode("utf-8")
        renderer = QSvgRenderer(svg_bytes)
        if not renderer.isValid():
            return
        # Right edge of the widget minus the inset; vertically centred.
        x = self.width() - _CHEVRON_RIGHT_PAD - _CHEVRON_SIZE
        y = (self.height() - _CHEVRON_SIZE) / 2.0
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(
                p,
                QRectF(float(x), float(y), float(_CHEVRON_SIZE), float(_CHEVRON_SIZE)),
            )
        finally:
            p.end()

    # ── Menu popup ────────────────────────────────────────────────────
    def _show_menu(self) -> None:
        from jellytoast.ui_helpers import opaque_menu

        menu = opaque_menu(self)
        # Width at least the button — items shouldn't read narrower
        # than the closed state they came from.
        menu.setMinimumWidth(self.width())
        # No checkmark on the current item — the selector button
        # itself shows the current value at all times, so a left-
        # column check is redundant and would shove every label
        # right.
        for i, (label, _data) in enumerate(self._items):
            action = menu.addAction(label)
            dot = self._dot_colors.get(i)
            if dot:
                action.setIcon(_dot_icon(dot))
            action.triggered.connect(
                lambda _checked=False, idx=i: self.setCurrentIndex(idx)
            )
        # Position: prefer below the button; flip above when the menu
        # would extend past either the parent dialog's bottom or the
        # screen's available bottom. Always anchor with a small gap
        # so the menu doesn't graze the button. Horizontally centre
        # the menu on the button so the dropdown reads as balanced
        # under the chevron, regardless of label width.
        _GAP = 8
        menu.ensurePolished()
        menu_w = max(menu.sizeHint().width(), self.width())
        menu_h = menu.sizeHint().height()
        btn_global = self.mapToGlobal(QPoint(0, 0))
        btn_bottom_y = btn_global.y() + self.height()
        btn_center_x = btn_global.x() + self.width() // 2
        # Screen bottom (Wayland popups can clip against screen edge).
        screen = QApplication.screenAt(btn_global) or QApplication.primaryScreen()
        screen_bottom = screen.availableGeometry().bottom()
        # Dialog bottom (visual containment — keeps the menu inside
        # the host card).
        win = self.window()
        if win is not None:
            dlg_bottom = win.mapToGlobal(QPoint(0, win.height())).y()
            below_limit = min(screen_bottom, dlg_bottom)
        else:
            below_limit = screen_bottom
        room_below = below_limit - btn_bottom_y
        pos_x = btn_center_x - menu_w // 2
        if menu_h + _GAP > room_below and btn_global.y() > menu_h + _GAP:
            # Not enough room below — flip above the button.
            pos = QPoint(pos_x, btn_global.y() - menu_h - _GAP)
        else:
            pos = QPoint(pos_x, btn_bottom_y + _GAP)
        menu.exec(pos)
