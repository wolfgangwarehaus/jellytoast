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

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QProxyStyle,
    QPushButton,
    QStyle,
    QStyleFactory,
    QWidgetAction,
)

from jellytoast.design_tokens import TYPE_BODY, rad, type_qss


class _ScrollableMenuStyle(QProxyStyle):
    """Force a QMenu to SCROLL an over-tall popup instead of running off the
    screen. The Fusion style leaves menus non-scrollable
    (``SH_Menu_Scrollable`` = 0), so a 200+ row list (the font-family picker)
    would otherwise shove its top off the top of the screen. Applied to a
    single menu instance so it never changes any other menu's behaviour."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_Menu_Scrollable:
            return 1
        return super().styleHint(hint, option, widget, returnData)


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
            border-radius: {rad(6)}px;
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

    # Above this many items the dropdown swaps its per-QAction menu for a
    # scrollable QListWidget hosted in the menu (a proper drag-thumb scrollbar
    # + smooth wheel, consistent across Linux/Windows/macOS — native menu
    # scrolling is clunky and per-OS). The font-family picker (~200 families)
    # trips this; every other selector (theme, quality, ~<10) stays a menu.
    _LONG_LIST_THRESHOLD = 40

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
        # Optional per-row font (QFont), keyed by index — used by the font
        # picker so each family renders its menu row in its own typeface.
        self._fonts: dict[int, "QFont"] = {}
        # Optional per-row QIcon, keyed by index — a richer alternative to
        # dot_color (e.g. the theme-preset picker shows each scheme's colour
        # palette). Shown BOTH in the popup rows and on the closed button.
        self._icons: dict[int, "QIcon"] = {}
        # Optional per-row tooltip, keyed by index — set via the QComboBox-
        # compatible setItemData(i, text, ToolTipRole) (the smart-playlist
        # editor annotates provider-unsupported rule fields this way).
        self._tooltips: dict[int, str] = {}
        self._current_index = -1
        self.clicked.connect(self._show_menu)

    # ── QComboBox-compatible API ─────────────────────────────────────
    def addItem(self, label: str, data=None, dot_color: str = "", font=None, icon=None) -> None:
        """``dot_color`` (``#rrggbb``) paints a small filled circle as
        the row's menu icon — a family/category tag (e.g. the audio
        output picker color-codes PipeWire vs ALSA devices).

        ``font`` (``QFont``) renders THIS row's menu label in its own
        typeface — the font picker passes ``QFont(family)`` so the list
        previews each font in itself. Ignored for the closed-state text
        (the button keeps the UI font).

        ``icon`` (``QIcon``) is a full custom icon for the row AND the closed
        button (e.g. the theme-preset picker's colour-palette strip); takes
        precedence over ``dot_color``."""
        idx = len(self._items)
        self._items.append((label, data))
        self._dot_colors[idx] = dot_color
        if font is not None:
            self._fonts[idx] = font
        if icon is not None:
            self._icons[idx] = icon
        if self._current_index < 0:
            self.setCurrentIndex(0)

    def count(self) -> int:
        return len(self._items)

    def itemData(self, i: int, role=None):
        """``role`` mirrors QComboBox.itemData: UserRole/None returns the
        item's data value, ToolTipRole the tooltip set via setItemData."""
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltips.get(i)
        if 0 <= i < len(self._items):
            return self._items[i][1]
        return None

    def setItemData(self, i: int, value, role=None) -> None:
        """QComboBox-compatible per-item data write. UserRole/None replaces
        the item's data value; ToolTipRole sets the row's popup tooltip.
        Other roles are ignored (nothing stores them). Grew out of the
        smart-playlist editor calling the QComboBox API on the row-field
        Selector — which only ever happens on Subsonic (unsupported-field
        tooltips), so a missing method here broke rule-add on Navidrome
        while Jellyfin sailed by."""
        if not 0 <= i < len(self._items):
            return
        if role == Qt.ItemDataRole.ToolTipRole:
            self._tooltips[i] = str(value)
        elif role is None or role == Qt.ItemDataRole.UserRole:
            self._items[i] = (self._items[i][0], value)

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
            ic = self._icons.get(i)
            if ic is not None:
                self.setIcon(ic)  # closed button previews the current row's icon
            if changed:
                self.currentIndexChanged.emit(i)

    def clear(self) -> None:
        self._items.clear()
        self._dot_colors.clear()
        self._fonts.clear()
        self._icons.clear()
        self._current_index = -1
        self.setText("")
        self.setIcon(QIcon())

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
        # Width at least the button — items shouldn't read narrower than the
        # closed state they came from.
        menu.setMinimumWidth(self.width())

        # Vertical bounds the popup must stay WITHIN: the host dialog's top and
        # bottom when there is one (so a long dropdown stays inside the settings
        # panel instead of spilling past it), else the screen. cap_h is the room
        # between them; the list caps itself to that and scrolls the overflow.
        btn_global = self.mapToGlobal(QPoint(0, 0))
        screen = QApplication.screenAt(btn_global) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        top_limit = avail.top() + 8
        bot_limit = avail.bottom() - 8
        win = self.window()
        if win is not None:
            wt = win.mapToGlobal(QPoint(0, 0)).y()
            top_limit = max(top_limit, wt + 8)
            bot_limit = min(bot_limit, wt + win.height() - 8)
        cap_h = max(240, bot_limit - top_limit)

        # Long lists (the ~200-family font picker) use a scrollable QListWidget
        # hosted in the menu — a real drag-thumb scrollbar + smooth wheel,
        # consistent across platforms. Short selectors stay per-QAction menus.
        long_list = len(self._items) > self._LONG_LIST_THRESHOLD
        list_widget = self._add_list_widget(menu, cap_h) if long_list else None
        if not long_list:
            # No checkmark on the current item — the selector button shows the
            # current value already, so a left check just shoves labels right.
            if self._tooltips:
                # QMenu suppresses action tooltips unless asked.
                menu.setToolTipsVisible(True)
            for i, (label, _data) in enumerate(self._items):
                action = menu.addAction(label)
                tip = self._tooltips.get(i)
                if tip:
                    action.setToolTip(tip)
                custom_icon = self._icons.get(i)
                dot = self._dot_colors.get(i)
                if custom_icon is not None:
                    action.setIcon(custom_icon)
                elif dot:
                    action.setIcon(_dot_icon(dot))
                row_font = self._fonts.get(i)
                if row_font is not None:
                    # Render this row in its own font (font-picker preview); keep
                    # the design size/weight so long family names don't blow up
                    # the menu — only the family is per-row.
                    f = QFont(row_font)
                    f.setPixelSize(TYPE_BODY.size_px)
                    action.setFont(f)
                action.triggered.connect(
                    lambda _checked=False, idx=i: self.setCurrentIndex(idx)
                )

        # Position: prefer below the button; flip above when it would extend
        # past the parent dialog's or the screen's bottom. Centre on the button.
        _GAP = 8
        menu.ensurePolished()
        menu_w = max(menu.sizeHint().width(), self.width())
        btn_bottom_y = btn_global.y() + self.height()
        btn_center_x = btn_global.x() + self.width() // 2
        menu_h = menu.sizeHint().height()
        # A SHORT menu that's still taller than the cap (rare) forces QMenu
        # scroll via a per-menu proxy style (Fusion leaves menus non-scrollable).
        # The long-list QListWidget is already internally capped, so skip it.
        if not long_list and menu_h > cap_h:
            base = QStyleFactory.create(QApplication.style().objectName())
            menu._jt_scroll_style = (
                _ScrollableMenuStyle(base) if base else _ScrollableMenuStyle()
            )
            menu.setStyle(menu._jt_scroll_style)
            menu.setMaximumHeight(cap_h)
            menu_h = cap_h
        room_below = bot_limit - btn_bottom_y
        pos_x = btn_center_x - menu_w // 2
        if menu_h + _GAP > room_below and (btn_global.y() - _GAP) - menu_h > top_limit:
            pos = QPoint(pos_x, btn_global.y() - menu_h - _GAP)
        else:
            pos = QPoint(pos_x, btn_bottom_y + _GAP)
        # Final clamp: keep the WHOLE popup within the vertical bounds (the
        # settings panel when hosted in one) rather than the raw screen — so a
        # tall list fills the panel instead of spilling past its bottom.
        pos.setY(max(top_limit, min(pos.y(), bot_limit - menu_h)))
        # Focus the list + scroll the current row into view once shown (fires in
        # the menu's own event loop during exec).
        if list_widget is not None:
            QTimer.singleShot(0, lambda lw=list_widget: self._focus_list(lw))
        menu.exec(pos)

    def _focus_list(self, lw: QListWidget) -> None:
        try:
            lw.setFocus(Qt.FocusReason.PopupFocusReason)
            cur = lw.currentItem()
            if cur is not None:
                lw.scrollToItem(cur)
        except RuntimeError:
            pass  # menu closed before the deferred call ran

    def _add_list_widget(self, menu, cap_h: int) -> QListWidget:
        """Populate the menu with a single scrollable QListWidget (proper
        drag-thumb scrollbar + smooth wheel, consistent cross-platform) instead
        of one QAction per row — for long lists like the font picker. The QMenu
        still owns positioning + click-outside dismissal (Wayland-safe); the
        list owns scrolling. Gated by ``_LONG_LIST_THRESHOLD``."""
        from jellytoast.theme import _hex_to_rgb
        from jellytoast.ui_helpers import (
            ACCENT,
            TEXT,
            ink_alpha,
            install_autofade_scrollbars,
        )

        try:
            _ar, _ag, _ab = _hex_to_rgb(ACCENT)
        except Exception:
            _ar, _ag, _ab = (255, 255, 255)

        lw = QListWidget()
        lw.setObjectName("jtSelectorList")
        lw.setFrameShape(QFrame.Shape.NoFrame)
        lw.setUniformItemSizes(True)
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Scroll NATIVELY (opt out of the app-wide smooth-scroll momentum glide,
        # which felt way too fast here) and snap to whole rows — a precise,
        # controlled dropdown feel for a 200-item list.
        lw.setProperty("jt_native_scroll", True)
        lw.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerItem)
        lw.setStyleSheet(
            f"""
            QListWidget#jtSelectorList {{
                background: transparent; border: none; outline: none;
                {type_qss(TYPE_BODY)}
            }}
            QListWidget#jtSelectorList::item {{
                color: {TEXT}; padding: 7px 14px; border-radius: {rad(6)}px;
            }}
            QListWidget#jtSelectorList::item:hover {{ background: {ink_alpha(0.06)}; }}
            QListWidget#jtSelectorList::item:selected {{
                background: rgba({_ar},{_ag},{_ab},0.28); color: {TEXT};
            }}
            """
        )
        for i, (label, _data) in enumerate(self._items):
            it = QListWidgetItem(label)
            tip = self._tooltips.get(i)
            if tip:
                it.setToolTip(tip)
            row_font = self._fonts.get(i)
            if row_font is not None:
                f = QFont(row_font)
                f.setPixelSize(TYPE_BODY.size_px)
                it.setFont(f)
            custom_icon = self._icons.get(i)
            dot = self._dot_colors.get(i)
            if custom_icon is not None:
                it.setIcon(custom_icon)
            elif dot:
                it.setIcon(_dot_icon(dot))
            lw.addItem(it)
        if 0 <= self._current_index < lw.count():
            lw.setCurrentRow(self._current_index)

        def _pick(item):
            self.setCurrentIndex(lw.row(item))
            menu.close()

        lw.itemClicked.connect(_pick)
        lw.itemActivated.connect(_pick)  # Enter / Return

        # Size: at least the button width; height capped so the LIST scrolls
        # internally rather than the menu growing off-screen.
        row_h = lw.sizeHintForRow(0) if lw.count() else 0
        if row_h <= 0:
            row_h = TYPE_BODY.size_px + 16
        content_h = row_h * lw.count() + 2 * lw.frameWidth() + 4
        lw.setMinimumWidth(max(self.width(), 200))
        lw.setFixedHeight(min(content_h, cap_h))
        try:
            install_autofade_scrollbars(lw)
        except Exception:
            pass

        wa = QWidgetAction(menu)
        wa.setDefaultWidget(lw)
        menu.addAction(wa)
        return lw
