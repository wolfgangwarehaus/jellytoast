"""Cast device-picker dialog — the unified, collapsible cast menu
(Chromecast / AirPlay / DLNA / Sonos / Snapcast sections, per-device rows,
the cast-proxy + forget-credentials affordances).

Extracted from ``now_playing_bar.py`` (2026-06-02): the three cast classes
(``_CastDeviceRow``, ``_CastSection``, ``CastDialog``) had zero code
coupling to the rest of the bar, so they move out cleanly to shrink the
~3.6k-line bar. Section labels / types / collapse-state live in
``cast_dialog_sections``; ``jellytoast`` opens ``CastDialog`` from here.
"""

import logging
from typing import List

from PySide6.QtCore import (
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QIcon,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from modules.cast_manager import CastDevice, CastManager
from modules.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_SUBHEAD,
    TYPE_TINY,
    font,
    type_qss,
)
from modules.icons import accent_icon, icon
from modules.player_state import PlayerBus
from modules.theme import ink_rgb
from modules.ui_helpers import (
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WASH_HOVER,
    WASH_PRESSED,
    ink_alpha,
)

logger = logging.getLogger(__name__)


class _CastDeviceRow(QWidget):
    """One row in the cast device list: glyph + name/kind + a heart
    toggle. Hearted devices are pinned to the top of the list (the
    dialog re-renders on toggle).

    The row owns its own click + hover handling rather than leaning on
    QListWidget: a click anywhere outside the heart emits ``clicked``
    (the dialog selects the matching item), the heart button consumes
    its own clicks, and the empty outline heart only appears while the
    row is hovered so an un-pinned list stays visually calm. The filled
    heart on a pinned device shows always."""

    favorite_toggled = Signal(object, bool)  # CastDevice, is_favorite
    clicked = Signal()

    def __init__(self, dev: CastDevice, is_favorite: bool, parent=None):
        super().__init__(parent)
        self._dev = dev
        self._is_favorite = is_favorite
        self._hovered = False

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 8, 0)
        h.setSpacing(10)

        from modules.cast_dialog_sections import SECTION_LABELS

        # Label + glyph per actual protocol. The old code split binary
        # chromecast-vs-"AirPlay", which mislabelled every DLNA / Sonos /
        # Snapcast device as "AirPlay" (those sections were added after
        # this row was first written) — e.g. a DLNA renderer read
        # "192.168.x.x · AirPlay". Found during the 2026-05-28 GUI cast walk.
        kind = SECTION_LABELS.get(dev.device_type, (dev.device_type or "Cast").title())
        glyph_name = {"chromecast": "cast", "airplay": "airplay"}.get(dev.device_type, "cast")
        glyph = QLabel()
        glyph.setPixmap(icon(glyph_name).pixmap(QSize(18, 18)))
        glyph.setStyleSheet("background: transparent;")
        h.addWidget(glyph)

        name = QLabel(f"{dev.name}   ·   {kind}")
        name.setStyleSheet(f"color: {TEXT}; background: transparent;")
        h.addWidget(name, 1)

        self._heart = QPushButton()
        self._heart.setFixedSize(28, 28)
        self._heart.setIconSize(QSize(16, 16))
        self._heart.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._heart.setCursor(Qt.CursorShape.PointingHandCursor)
        self._heart.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none;
                          border-radius: 6px; }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
        """)
        self._heart.clicked.connect(self._toggle)
        h.addWidget(self._heart)
        self._update_heart_icon()

    def _update_heart_icon(self):
        # Filled accent heart when pinned (always visible); plain outline
        # only while hovered; otherwise no glyph at all. The button keeps
        # its fixed 28px slot regardless, so showing / hiding the icon
        # never shifts the rest of the row.
        if self._is_favorite:
            self._heart.setIcon(accent_icon("favorite_filled"))
        elif self._hovered:
            self._heart.setIcon(icon("favorite_outline"))
        else:
            self._heart.setIcon(QIcon())
        self._heart.setToolTip("Unpin from top" if self._is_favorite else "Pin to top")

    def _toggle(self):
        self._is_favorite = not self._is_favorite
        self._update_heart_icon()
        self.favorite_toggled.emit(self._dev, self._is_favorite)

    def _set_hovered(self, on: bool):
        if on == self._hovered:
            return
        self._hovered = on
        self._update_heart_icon()
        self.update()

    def enterEvent(self, e):
        self._set_hovered(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        # Moving the cursor onto the heart child fires the row's
        # leaveEvent even though the cursor is still within the row — so
        # confirm against the actual cursor position before clearing.
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._set_hovered(inside)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        # Accept so the row becomes the grab target and receives the
        # release. The heart button consumes its own presses, so this
        # only fires for clicks on the glyph / name / empty space.
        if e.button() == Qt.MouseButton.LeftButton:
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        # The row sits on top of the QListWidget item, so the item's
        # :selected background still shows through the transparent body.
        # Hover, though, needs the viewport's mouse tracking we no longer
        # get — so the row paints its own hover wash.
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*ink_rgb(), 13))
            p.drawRoundedRect(self.rect(), 6, 6)


class _CastSection(QWidget):
    """One section of the cast picker — a clickable header with a chevron
    glyph and a QListWidget body that hides when the section is collapsed.

    Sections are keyed by ``section_type`` (matches ``CastDevice
    .device_type`` for live types, plus the placeholder keys for the
    yet-unmerged DLNA/Sonos/Snapcast backends). The header click toggles
    collapsed state and emits ``toggled``; the body's ``QListWidget``
    selection forwards as ``selection_changed`` and ``item_activated``
    so the parent dialog can drive a single ``selected_device`` across
    all sections.
    """

    toggled = Signal(str, bool)  # section_type, collapsed
    selection_changed = Signal(object)  # CastDevice | None (None on clear)
    item_activated = Signal(object)  # CastDevice
    favorite_toggled = Signal(object, bool)  # CastDevice, is_fav

    HEADER_HEIGHT = 30

    def __init__(self, section_type: str, label: str, parent=None):
        super().__init__(parent)
        self.section_type = section_type
        self._label = label
        self._collapsed = True  # parent will resolve initial state
        self._devices: list = []
        self._favs: set = set()

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        self._header = self._build_header()
        col.addWidget(self._header)

        self._list = QListWidget()
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._list.setSpacing(0)
        self._list.setIconSize(QSize(18, 18))
        # No frame on the list — the section "owns" its visual region
        # and the QListWidget should disappear into that container.
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline-style: none;
                padding: 2px 0;
            }}
            QListWidget::item {{
                color: {TEXT};
                padding: 0;
                border-radius: 6px;
                margin: 1px 0;
            }}
            QListWidget::item:hover {{
                background: {ink_alpha(0.05)};
            }}
            QListWidget::item:selected {{
                background: {ink_alpha(0.10)};
                color: {TEXT};
            }}
        """)
        # Forward signals up to the dialog so it can run cross-section
        # mutual-exclusion + drive the Cast button enable state.
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemActivated.connect(self._on_item_activated)
        # Variable height: enough for the visible rows, no scrollbar
        # inside the list itself (the parent QScrollArea handles the
        # case where the dialog overflows).
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        col.addWidget(self._list)
        self._list.hide()

    # ── Header ─────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QFrame()
        w.setObjectName("jtCastSectionHeader")
        w.setFixedHeight(self.HEADER_HEIGHT)
        w.setCursor(Qt.CursorShape.PointingHandCursor)
        w.setStyleSheet(f"""
            QFrame#jtCastSectionHeader {{
                background: transparent;
                border-radius: 6px;
            }}
            QFrame#jtCastSectionHeader:hover {{
                background: {ink_alpha(0.05)};
            }}
        """)

        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 6, 0)
        h.setSpacing(6)

        self._chevron = QLabel()
        self._chevron.setFixedWidth(14)
        self._chevron.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._chevron.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        h.addWidget(self._chevron)

        self._name_label = QLabel(self._label)
        self._name_label.setFont(font(TYPE_MICRO))
        self._name_label.setStyleSheet(f"color: {TEXT}; background: transparent;")
        h.addWidget(self._name_label)

        h.addStretch(1)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} background: transparent;"
        )
        h.addWidget(self._count_label)

        # Whole-row click toggle — matches the spec ("click anywhere on
        # the row toggles"). Bound on the frame, not the chevron, so
        # the user can grab the entire 30px strip.
        w.mousePressEvent = self._on_header_press
        return w

    def _on_header_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.set_collapsed(not self._collapsed, emit=True)
            e.accept()

    def _refresh_header(self):
        # ▾ when expanded (content visible underneath), ▸ when collapsed
        # (content stowed away). Unicode arrows kept here — using the
        # icon() asset would force a recolour pass on theme change.
        self._chevron.setText("▸" if self._collapsed else "▾")
        n = len(self._devices)
        if n == 0:
            self._count_label.setText("none discovered")
        elif n == 1:
            self._count_label.setText("1 device")
        else:
            self._count_label.setText(f"{n} devices")

    # ── Public API ─────────────────────────────────────────────────────
    def set_devices(self, devices: list, favs: set):
        """Replace the section's device list. Preserves selection by
        UUID across re-renders so a freshly-arriving device doesn't
        deselect the user's current pick."""
        self._devices = list(devices)
        self._favs = favs

        prev_uuid = None
        items = self._list.selectedItems()
        if items:
            dev = items[0].data(Qt.ItemDataRole.UserRole)
            if dev is not None:
                prev_uuid = getattr(dev, "uuid", None)

        self._list.clear()
        # Hearted devices pinned to the top within this section.
        ordered = sorted(self._devices, key=lambda d: d.uuid not in favs)
        for dev in ordered:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dev)
            item.setSizeHint(QSize(0, 38))
            self._list.addItem(item)
            row = _CastDeviceRow(dev, dev.uuid in favs)
            row.favorite_toggled.connect(self.favorite_toggled.emit)
            row.clicked.connect(lambda it=item: self._list.setCurrentItem(it))
            self._list.setItemWidget(item, row)
            if prev_uuid and dev.uuid == prev_uuid:
                self._list.setCurrentItem(item)

        # Size the list to exactly fit its rows so the parent
        # QScrollArea governs overflow rather than a nested scroll
        # bar inside each section.
        self._list.setFixedHeight(max(0, len(ordered)) * 40 + 4)
        self._refresh_header()
        self._apply_visibility()

    def set_collapsed(self, collapsed: bool, *, emit: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._apply_visibility()
        self._refresh_header()
        if emit:
            self.toggled.emit(self.section_type, collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def has_devices(self) -> bool:
        return bool(self._devices)

    def list_widget(self) -> QListWidget:
        return self._list

    def clear_selection(self):
        self._list.clearSelection()

    def _apply_visibility(self):
        self._list.setVisible(not self._collapsed and bool(self._devices))

    # ── Signal forwarding ──────────────────────────────────────────────
    def _on_selection_changed(self):
        items = self._list.selectedItems()
        if not items:
            self.selection_changed.emit(None)
            return
        dev = items[0].data(Qt.ItemDataRole.UserRole)
        self.selection_changed.emit(dev)

    def _on_item_activated(self, item):
        dev = item.data(Qt.ItemDataRole.UserRole)
        if dev is not None:
            self.item_activated.emit(dev)


class CastDialog(QDialog):
    """Frameless frosted dialog matching the settings + main window. Auto-
    scans on open; devices appear live as discovery callbacks fire. The
    Rescan button is kept as a manual escape hatch but the user shouldn't
    need it for the common path."""

    BODY_RADIUS = 14
    # After this long with no devices, the "Scanning…" placeholder
    # flips to "No devices found" so the dialog doesn't sit in a
    # forever-loading state on networks with nothing castable.
    SCAN_GIVEUP_MS = 6000

    # Cross-thread bridge: pychromecast's get_chromecasts() and zeroconf's
    # ServiceBrowser fire their callbacks on plain Python threads with no
    # Qt event loop. Re-emitting through a signal hands off to the GUI
    # thread automatically (Qt::AutoConnection picks queued mode for
    # cross-thread connections), which a bare QTimer.singleShot can't do
    # because the timer would land in the worker thread that has no
    # event loop running.
    _devices_changed = Signal(list)

    def __init__(self, cast_manager: CastManager, parent=None):
        super().__init__(parent)
        self.cast_manager = cast_manager
        self.selected_device: CastDevice | None = None
        # Title must match the KWin noborder rule (see keep_above) so
        # the server-side decoration is stripped on KDE Wayland.
        from modules.keep_above import CAST_DIALOG_WINDOW_TITLE

        self.setWindowTitle(CAST_DIALOG_WINDOW_TITLE)
        self.setFixedSize(440, 480)
        # Mirror the settings dialog's window setup so the cast picker
        # draws the same way — frameless everywhere EXCEPT KDE Wayland,
        # where it stays a decorated Window stripped by the app-wide
        # KWin `noborder` rule. KWin drops the blur effect on
        # *undecorated* windows, so a plain FramelessWindowHint dialog
        # never gets frosted; the decorated + noborder route keeps it.
        from modules.platform_compat import is_kde_wayland

        _flags = Qt.WindowType.Window
        if not is_kde_wayland():
            _flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtCastDialog")
        # Non-modal — a modal exec() disables the parent window, which
        # Qt then paints in its dimmed/desaturated disabled palette.
        # The cast picker behaves like the (non-modal) Settings dialog:
        # the main window stays live and full-colour behind it.
        self.setModal(False)

        from modules.ui_helpers import GLOBAL_STYLE, body_color_tuple

        # Status-aware body: glass when blur is verified, near-opaque frosted
        # panel otherwise (never see-through). See ui_helpers.body_color_tuple.
        self._dialog_body_color = body_color_tuple("dialog")
        # GLOBAL_STYLE provides QListWidget/QPushButton baselines; we
        # override per-list and per-button below to keep the cast card
        # aesthetic consistent with the settings dialog.
        self.setStyleSheet(GLOBAL_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(20, 6, 20, 16)
        v.setSpacing(10)

        # Active-cast banner — visible only when a cast session is live.
        # Shows "Casting to {name}" + a Disconnect button that kills the
        # session. Hidden otherwise so the dialog reads as a picker.
        self._active_banner = self._build_active_banner()
        self._apply_banner_qss()
        v.addWidget(self._active_banner)

        v.addWidget(self._section_header("Available devices"))

        sub = QLabel("Pick a Chromecast, AirPlay, DLNA, Sonos, or Snapcast receiver.")
        sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # Scanning state — visible while we wait for the first device to
        # come back. Replaced by the section column as soon as one shows up.
        self._scanning_label = QLabel("Scanning your network…")
        self._scanning_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"background: {ink_alpha(0.04)};"
            "border-radius: 8px; padding: 14px 16px;"
        )
        self._scanning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._scanning_label)

        # ── Section column ────────────────────────────────────────────
        # One section per cast type. Section state (collapsed/expanded)
        # persists in QSettings per type — see modules.cast_dialog_sections.
        # A QScrollArea wraps the column so an unusually full network
        # (many Chromecasts + many AirPlays) can overflow gracefully
        # rather than blowing past the fixed dialog height.
        from modules.cast_dialog_sections import SECTION_LABELS, SECTION_TYPES

        self._sections: dict[str, _CastSection] = {}

        self._sections_scroll = QScrollArea()
        self._sections_scroll.setWidgetResizable(True)
        self._sections_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._sections_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._sections_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        sections_host = QWidget()
        sections_host.setStyleSheet("background: transparent;")
        sections_col = QVBoxLayout(sections_host)
        sections_col.setContentsMargins(0, 0, 0, 0)
        sections_col.setSpacing(4)

        for t in SECTION_TYPES:
            section = _CastSection(t, SECTION_LABELS[t], parent=sections_host)
            section.selection_changed.connect(
                lambda dev, src=t: self._on_section_selection_changed(src, dev)
            )
            section.item_activated.connect(self._on_section_item_activated)
            section.favorite_toggled.connect(self._on_favorite_toggled)
            section.toggled.connect(self._on_section_toggled)
            self._sections[t] = section
            sections_col.addWidget(section)

        sections_col.addStretch(1)
        self._sections_scroll.setWidget(sections_host)
        self._sections_scroll.hide()  # hidden until first device lands
        v.addWidget(self._sections_scroll, 1)

        # Bottom action row: Rescan on the left, Cancel + Cast on the
        # right. All three share a consistent transparent-default /
        # grey-box-on-hover language so the dialog reads as a calm
        # bottom strip rather than three differently-weighted controls.
        # Cast is distinguished by accent-colored text (and dims when
        # disabled), not by a different hover treatment.
        action_btn_css = f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
            QPushButton:pressed {{ background: {WASH_PRESSED}; }}
            QPushButton:disabled {{ color: {ink_alpha(0.30)}; }}
        """
        # Cast-button QSS is built from current accent — extracted into
        # _cast_btn_qss() so _reapply_accent can re-stamp it when the
        # user picks a new accent in Settings.
        cast_btn_css = self._cast_btn_qss()

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.scan_btn = QPushButton("Rescan")
        self.scan_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scan_btn.setStyleSheet(action_btn_css)
        self.scan_btn.clicked.connect(self.scan)
        btns.addWidget(self.scan_btn)
        # Forget paired device — only enabled when the selected list
        # item is an AirPlay 2 receiver with stored credentials. Clears
        # the credentials so the next cast attempt re-launches the
        # pairing dialog. Lives next to Rescan because both are "fix
        # the list" actions; Cancel / Cast are the dialog's primary
        # decision pair.
        self.forget_btn = QPushButton("Forget")
        self.forget_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.forget_btn.setStyleSheet(action_btn_css)
        self.forget_btn.setEnabled(False)
        self.forget_btn.setToolTip(
            "Clear stored pairing credentials for the selected "
            "AirPlay 2 device so it can be re-paired."
        )
        self.forget_btn.clicked.connect(self._on_forget_clicked)
        btns.addWidget(self.forget_btn)
        btns.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel.setStyleSheet(action_btn_css)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)

        self.cast_btn = QPushButton("Cast")
        self.cast_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cast_btn.setStyleSheet(cast_btn_css)
        self.cast_btn.setEnabled(False)
        self.cast_btn.clicked.connect(self.accept)
        btns.addWidget(self.cast_btn)
        v.addLayout(btns)

        outer.addWidget(body, 1)

        # Section signals were wired up when each _CastSection was built.
        # Live updates as devices are discovered — saves the user from
        # having to click rescan + wait. The callback fires on the
        # discovery thread; emitting our signal there hands off to the
        # GUI thread (queued connection) before _render_devices runs.
        self._devices_changed.connect(self._render_devices)
        self.cast_manager.set_devices_callback(self._devices_changed.emit)
        # Pull whatever's already in the cache, then start a fresh
        # discovery so the list stays current. Banner reflects current
        # active_cast immediately so the user can disconnect without
        # waiting for the discovery callback.
        # Scan-give-up timer — flips the scanning label to the
        # "No devices found" empty state if nothing has landed by
        # SCAN_GIVEUP_MS. Reset on every scan() / kept off when
        # devices arrive.
        self._scan_giveup_timer = QTimer(self)
        self._scan_giveup_timer.setSingleShot(True)
        self._scan_giveup_timer.setInterval(self.SCAN_GIVEUP_MS)
        self._scan_giveup_timer.timeout.connect(self._on_scan_giveup)

        self._render_devices(self.cast_manager.get_all_devices())
        self._refresh_active_banner()
        self.scan()

        # Live-accent: rebuild the banner stylesheet + restamp the
        # Cast button color when the user picks a new accent. Both
        # bake the accent at construction; without this they'd freeze
        # at whatever was active when the dialog opened.
        # UniqueConnection: CastDialog is constructed every time the
        # user opens cast; without idempotency the connection count
        # grows per session and _reapply_accent fires N+1 times.
        PlayerBus.get().theme_changed.connect(
            self._reapply_accent, Qt.ConnectionType.UniqueConnection
        )

    # ── Title bar ──────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtCastTitle")
        tb.setStyleSheet("""
            QWidget#jtCastTitle { background: transparent; }
            QWidget#jtCastTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        cast_glyph = QLabel()
        cast_glyph.setPixmap(icon("cast").pixmap(QSize(18, 18)))
        h.addWidget(cast_glyph)

        title = QLabel("Cast to device")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        h.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; border-radius: 6px; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; color: {TEXT}; }}
        """)
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def keyPressEvent(self, e):
        # Esc dismisses the picker. QDialog binds this by default, but
        # the frameless + WA_TranslucentBackground combo on KDE Wayland
        # doesn't reliably route the key event to QDialog's handler.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        # Compositor blur once the surface is mapped — frosted themes
        # only; matches the settings dialog / mini player.
        QTimer.singleShot(0, self._apply_blur)
        # Park focus on the first section that has devices so Down/Enter
        # just work. If everything is empty (still scanning), the focus
        # steer happens once _render_devices populates a section.
        self._focus_first_populated_section()

    def _apply_blur(self):
        """Blur behind the cast dialog when the active theme is frosted
        — draws it the same way as the settings dialog and mini player.
        Silent no-op where the compositor has no blur support."""
        from modules import blur
        from modules.theme import get_active_theme

        blur.apply(self, get_active_theme().blur, corner_radius=self.BODY_RADIUS)

    def _focus_first_populated_section(self):
        for section in self._sections.values():
            if section.has_devices() and not section.is_collapsed():
                section.list_widget().setFocus(Qt.FocusReason.OtherFocusReason)
                return

    def _on_section_item_activated(self, _dev):
        # Section's QListWidget.itemActivated forwarded — Return/Enter
        # or double-click. _on_section_selection_changed has already set
        # selected_device + enabled cast_btn, so this is "press Cast".
        if self.selected_device is not None:
            self.accept()

    def _section_header(self, text: str) -> QLabel:
        # font(TYPE_MICRO) handles uppercase + letter-spacing via QFont,
        # so we pass mixed-case text here — Qt's QSS doesn't actually
        # honor text-transform/letter-spacing, only QFont does.
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    # ── Device discovery ───────────────────────────────────────────────
    def _any_devices_loaded(self) -> bool:
        return any(s.has_devices() for s in self._sections.values())

    def scan(self):
        # Show the scanning placeholder if nothing is rendered yet — if
        # we already have devices from a previous scan, leave them
        # visible while a fresh discovery runs in the background.
        if not self._any_devices_loaded():
            self._scanning_label.setText("Scanning your network…")
            self._scanning_label.show()
            self._sections_scroll.hide()
            self._scan_giveup_timer.start()
        self.cast_manager.discover_all()

    def _render_devices(self, devices: List[CastDevice]):
        from modules.cast_dialog_sections import (
            SECTION_TYPES,
            group_devices_by_type,
            resolve_state,
        )
        from modules.settings import get_settings

        was_visible = self._sections_scroll.isVisible()
        had_devices_before = self._any_devices_loaded()

        favs = set(get_settings().favorite_cast_device_ids)
        buckets = group_devices_by_type(devices)

        s_settings = get_settings()._s  # underlying QSettings
        for t in SECTION_TYPES:
            section = self._sections[t]
            bucket = buckets.get(t, [])
            # Push devices first so resolve_state's has_devices is fresh.
            section.set_devices(bucket, favs)
            state = resolve_state(s_settings, t, has_devices=bool(bucket))
            # Apply without re-emitting to avoid persisting the default
            # back as an "explicit" choice on every render — the user
            # hasn't toggled anything yet.
            section.set_collapsed(state.collapsed, emit=False)

        if not self._any_devices_loaded():
            # Leave the label alone — it's either "Scanning…" (in
            # progress) or "No devices found" (give-up timer fired).
            # Clearing the sections still matters because devices may
            # have been REMOVED from the cache.
            self._sections_scroll.hide()
            return

        # Devices arrived — stop the give-up timer so the empty
        # state doesn't flip in over a now-populated list.
        self._scan_giveup_timer.stop()
        self._scanning_label.hide()
        self._sections_scroll.show()
        # First device just landed while the dialog is open — steer
        # keyboard focus into the first populated section so Down/Enter
        # immediately drives it.
        if (not was_visible or not had_devices_before) and self.isVisible():
            self._focus_first_populated_section()
        # Banner state can change as devices come and go (active_cast
        # may have just been discovered with full metadata).
        self._refresh_active_banner()

    def _on_favorite_toggled(self, dev: CastDevice, is_fav: bool):
        """Heart toggled on a device row — persist the change (name +
        type alongside the uuid, so the cast button's right-click menu
        can label it later) and re-render so the device jumps to or
        leaves the pinned group."""
        from modules.settings import get_settings

        s = get_settings()
        favs = [f for f in s.favorite_cast_devices if f["uuid"] != dev.uuid]
        if is_fav:
            favs.append(
                {
                    "uuid": dev.uuid,
                    "name": dev.name,
                    "type": dev.device_type,
                }
            )
        s.favorite_cast_devices = favs
        self._render_devices(self.cast_manager.get_all_devices())

    def _on_section_toggled(self, section_type: str, collapsed: bool):
        """User clicked a section header. Persist the new state so the
        next dialog open honours their choice."""
        from modules.cast_dialog_sections import write_collapsed
        from modules.settings import get_settings

        write_collapsed(get_settings()._s, section_type, collapsed)

    @Slot()
    def _on_scan_giveup(self):
        """SCAN_GIVEUP_MS elapsed without any device showing up — flip
        the scanning placeholder to a 'No devices found' empty state
        so the dialog reads as 'done scanning, network empty' instead
        of 'forever loading'."""
        if self._any_devices_loaded():
            return
        self._scanning_label.setText(
            "No devices found on your network.\nTry Rescan, or check that your devices are awake."
        )
        self._scanning_label.show()
        self._sections_scroll.hide()

    # ── Active-cast banner ─────────────────────────────────────────────
    def _build_active_banner(self) -> QWidget:
        w = QFrame()
        w.setObjectName("castActiveBanner")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 10, 8, 10)
        h.setSpacing(10)

        text_wrap = QVBoxLayout()
        text_wrap.setContentsMargins(0, 0, 0, 0)
        text_wrap.setSpacing(1)
        # Mixed-case text + font(TYPE_MICRO) — QFont applies the uppercase
        # transform and letter-spacing that QSS would silently ignore.
        kicker = QLabel("Casting to")
        kicker.setFont(font(TYPE_MICRO))
        kicker.setStyleSheet(f"color: {TEXT_FAINT};")
        text_wrap.addWidget(kicker)
        self._active_label = QLabel("")
        self._active_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        text_wrap.addWidget(self._active_label)
        h.addLayout(text_wrap, 1)

        # Explicit outline — the bare "ghost" object-name styling left
        # the button floating with no edge against the accent-tinted
        # banner. A 1px border gives it a clear hit target.
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._disconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._disconnect_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {ink_alpha(0.28)};
                border-radius: 7px;
                padding: 5px 14px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{
                background: {WASH_HOVER};
                border-color: {ink_alpha(0.45)};
            }}
            QPushButton:pressed {{ background: {WASH_PRESSED}; }}
        """)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        h.addWidget(self._disconnect_btn)

        w.hide()
        return w

    def _refresh_active_banner(self):
        active = self.cast_manager.active_cast
        if active is None:
            self._active_banner.hide()
            return
        # Label by the device's actual protocol — the old hardcoded
        # Chromecast/AirPlay ternary mislabelled DLNA / Sonos / Snapcast
        # devices as "AirPlay". Reuse the dialog's SECTION_LABELS map
        # (same default-casing as the row-label helper at _row_kind).
        # device_type is a CastType (str-backed), so the string-keyed
        # SECTION_LABELS lookup resolves correctly.
        from modules.cast_dialog_sections import SECTION_LABELS

        kind = SECTION_LABELS.get(active.device_type, (active.device_type or "Cast").title())
        self._active_label.setText(f"{active.name}   ·   {kind}")
        self._active_banner.show()

    def _apply_banner_qss(self):
        """Apply the active-cast banner stylesheet from the CURRENT
        accent — split out so _reapply_accent can re-stamp it on
        theme_changed without rebuilding the whole banner widget."""
        from modules.theme import _hex_to_rgb as _hr
        from modules.theme import get_active_theme as _gt

        _ar, _ag, _ab = _hr(_gt().accent)
        self._active_banner.setStyleSheet(f"""
            QFrame#castActiveBanner {{
                background: rgba({_ar},{_ag},{_ab},0.14);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.25);
                border-radius: 8px;
            }}
        """)

    def _cast_btn_qss(self) -> str:
        """QSS for the primary Cast action button — accent-coloured
        text, transparent body. Re-callable so _reapply_accent can
        push a fresh stylesheet when the user picks a new accent."""
        from modules.ui_helpers import ACCENT as _ACCENT

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {_ACCENT};
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
            QPushButton:pressed {{ background: {WASH_PRESSED}; }}
            QPushButton:disabled {{ color: {ink_alpha(0.30)}; }}
        """

    def _reapply_accent(self):
        """Re-stamp every surface whose stylesheet baked the accent at
        construction. Wired to PlayerBus.theme_changed in __init__."""
        self._apply_banner_qss()
        if hasattr(self, "cast_btn"):
            self.cast_btn.setStyleSheet(self._cast_btn_qss())
        # Refresh the cached body fill + repaint — the body opacity differs
        # across theme modes AND with the verified blur status, and
        # paintEvent reads the cached copy rather than the live token.
        from modules.ui_helpers import body_color_tuple

        self._dialog_body_color = body_color_tuple("dialog")
        self.update()
        # Frosted blurs behind the dialog; Transparent / Solid don't.
        self._apply_blur()

    def _on_disconnect(self):
        # stop_cast() handles both branches (chromecast.quit_app() +
        # mc.stop(), or AirPlay POST /stop) and clears active_cast.
        self.cast_manager.stop_cast()
        # Tell the rest of the app the cast session ended so the
        # NowPlayingBar / mini player can drop any cast indicators.
        try:
            from modules.player_state import PlayerBus

            PlayerBus.get().cast_stopped.emit()
        except Exception:
            pass
        self._refresh_active_banner()
        # Disconnecting is a terminal action — close the picker rather
        # than leaving the user on a now-stale dialog. reject() (not
        # accept()) so _open_cast_dialog doesn't treat it as a cast.
        self.reject()

    def _on_section_selection_changed(self, source_type: str, dev):
        """A section's QListWidget reported a selection change. Drive
        mutual exclusion (only one row highlighted across all sections)
        + the Cast/Forget button enable state from this one path."""
        if dev is None:
            # The forwarding section just cleared its own selection —
            # only disable global state if NO section currently holds
            # a selection, otherwise we'd race the cross-section
            # clear we're about to trigger.
            for t, sec in self._sections.items():
                if t == source_type:
                    continue
                if sec.list_widget().selectedItems():
                    return
            self.selected_device = None
            self.cast_btn.setEnabled(False)
            self.forget_btn.setEnabled(False)
            return

        # New selection — clear every other section so the user sees
        # exactly one highlighted row across the whole dialog.
        for t, sec in self._sections.items():
            if t != source_type:
                # Block signals so our cross-section clear doesn't
                # re-enter _on_section_selection_changed with dev=None.
                sec.list_widget().blockSignals(True)
                sec.clear_selection()
                sec.list_widget().blockSignals(False)

        self.selected_device = dev
        self.cast_btn.setEnabled(True)
        # Enable Forget only for AirPlay 2 receivers that have stored
        # credentials. Chromecasts and AirPlay 1 devices don't pair, so
        # Forget would be a no-op for them.
        forget_eligible = False
        try:
            from modules import airplay2 as _ap2

            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                if _ap2.get_stored_credentials(ap2_dev.identifier):
                    forget_eligible = True
        except Exception:
            pass
        self.forget_btn.setEnabled(forget_eligible)

    def _on_forget_clicked(self):
        dev = self.selected_device
        if dev is None:
            return
        try:
            from modules import airplay2 as _ap2

            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                _ap2.forget_credentials(ap2_dev.identifier)
                # Reflect immediately — the button should grey out
                # since the credentials we were storing are gone.
                self.forget_btn.setEnabled(False)
        except Exception as e:
            logger.warning("CastDialog forget_credentials failed: %s", e)

    def paintEvent(self, e):
        # Rounded card body, matching the settings dialog. The custom
        # titlebar is part of the same surface, so the rounded rect
        # spans the full window.
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            p.setBrush(QColor(*self._dialog_body_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
