"""
jellytoast settings dialog. Frameless + frosted to match the main window
and mini player. Sidebar nav on the left, page content on the right.

Sections:
- General  — startup destination, window/tray behavior
- Account  — server URL, sign-out
- Appearance — theme mode (placeholders for light/transparent)
- Display  — font + UI scaling (placeholders)
- About    — version + description

Settings that need the host window to react (sign-out, server change)
are emitted as signals; the host listens and acts.
"""

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QFormLayout,
    QComboBox,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QApplication,
    QLineEdit,
    QSlider,
    QInputDialog,
    QMessageBox,
    QFrame,
)


class _AccentDelegate(QStyledItemDelegate):
    """Paint dropdown items ourselves so the selected / current item
    reads as our accent purple, not Qt's default Fusion blue.

    Why custom paint and not QSS / palette: Qt 6's style painter
    (Fusion on KDE Wayland) draws the focus rectangle and selection
    fill on QListView items by calling into ``QStyle`` with palette
    roles. Setting ``QPalette.Highlight`` on the view OR on the app
    didn't take effect — the delegate-side paint kept reading the
    system blue. Same for ``selection-background-color`` in QSS: Qt
    ignores it for the focus / current-item indicator. The only
    reliable override is to subclass the delegate and paint the item
    backgrounds ourselves, stripping the State_HasFocus /
    State_Selected flags before delegating to ``super().paint`` so
    Qt's painter contributes only the text glyph.
    """

    def __init__(self, accent_rgb: tuple[int, int, int], parent=None):
        super().__init__(parent)
        ar, ag, ab = accent_rgb
        # Subtle accent wash for both selection and hover — selected
        # is just enough stronger than hover to read as "this is the
        # active value" without becoming a loud purple block. Keep
        # both states well below 50% alpha so dark text on top stays
        # legible.
        self._sel_fill = QColor(ar, ag, ab)
        self._sel_fill.setAlphaF(0.28)
        self._hover_fill = QColor(ar, ag, ab)
        self._hover_fill.setAlphaF(0.14)
        self._bg_fill = QColor(20, 22, 26)
        # Same text colour in all three states (idle / hover /
        # selected) so the row doesn't strobe between white and off-
        # white as the user keyboard-navigates the dropdown.
        self._text_color = QColor(0xEE, 0xEE, 0xEE)
        # Margins / radius match the QSS rule for non-delegated items
        # (`margin: 1px 4px; border-radius: 4px`) so the highlight
        # capsule reads as the same shape regardless of which paint
        # path Qt took.
        self._h_inset = 4
        self._v_inset = 1
        self._radius = 4

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        # Strip flags Qt's style painter would otherwise use to draw
        # the focus rectangle / selection fill on top of our paint.
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        opt.state &= ~QStyle.StateFlag.State_MouseOver
        # Re-colour the text via the option's palette so super().paint
        # draws the glyph in our text colour (otherwise selected items
        # would still try to use HighlightedText, etc.).
        opt.palette.setColor(QPalette.ColorRole.Text, self._text_color)
        opt.palette.setColor(QPalette.ColorRole.WindowText, self._text_color)
        opt.palette.setColor(QPalette.ColorRole.HighlightedText, self._text_color)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Solid row background first so the popup reads fully opaque
        # even where the highlight capsule doesn't cover.
        painter.fillRect(opt.rect, self._bg_fill)
        # Highlight capsule (selection takes precedence over hover).
        if is_selected or is_hovered:
            painter.setBrush(self._sel_fill if is_selected else self._hover_fill)
            painter.setPen(Qt.PenStyle.NoPen)
            capsule = opt.rect.adjusted(
                self._h_inset,
                self._v_inset,
                -self._h_inset,
                -self._v_inset,
            )
            painter.drawRoundedRect(capsule, self._radius, self._radius)
        painter.restore()
        # Let the base class paint the text on top — with the state
        # flags stripped so it doesn't redraw a Qt-style highlight.
        super().paint(painter, opt, index)


class _OpaqueComboBox(QComboBox):
    """QComboBox whose popup container is forced opaque.

    The settings dialog uses ``WA_TranslucentBackground`` so its rounded
    body can be painted with anti-aliased corners. On KDE Wayland the
    combobox popup window inherits that attribute when Qt creates the
    container, and Qt 6 doesn't reliably honour a later toggle because
    the QWindow surface was already created ARGB. The result: ghost
    text from the dialog body bleeds through the dropdown.

    The fix is layered (same defence-in-depth pattern as
    ``ui_helpers._harden_popup_opacity``):
    - Toggle ``WA_TranslucentBackground`` off and hide/show to give Qt
      a chance to recreate the surface as opaque.
    - Force ``autoFillBackground=True`` + opaque palette ``Window`` /
      ``Base`` so even if the surface stays ARGB the autofill writes
      alpha=255 across the popup rect before the view paints.
    - QSS on the view paints the visual treatment on top of those
      filled pixels.

    First ``showPopup`` runs the fixup; subsequent opens hit the cached
    opaque popup with no flicker.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_opaque = False

    def showPopup(self):
        super().showPopup()
        if self._popup_opaque:
            return
        view = self.view()
        popup = view.window() if view is not None else None
        if popup is None or popup is self.window():
            self._popup_opaque = True
            return
        from modules.ui_helpers import _harden_popup_opacity
        from modules.theme import _hex_to_rgb as _h2r

        # Layered fix:
        # 1. Hide + harden attrs/palette/autoFill (covers the surface).
        # 2. Apply the QSS DIRECTLY to the popup wrapper AND the inner
        #    view — Qt 6 on Wayland does not reliably cascade
        #    `QComboBox QAbstractItemView` rules from the host dialog
        #    to a popup-class top-level QWindow, so the items were
        #    falling back to Qt's default selection highlight (blue)
        #    and inheriting the translucent backing. Setting the rules
        #    on the popup widgets themselves bypasses the cascade.
        was_translucent = popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if was_translucent:
            popup.hide()
        _harden_popup_opacity(popup)
        if view is not None and view is not popup:
            _harden_popup_opacity(view)

        ar, ag, ab = _h2r(ACCENT)
        popup.setStyleSheet(f"""
            QFrame {{
                background: rgb(20, 22, 26);
                border: 1px solid {BORDER};
                border-radius: 8px;
            }}
        """)
        if view is not None:
            # Palette (defensive — some Qt paint paths still consult it).
            view_pal = view.palette()
            view_pal.setColor(QPalette.ColorRole.Highlight, QColor(ar, ag, ab))
            view_pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
            view.setPalette(view_pal)
            # Install custom delegate — the load-bearing fix. Neither
            # QSS nor palette reliably override Qt 6's style painter
            # for the focus / current-item rectangle on KDE Wayland,
            # so we paint the item background ourselves and strip
            # Qt's selection flags before delegating to super().
            view.setItemDelegate(_AccentDelegate((ar, ag, ab), view))
            # Stylesheet still useful for view-level rules (frame,
            # padding) the delegate doesn't touch.
            view.setStyleSheet(f"""
                QAbstractItemView {{
                    background: rgb(20, 22, 26);
                    color: {TEXT};
                    border: none;
                    outline: 0;
                    padding: 4px 0;
                }}
            """)
        self._popup_opaque = True
        if was_translucent:
            super().showPopup()


from modules.icons import icon
from modules.ui_helpers import (
    BORDER,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    DIALOG_BODY_COLOR,
    ACCENT,
    ERROR_FG,
)
from modules.theme import _hex_to_rgb
from modules.design_tokens import (
    TYPE_TITLE,
    TYPE_SUBHEAD,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    font,
    type_qss,
)
from modules.player_state import PlayerBus
from modules.settings import get_settings
from modules.theme import THEMES as _THEME_REGISTRY
from modules.keep_above import (
    install_mini_player_rule,
    remove_mini_player_rule,
    is_supported as keep_above_supported,
)
from modules import autostart as _autostart


# Native music surfaces the top-bar Home button can route to. Mirrors
# the keys consumed by JellytoastWindow._route_home. The same setting
# also drives the launch landing — jellytoast always boots into the
# user's chosen Home surface.
HOME_DESTINATIONS = [
    ("Albums", "albums"),
    ("Playlists", "playlists"),
    ("Artists", "artists"),
    ("Songs", "songs"),
    ("Genres", "genres"),
    ("Suggestions", "suggestions"),
]

# Themes the user can pick from. Entries flagged `enabled=False` show
# up in the dropdown but can't be selected — placeholder slots for
# palettes we haven't shipped yet.
_THEME_CHOICES = [
    (_THEME_REGISTRY["frosted_dark"].label, "frosted_dark", True),
    (_THEME_REGISTRY["dark"].label, "dark", True),
    (_THEME_REGISTRY["transparent"].label, "transparent", True),
    ("Light (coming soon)", "light", False),
]

LYRICS_FONT_SIZES = [
    ("Smaller", "small"),
    ("Default", "default"),
    ("Larger", "large"),
    ("Largest", "largest"),
]

# (visible label, mpv "replaygain" property value)
REPLAYGAIN_MODES = [
    ("Off", "no"),
    ("Track (per song)", "track"),
    ("Album (preserve relative loudness)", "album"),
]

# (visible label, audio_quality setting value)
# "original" maps to DirectStream (Jellyfin) / format=raw (Subsonic).
# Numeric values are kbps and trigger server-side transcode.
AUDIO_QUALITIES = [
    ("Original (no transcode)", "original"),
    ("320 kbps", "320"),
    ("256 kbps", "256"),
    ("192 kbps", "192"),
    ("128 kbps", "128"),
    ("96 kbps", "96"),
]

# (visible label, cast_stream_routing setting value)
# Controls how a cast device reaches the media stream. "auto" casts
# direct when the server is a private LAN IP and relays through this
# machine otherwise (Tailscale / remote / self-signed host).
CAST_ROUTING_MODES = [
    ("Auto (direct on LAN, relay otherwise)", "auto"),
    ("Always relay through this device", "proxy"),
    ("Always direct (no relay)", "direct"),
]


class SettingsDialog(QDialog):
    BODY_RADIUS = 14

    sign_out_requested = Signal()
    server_change_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.s = get_settings()
        self.setWindowTitle("jellytoast Settings")
        self.setFixedSize(820, 540)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtSettingsDialog")
        # WindowModal (not ApplicationModal) — blocks the parent main
        # window only, so the mini player (a separate top-level) stays
        # draggable and clickable while Settings is open. exec() honours
        # whatever modality we set here.
        self.setWindowModality(Qt.WindowModality.WindowModal)

        # Dialog-level styling for QComboBox + its popup. The dialog
        # uses WA_TranslucentBackground for the rounded card look, and
        # without explicit popup styling the Theme combo / ReplayGain
        # combo / etc. inherit that translucency — the popup ghosts
        # over the field below it. An opaque background and a clear
        # down-arrow icon (Qt's default arrow renders invisible against
        # the dark surface here) fix both issues for every combo in
        # the dialog without per-combo styling.
        self._build_and_apply_dialog_stylesheet()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_h = QHBoxLayout(body)
        body_h.setContentsMargins(10, 0, 14, 14)
        body_h.setSpacing(2)

        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline-style: none;
                padding: 4px;
            }}
            QListWidget::item {{
                color: {TEXT_DIM};
                padding: 9px 14px;
                border-radius: 8px;
                margin: 2px 0;
            }}
            QListWidget::item:hover {{
                background: rgba(255,255,255,0.05);
                color: {TEXT};
            }}
            QListWidget::item:selected {{
                background: rgba(255,255,255,0.10);
                color: {TEXT};
            }}
        """)
        body_h.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        body_h.addWidget(self.stack, 1)

        outer.addWidget(body, 1)

        # Account is now folded into General as a Server section at
        # the top; About moved to the titlebar info button. Hotkeys and
        # Scrobbling are new pages — Hotkeys is read-only for now,
        # Scrobbling is a placeholder for upcoming Last.fm /
        # ListenBrainz integration.
        self._add_page("General", self._build_general())
        self._add_page("Playback", self._build_playback())
        # Casting got its own page 2026-05-16 (was nested under Playback).
        # Hosts per-protocol toggles, discovery timing, stream routing.
        self._add_page("Casting", self._build_casting())
        self._add_page("Library", self._build_library())
        # Downloads manages explicitly-downloaded music; it expands to
        # fill the page (its list scrolls) rather than sitting form-
        # sized at the top like the others.
        self._add_page("Downloads", self._build_downloads(), expand=True)
        # Display rolls in what was previously Appearance + Lyrics —
        # all three pages controlled how the UI looks, so they live
        # under one nav entry now.
        self._add_page("Display", self._build_display())
        self._add_page("Hotkeys", self._build_hotkeys())
        self._add_page("Scrobbling", self._build_scrobbling())
        # Debug / power-user color editor — lives last in the nav so
        # the day-to-day pages don't have to scroll past it.
        self._add_page("Colors", self._build_colors(), expand=True)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def _add_page(self, title: str, content: QWidget, expand: bool = False):
        QListWidgetItem(title, self.nav)
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        v = QVBoxLayout(wrap)
        v.setContentsMargins(20, 14, 20, 14)
        v.setSpacing(0)
        if expand:
            # The page owns its own vertical space (e.g. a scrolling
            # list) — let it fill instead of pinning it to the top.
            v.addWidget(content, 1)
        else:
            v.addWidget(content)
            v.addStretch(1)
        self.stack.addWidget(wrap)

    def _build_downloads(self) -> QWidget:
        """The Downloads page — the offline-downloads management screen.
        Lazy import keeps the offline package (and its SQLite handle)
        off the settings dialog's construction path until this page is
        actually built."""
        from modules.downloads_view import DownloadsView

        return DownloadsView()

    # ── Title bar ──────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtSettingsTitle")
        tb.setStyleSheet("""
            QWidget#jtSettingsTitle { background: transparent; }
            QWidget#jtSettingsTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        cog = QLabel()
        cog.setPixmap(icon("settings").pixmap(QSize(18, 18)))
        h.addWidget(cog)

        title = QLabel("Settings")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        h.addStretch(1)

        # About button (info circle) — opens a small overlay with the
        # name + version + blurb. Replaces the prior About settings page.
        about_btn = QPushButton()
        about_btn.setIcon(icon("info"))
        about_btn.setIconSize(QSize(18, 18))
        about_btn.setFixedSize(32, 28)
        about_btn.setToolTip("About jellytoast")
        about_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        about_btn.setStyleSheet("""
            QPushButton { background: transparent; border: none; }
            QPushButton:hover { background: rgba(255,255,255,0.08); border-radius: 6px; }
        """)
        about_btn.clicked.connect(self._show_about)
        h.addWidget(about_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: rgba(239,68,68,0.85); color: white; }}
        """)
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        # Make titlebar draggable via KWin (matches the main window).
        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    # ── Page: General ──────────────────────────────────────────────────
    def _build_general(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        # 12 px between groups — matches the rhythm we use on every
        # other settings page (Playback, Library, Display, Hotkeys,
        # Scrobbling) so the dialog reads as one consistent surface.
        v.setSpacing(12)

        # ── Server (folded in from the old Account page) ───────────────
        # Most surveyed players (Supersonic, Apple Music) treat server
        # / account as a row in General rather than its own peer page.
        # No section header — the prominent "Signed in to …" line at the
        # top already labels what this group is.
        username = self.s.username
        signed_in = bool(self.s.access_token)
        provider_kind = (self.s.provider_kind or "jellyfin").lower()
        provider_display = "Navidrome" if provider_kind == "subsonic" else "Jellyfin"
        if signed_in:
            if username:
                status_text = f"Signed in to {provider_display} as {username}."
            else:
                status_text = f"Signed in to {provider_display}."
        else:
            status_text = "Not signed in."
        status = QLabel(status_text)
        status.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        v.addWidget(status)

        url = self.s.server_url or "Not configured"
        url_label = QLabel(url)
        url_label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} padding-top: 2px;")
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_label.setWordWrap(True)
        v.addWidget(url_label)

        # Live connection status — probe the server on dialog open
        # so the user gets immediate feedback about whether the
        # stored URL is still reachable. Useful after a server move
        # or network change where the persisted access_token might
        # be present but the server itself is gone.
        self._conn_status_label = QLabel("Connection: checking…")
        self._conn_status_label.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(self._conn_status_label)
        if signed_in and self.s.server_url:
            from modules.async_io import run_async
            from modules.providers import get_provider as _gp

            _api = _gp()
            run_async(
                _api.probe,
                self.s.server_url,
                on_result=lambda info: self._on_connection_probe(info),
                on_error=lambda _e: self._on_connection_probe(None),
            )
        else:
            self._conn_status_label.setText("Connection: not signed in")

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(8)
        change_btn = QPushButton("Change server URL…")
        change_btn.setObjectName("ghost")
        change_btn.clicked.connect(self.server_change_requested.emit)
        btn_row.addWidget(change_btn)
        signout_btn = QPushButton("Sign out")
        signout_btn.setObjectName("ghost")
        signout_btn.clicked.connect(self.sign_out_requested.emit)
        btn_row.addWidget(signout_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        v.addSpacing(18)

        # Checkboxes grouped — these are the on/off behavior
        # toggles. Section headers ("Startup" / "Window") were removed
        # in favor of one flat group, since the visual chunking of
        # related checkboxes already reads as a unit.

        # Disk truth (whether the autostart .desktop file exists) wins
        # over the persisted flag — they can drift if the user nukes
        # the file from a file manager.
        self._autostart_check = QCheckBox("Launch jellytoast at login")
        self._autostart_check.setChecked(_autostart.is_enabled())
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        v.addWidget(self._autostart_check)

        self._tray_check = QCheckBox("Hide to system tray when window is closed")
        self._tray_check.setChecked(self.s.minimize_to_tray)
        self._tray_check.toggled.connect(lambda val: setattr(self.s, "minimize_to_tray", val))
        v.addWidget(self._tray_check)

        self._mini_check = QCheckBox("Show mini player on startup")
        self._mini_check.setChecked(self.s.show_mini_on_start)
        self._mini_check.toggled.connect(lambda val: setattr(self.s, "show_mini_on_start", val))
        v.addWidget(self._mini_check)

        # Wayland-only: xdg-shell forbids apps from setting their own
        # stacking, so Qt.WindowStaysOnTopHint is a no-op there. We
        # install a KWin window rule to do it compositor-side. Show the
        # toggle only on KDE Wayland — outside that, the X11 hint
        # already works and there's nothing to expose.
        if keep_above_supported():
            self._keep_above_check = QCheckBox("Keep mini player on top (KDE Wayland)")
            self._keep_above_check.setChecked(self.s.mini_player_keep_above)
            self._keep_above_check.toggled.connect(self._on_keep_above_toggled)
            v.addWidget(self._keep_above_check)

        v.addSpacing(18)

        # Home destination at the bottom — the only form-style row on
        # this page, separated from the checkbox stack.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._home_combo = _OpaqueComboBox()
        for label, key in HOME_DESTINATIONS:
            self._home_combo.addItem(label, key)
        self._select_combo_by_data(self._home_combo, self.s.home_destination)
        self._home_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s, "home_destination", self._home_combo.currentData() or "albums"
            )
        )
        form.addRow(
            self._field_label("Home button & launch open:"),
            self._home_combo,
        )
        v.addLayout(form)
        v.addStretch(1)
        return page

    def _on_connection_probe(self, info):
        """Update the connection-status label after the probe lands.
        info is the dict provider.probe returned on success, or None
        on any failure (network error, wrong port, non-backend
        response). The label may have been destroyed if the user
        closed the dialog before the probe came back."""
        label = getattr(self, "_conn_status_label", None)
        if label is None:
            return
        try:
            if info:
                label.setText("Connection: reachable")
                label.setStyleSheet(
                    f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
                )
            else:
                label.setText("Connection: unreachable — check the URL or your network")
                label.setStyleSheet(f"color: {ERROR_FG}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        except RuntimeError:
            # QLabel was destroyed (dialog closed) — drop silently.
            pass

    def _on_keep_above_toggled(self, on: bool):
        # Persist first, then apply — if the rule write fails we still
        # remember the user's intent for the next launch / retry.
        self.s.mini_player_keep_above = on
        if on:
            install_mini_player_rule()
        else:
            remove_mini_player_rule()

    def _on_autostart_toggled(self, on: bool):
        # Persist user intent, then mutate the filesystem. If the
        # filesystem op fails (e.g. read-only home), the QSettings flag
        # still records what the user asked for so we can retry next
        # time the dialog opens.
        self.s.autostart = on
        ok = _autostart.enable() if on else _autostart.disable()
        # If reality drifted (e.g. enable failed), reflect that in the
        # checkbox without re-firing the toggled signal.
        if on and not ok and not _autostart.is_enabled():
            self._autostart_check.blockSignals(True)
            self._autostart_check.setChecked(False)
            self._autostart_check.blockSignals(False)

    # ── Page: Playback ─────────────────────────────────────────────────
    def _build_playback(self) -> QWidget:
        # Trimmed 2026-05-11 — section headers ("Streaming",
        # "ReplayGain", "Behavior") and the long descriptive paragraphs
        # were removed in favour of a single tidy form. ReplayGain is
        # surfaced as "Normalization" for users who don't know the
        # tag-spec name. Only the genuinely-load-bearing hints are
        # kept: gapless mentions its processing cost, media-keys
        # mentions the restart requirement.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # Single shared form so Quality and Normalization labels +
        # combo boxes column-align cleanly.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._quality_combo = _OpaqueComboBox()
        for label, key in AUDIO_QUALITIES:
            self._quality_combo.addItem(label, key)
        self._select_combo_by_data(self._quality_combo, self.s.audio_quality or "original")
        self._quality_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s,
                "audio_quality",
                self._quality_combo.currentData() or "original",
            )
        )
        form.addRow(self._field_label("Quality:"), self._quality_combo)

        # ReplayGain is the canonical tag spec, but the user-facing
        # label reads as "Normalization" since that's the effect users
        # recognise (and what other players call it).
        self._rg_combo = _OpaqueComboBox()
        for label, key in REPLAYGAIN_MODES:
            self._rg_combo.addItem(label, key)
        self._select_combo_by_data(self._rg_combo, self.s.replaygain)
        self._rg_combo.currentIndexChanged.connect(self._on_replaygain_changed)
        form.addRow(self._field_label("Normalization:"), self._rg_combo)

        v.addLayout(form)
        v.addSpacing(8)

        # Each checkbox lives in an HBox with its inline note pushed to
        # the right via a stretch — keeps the checkbox column visually
        # tidy without breaking the row rhythm with separate dim-caption
        # rows below each box.
        gapless_row = QHBoxLayout()
        gapless_row.setContentsMargins(0, 0, 0, 0)
        self._gapless_check = QCheckBox("Gapless playback")
        self._gapless_check.setChecked(self.s.gapless)
        self._gapless_check.toggled.connect(lambda val: setattr(self.s, "gapless", val))
        gapless_row.addWidget(self._gapless_check)
        gapless_row.addStretch(1)
        gapless_note = QLabel("Small processing cost.")
        gapless_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
        )
        gapless_row.addWidget(gapless_note)
        v.addLayout(gapless_row)

        self._media_keys_check = QCheckBox("OS media keys & system media controls")
        self._media_keys_check.setChecked(self.s.media_controls_enabled)
        self._media_keys_check.toggled.connect(
            lambda val: setattr(self.s, "media_controls_enabled", val)
        )
        v.addWidget(self._media_keys_check)

        # Show streaming info above the bottom bar's play button.
        # Off by default — surfaces container + bitrate for users
        # who want to verify they're getting the expected quality
        # (e.g., FLAC vs transcoded MP3). Wired live via the bus
        # so a toggle doesn't need a restart.
        stream_row = QHBoxLayout()
        stream_row.setContentsMargins(0, 0, 0, 0)
        self._stream_info_check = QCheckBox(
            "Show streaming format & bitrate above play button"
        )
        self._stream_info_check.setChecked(self.s.show_streaming_info)

        def _on_stream_info_toggled(val: bool):
            self.s.show_streaming_info = val
            try:
                PlayerBus.get().streaming_info_changed.emit(val)
            except Exception:
                pass

        self._stream_info_check.toggled.connect(_on_stream_info_toggled)
        stream_row.addWidget(self._stream_info_check)
        stream_row.addStretch(1)
        mk_note = QLabel("Restart required.")
        mk_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        stream_row.addWidget(mk_note)
        v.addLayout(stream_row)

        # ── Equalizer section ──────────────────────────────────────────────
        v.addSpacing(12)
        v.addWidget(self._build_eq_section())

        v.addStretch(1)
        return page

    def _build_eq_section(self) -> QWidget:
        """10-band graphic EQ + master pre-amp. Per docs/research/
        eq_dsp.md. Off by default; explicit "no longer bit-perfect"
        disclosure on the toggle. Cast-greying observes
        PlayerBus.cast_started / cast_stopped — the EQ chain lives
        inside mpv and doesn't apply to the cast device's own decoder."""
        from modules.eq_presets import (
            BAND_FREQUENCIES,
            PRESETS,
        )

        wrap = QFrame()
        wrap.setObjectName("jtEqSection")
        wrap.setStyleSheet(
            "QFrame#jtEqSection { background: transparent; border: none; }"
        )
        wv = QVBoxLayout(wrap)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(8)

        # ── Header row: enabled checkbox + preset combo + save / delete ────
        header = QHBoxLayout()
        header.setSpacing(8)

        self._eq_enabled_check = QCheckBox("Equalizer")
        self._eq_enabled_check.setChecked(self.s.eq_enabled)
        self._eq_enabled_check.toggled.connect(self._on_eq_enabled_toggled)
        header.addWidget(self._eq_enabled_check)

        header.addSpacing(12)
        preset_lbl = QLabel("Preset:")
        preset_lbl.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        header.addWidget(preset_lbl)

        self._eq_preset_combo = _OpaqueComboBox()
        self._populate_eq_preset_combo()
        self._select_combo_by_data(self._eq_preset_combo, self.s.eq_preset)
        self._eq_preset_combo.currentIndexChanged.connect(self._on_eq_preset_changed)
        header.addWidget(self._eq_preset_combo, 1)

        self._eq_save_btn = QPushButton("Save…")
        self._eq_save_btn.clicked.connect(self._on_eq_save_preset)
        header.addWidget(self._eq_save_btn)

        self._eq_delete_btn = QPushButton("Delete")
        self._eq_delete_btn.clicked.connect(self._on_eq_delete_preset)
        header.addWidget(self._eq_delete_btn)

        wv.addLayout(header)

        # Caption row — empty by default; populated during cast-greying
        # with "Casting — EQ inactive". Kept as a single QLabel so the
        # cast message and (former) bit-perfect disclosure share one
        # slot; setting empty text leaves no visible gap.
        self._eq_caption = QLabel("")
        self._eq_caption.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 22px;"
        )
        wv.addWidget(self._eq_caption)

        # ── Slider grid: pre-amp + 10 bands ─────────────────────────────────
        # Vertical sliders in a QGridLayout. Row 0 = live dB readout,
        # row 1 = slider, row 2 = band-centre label. Indices: column 0
        # is pre-amp, columns 1..10 are bands 31Hz..16kHz.
        self._eq_sliders: list[QSlider] = []
        self._eq_readouts: list[QLabel] = []

        # Each column is its own QWidget with a QVBoxLayout —
        # using a single QGridLayout for all 11 columns let the slider
        # widget visually overflow into the band-label row at min/max
        # value. Per-column widgets give Qt strict bounds per column
        # so the layout can't bleed across rows.
        slider_frame = QFrame()
        slider_frame.setStyleSheet("QFrame { background: transparent; }")
        sf_layout = QHBoxLayout(slider_frame)
        sf_layout.setContentsMargins(8, 4, 8, 4)
        sf_layout.setSpacing(6)

        def _fmt_freq(hz: int) -> str:
            return f"{hz // 1000}k" if hz >= 1000 else str(hz)

        labels = ["Pre"] + [_fmt_freq(f) for f in BAND_FREQUENCIES]
        initial = [self.s.eq_preamp] + list(self.s.eq_bands)

        for label_text, val in zip(labels, initial):
            col_widget = QWidget()
            col_widget.setStyleSheet("background: transparent;")
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(0, 0, 0, 0)
            col_layout.setSpacing(8)

            readout = QLabel(self._fmt_db_readout(val))
            readout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            readout.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            readout.setFixedHeight(18)
            col_layout.addWidget(readout)
            self._eq_readouts.append(readout)

            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setSingleStep(1)
            slider.setPageStep(3)
            slider.setTickPosition(QSlider.TickPosition.TicksRight)
            slider.setTickInterval(6)  # ticks at -12, -6, 0, +6, +12
            slider.setValue(int(round(float(val))))
            slider.setFixedHeight(150)
            slider.setStyleSheet(self._eq_slider_qss())
            # Double-click returns the slider to 0 dB. Qt has no signal
            # for double-click on a slider, so we install an event
            # filter on the slider widget to catch the event.
            slider.installEventFilter(self)
            slider.valueChanged.connect(self._on_eq_slider_changed)
            # Defer the actual filter-chain apply until the user
            # releases the slider — mid-drag mpv["af"] rewrites cause
            # audible pops as the filter graph re-plugs. valueChanged
            # still fires for keyboard / click / double-click and those
            # paths go through the settle timer because no
            # sliderReleased fires.
            slider.sliderPressed.connect(self._on_eq_drag_started)
            slider.sliderReleased.connect(self._on_eq_drag_ended)
            # The slider gets center-aligned in its column so the
            # 4px-wide groove sits flush under the readout label.
            col_layout.addWidget(slider, 0, Qt.AlignmentFlag.AlignHCenter)
            self._eq_sliders.append(slider)

            band_lbl = QLabel(label_text)
            band_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            # TEXT_DIM (not TEXT_FAINT) so the freq label is legible
            # against the dialog background — the row sits right under
            # the slider's accent-purple fill and needed more contrast.
            band_lbl.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            band_lbl.setFixedHeight(18)
            col_layout.addWidget(band_lbl)

            col_widget.setFixedWidth(42)
            sf_layout.addWidget(col_widget)

        sf_layout.addStretch(1)
        wv.addWidget(slider_frame)

        # Slider drag → 30ms settle timer → one settings write + signal
        # emit. Per docs/research/eq_dsp.md §3 throttling: a 60Hz drag
        # × 11 sliders is otherwise 660 `af` writes/sec.
        self._eq_settle_timer = QTimer(self)
        self._eq_settle_timer.setSingleShot(True)
        self._eq_settle_timer.setInterval(30)
        self._eq_settle_timer.timeout.connect(self._on_eq_settled)
        self._eq_pending_user_drag = False

        # Cast-greying: observe cast_started / cast_stopped. EQ lives in
        # mpv; cast devices decode their own stream, so EQ doesn't apply
        # while casting. Disable the whole section + show a clarifying
        # caption while a cast is active.
        try:
            bus = PlayerBus.get()
            bus.cast_started.connect(self._on_eq_cast_active)
            bus.cast_stopped.connect(self._on_eq_cast_cleared)
        except Exception:
            pass

        # Initialize enabled / disabled state from current settings +
        # check if a cast is already active at dialog construction time.
        self._refresh_eq_enabled_state()

        # Stash refs to enable bulk en/disable in cast-greying.
        self._eq_section_widget = wrap
        self._eq_preset_count_builtin = len(PRESETS)
        return wrap

    # ── EQ helpers ──────────────────────────────────────────────────────────

    def _eq_slider_qss(self) -> str:
        """Vertical EQ slider QSS. Accent on the filled (below-handle)
        portion, dim grey above. The groove gets explicit 9px top +
        bottom margins so the 14px circular handle at min/max value
        doesn't extend visually past the widget bounds and overlap
        the freq label below. Reads accent live so a theme change
        rebuilds it on the next dialog open."""
        from modules.ui_helpers import ACCENT as _ACCENT

        return f"""
            QSlider:vertical {{
                background: transparent;
            }}
            QSlider::groove:vertical {{
                width: 4px;
                margin: 22px 0;
                background: rgba(255,255,255,0.10);
                border-radius: 2px;
            }}
            QSlider::sub-page:vertical,
            QSlider::add-page:vertical {{
                background: rgba(255,255,255,0.10);
                border-radius: 2px;
            }}
            QSlider::handle:vertical {{
                width: 14px; height: 14px; margin: 0 -5px;
                background: {_ACCENT}; border-radius: 7px;
                border: 2px solid #ffffff;
            }}
            QSlider::tick:vertical {{
                background: rgba(255,255,255,0.18);
            }}
        """

    def _fmt_db_readout(self, val) -> str:
        try:
            x = float(val)
        except (TypeError, ValueError):
            x = 0.0
        if abs(x) < 0.05:
            return "0"
        sign = "+" if x > 0 else "−"
        return f"{sign}{abs(x):g}"

    def _populate_eq_preset_combo(self):
        """Built-in presets + user presets + Custom. Custom is the
        sentinel for "the user dragged a slider so no named preset
        applies". Always selected programmatically from the slider
        path; never user-picked."""
        from modules.eq_presets import PRESETS

        self._eq_preset_combo.blockSignals(True)
        try:
            self._eq_preset_combo.clear()
            for name in PRESETS:
                self._eq_preset_combo.addItem(name, name)
            user = self.s.eq_user_presets
            if user:
                # Separator-ish — insert a divider via a non-selectable
                # disabled item so user presets visually group.
                for name in sorted(user):
                    self._eq_preset_combo.addItem(name, name)
            # Custom always last. Stored so the combo always has a
            # selectable label that reflects the slider state.
            self._eq_preset_combo.addItem("Custom", "Custom")
        finally:
            self._eq_preset_combo.blockSignals(False)

    def _on_eq_enabled_toggled(self, val: bool):
        self.s.eq_enabled = val
        self._refresh_eq_enabled_state()
        self._emit_eq_changed()

    def _refresh_eq_enabled_state(self):
        """Apply the enable/disable cascade based on current settings
        AND any active cast. Sliders dim but remain legible when EQ is
        off (per research doc §4) so the user can preview a curve; they
        hard-disable on cast so the user understands the section is
        inactive."""
        eq_on = bool(self.s.eq_enabled)
        cast_active = getattr(self, "_eq_cast_blocking", False)
        # Controls below the enabled toggle gate on the master switch.
        # Disable entirely while casting since the chain has no effect.
        section_active = not cast_active
        self._eq_preset_combo.setEnabled(section_active and eq_on)
        self._eq_save_btn.setEnabled(section_active and eq_on)
        self._eq_delete_btn.setEnabled(
            section_active and eq_on and self._current_preset_is_user()
        )
        for s in self._eq_sliders:
            s.setEnabled(section_active and eq_on)
        for r in self._eq_readouts:
            # Readouts stay visible even when EQ is off so the user can
            # see what curve they have queued; just dim them further.
            r.setStyleSheet(
                f"color: {'#888' if not eq_on else TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
        if cast_active:
            self._eq_caption.setText(
                "Casting — EQ applies to local playback only and is inactive now."
            )
        else:
            self._eq_caption.setText("")

    def _current_preset_is_user(self) -> bool:
        name = self._eq_preset_combo.currentData() or ""
        return name in self.s.eq_user_presets

    def _on_eq_cast_active(self, *_args):
        self._eq_cast_blocking = True
        self._refresh_eq_enabled_state()

    def _on_eq_cast_cleared(self, *_args):
        self._eq_cast_blocking = False
        self._refresh_eq_enabled_state()

    def _on_eq_drag_started(self):
        """Mouse drag began on a slider. While dragging, valueChanged
        updates only the readout — the actual filter-chain apply waits
        for sliderReleased. mpv["af"] rewrites mid-drag cause audible
        pops as the filter graph re-plugs; deferring to release means
        one clean apply per slider gesture."""
        self._eq_dragging = True

    def _on_eq_drag_ended(self):
        """Mouse drag ended. Cancel any pending settle and apply
        immediately so the user hears the new curve as soon as they
        release the slider."""
        self._eq_dragging = False
        if self._eq_settle_timer.isActive():
            self._eq_settle_timer.stop()
        self._eq_pending_user_drag = True
        self._on_eq_settled()

    def _on_eq_slider_changed(self, _val: int):
        # Update the readout immediately for live visual feedback.
        sender = self.sender()
        if isinstance(sender, QSlider):
            try:
                idx = self._eq_sliders.index(sender)
                self._eq_readouts[idx].setText(self._fmt_db_readout(sender.value()))
            except (ValueError, IndexError):
                pass
        self._eq_pending_user_drag = True
        # Mid-drag: only the readout updates; the filter apply waits
        # for sliderReleased to avoid mid-drag chain rewrites and
        # the resulting audible pops. Non-drag value changes
        # (keyboard arrows, click on track, double-click-to-zero,
        # preset pick) don't fire sliderPressed/Released, so they
        # fall through to the settle timer which collapses rapid
        # sequential changes into one apply.
        if getattr(self, "_eq_dragging", False):
            return
        self._eq_settle_timer.start()

    def _on_eq_settled(self):
        """Slider settle — persist + emit. Switches preset to Custom
        if the user dragged manually."""
        if not self._eq_pending_user_drag:
            return
        self._eq_pending_user_drag = False
        preamp = float(self._eq_sliders[0].value())
        bands = [float(s.value()) for s in self._eq_sliders[1:]]
        self.s.eq_preamp = preamp
        self.s.eq_bands = bands
        # Drag → Custom unless the slider state happens to match the
        # currently-selected preset (e.g. user dragged then dragged
        # back). Cheap check; named-preset preservation is nicer UX.
        current_name = self._eq_preset_combo.currentData() or ""
        if not self._slider_state_matches_preset(current_name, preamp, bands):
            self._select_combo_by_data(self._eq_preset_combo, "Custom")
            self.s.eq_preset = "Custom"
        self._emit_eq_changed()

    def _slider_state_matches_preset(
        self, name: str, preamp: float, bands: list
    ) -> bool:
        from modules.eq_presets import PRESETS

        if name == "Custom":
            return False
        if name in PRESETS:
            ref = PRESETS[name]
            # Built-in presets carry auto-attenuated pre-amp (see
            # _on_eq_preset_changed) — match the same formula here so
            # the combo doesn't flip to Custom after the user picks a
            # built-in.
            max_positive = max(ref) if ref else 0.0
            expected_preamp = -max_positive if max_positive > 0 else 0.0
            return (
                all(abs(a - b) < 1e-6 for a, b in zip(ref, bands))
                and abs(preamp - expected_preamp) < 1e-6
            )
        user = self.s.eq_user_presets.get(name)
        if user is None:
            return False
        ref_bands = user.get("bands", [])
        ref_preamp = float(user.get("preamp", 0.0))
        return (
            abs(preamp - ref_preamp) < 1e-6
            and all(abs(a - b) < 1e-6 for a, b in zip(ref_bands, bands))
        )

    def _on_eq_preset_changed(self, _idx: int):
        name = self._eq_preset_combo.currentData() or ""
        if name == "Custom":
            # Selecting Custom keeps current slider values; just persist
            # the preset name so the combo reflects state on next open.
            self.s.eq_preset = "Custom"
            self._refresh_eq_enabled_state()
            return
        # Pull preset values from built-in or user table.
        from modules.eq_presets import PRESETS, get_preset

        if name in PRESETS:
            bands = get_preset(name)
            # Auto-attenuate pre-amp by the max positive band so
            # cascaded biquad gain doesn't clip on hot masters. Without
            # this, Pop / Rock / Bass Boost (which all have +5 to +7 dB
            # bands) sound garbly/muddy because the cumulative chain
            # gain pushes into clipping territory. Per the research
            # doc's "Pre-amp clipping" edge case — Audacious-style
            # auto-attenuation. User-saved presets carry their own
            # explicit pre-amp so we don't touch those.
            max_positive = max(bands) if bands else 0.0
            preamp = -max_positive if max_positive > 0 else 0.0
        else:
            user = self.s.eq_user_presets.get(name)
            if user is None:
                return
            preamp = float(user.get("preamp", 0.0))
            bands = [float(b) for b in user.get("bands", [])]
            from modules.eq_presets import BAND_COUNT

            if len(bands) != BAND_COUNT:
                return
        # Snap sliders without firing the drag handler.
        self._apply_slider_values(preamp, bands)
        self.s.eq_preamp = preamp
        self.s.eq_bands = bands
        self.s.eq_preset = name
        self._refresh_eq_enabled_state()
        self._emit_eq_changed()

    def _apply_slider_values(self, preamp: float, bands: list):
        """Set slider values without triggering the user-drag handler."""
        values = [preamp] + list(bands)
        for slider, readout, val in zip(self._eq_sliders, self._eq_readouts, values):
            iv = max(-12, min(12, int(round(float(val)))))
            slider.blockSignals(True)
            try:
                slider.setValue(iv)
            finally:
                slider.blockSignals(False)
            readout.setText(self._fmt_db_readout(iv))

    def _on_eq_save_preset(self):
        """Prompt for a preset name; if it already exists (built-in
        or user), refuse with a message. Save to eq_user_presets,
        refresh the combo, select the new entry."""
        from modules.eq_presets import PRESETS

        name, ok = QInputDialog.getText(
            self, "Save preset", "Preset name:"
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        if name in PRESETS or name in {"Custom", "Flat"}:
            QMessageBox.warning(
                self,
                "Name taken",
                f"'{name}' is a built-in preset name. Pick a different name.",
            )
            return
        existing = self.s.eq_user_presets
        if name in existing:
            ans = QMessageBox.question(
                self,
                "Overwrite preset",
                f"User preset '{name}' already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        preamp = float(self._eq_sliders[0].value())
        bands = [float(s.value()) for s in self._eq_sliders[1:]]
        existing[name] = {"preamp": preamp, "bands": bands}
        self.s.eq_user_presets = existing
        self._populate_eq_preset_combo()
        self._select_combo_by_data(self._eq_preset_combo, name)
        self.s.eq_preset = name
        self._refresh_eq_enabled_state()

    def _on_eq_delete_preset(self):
        """Delete the current preset from eq_user_presets. Built-ins
        can't be deleted (Delete button greys out for them via
        _current_preset_is_user)."""
        name = self._eq_preset_combo.currentData() or ""
        existing = self.s.eq_user_presets
        if name not in existing:
            return
        ans = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete user preset '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        del existing[name]
        self.s.eq_user_presets = existing
        self._populate_eq_preset_combo()
        # Fall back to Flat after a delete so the section reads
        # cleanly. Slider values stay as-is; user can dial back.
        self._select_combo_by_data(self._eq_preset_combo, "Custom")
        self.s.eq_preset = "Custom"
        self._refresh_eq_enabled_state()

    def _emit_eq_changed(self):
        try:
            PlayerBus.get().eq_changed.emit(
                bool(self.s.eq_enabled), list(self.s.eq_bands)
            )
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # Double-click on an EQ slider → snap to 0 dB. Qt's QSlider
        # has no double-click signal, so we install ourselves as an
        # event filter on each slider.
        try:
            from PySide6.QtCore import QEvent

            sliders = getattr(self, "_eq_sliders", None)
            if (
                sliders is not None
                and obj in sliders
                and event.type() == QEvent.Type.MouseButtonDblClick
            ):
                obj.setValue(0)
                # setValue fires valueChanged → triggers the settle
                # timer → persists + emits on its own.
                return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ── Page: Casting ──────────────────────────────────────────────────
    def _build_casting(self) -> QWidget:
        # Dedicated page (moved out of Playback 2026-05-16). Covers:
        # per-protocol discovery toggles, when discovery runs, and the
        # stream-routing escape hatch a cast device behind a private
        # network needs to reach the server. DLNA / Sonos / Snapcast
        # toggle rows are present even though their backends ship in
        # follow-up tasks — having the row visible from day one means
        # a user who doesn't own those device types can pre-disable
        # them and never hear the protocols mentioned again.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ── Device types ───────────────────────────────────────────────
        v.addWidget(self._section_header("Device types"))

        types_note = QLabel(
            "Disable cast types you don't own to skip them during "
            "discovery (faster scans, less network chatter)."
        )
        types_note.setWordWrap(True)
        types_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 2px;"
        )
        v.addWidget(types_note)

        # Per-protocol toggle rows. The Chromecast + AirPlay backends
        # ship today; DLNA / Sonos / Snapcast are accepted as settings
        # ahead of the A22-A24 work landing the actual discovery code.
        # (visible label, attribute name on Settings, ready flag)
        cast_types = [
            ("Chromecast", "cast_chromecast_enabled", True),
            ("AirPlay", "cast_airplay_enabled", True),
            ("DLNA / UPnP", "cast_dlna_enabled", False),
            ("Sonos", "cast_sonos_enabled", False),
            ("Snapcast", "cast_snapcast_enabled", False),
        ]
        self._cast_type_checks: dict = {}
        for label, attr, ready in cast_types:
            label_text = label if ready else f"{label} (coming soon)"
            cb = QCheckBox(label_text)
            cb.setChecked(bool(getattr(self.s, attr)))
            cb.toggled.connect(lambda val, a=attr: setattr(self.s, a, val))
            v.addWidget(cb)
            self._cast_type_checks[attr] = cb

        v.addSpacing(8)

        # ── Discovery timing ───────────────────────────────────────────
        v.addWidget(self._section_header("Discovery timing"))

        self._discover_at_startup_radio = QRadioButton("Discover at startup")
        self._discover_on_demand_radio = QRadioButton("Discover on demand (recommended)")
        timing_group = QButtonGroup(self)
        timing_group.addButton(self._discover_at_startup_radio)
        timing_group.addButton(self._discover_on_demand_radio)
        # Keep the group alive on self so it doesn't get GC'd; without
        # it the radios behave as independent checkboxes.
        self._discover_timing_group = timing_group
        current_timing = self.s.cast_discovery_timing
        self._discover_at_startup_radio.setChecked(current_timing == "startup")
        self._discover_on_demand_radio.setChecked(current_timing != "startup")

        def _on_timing_changed(_checked: bool):
            v_ = "startup" if self._discover_at_startup_radio.isChecked() else "on_demand"
            self.s.cast_discovery_timing = v_

        self._discover_at_startup_radio.toggled.connect(_on_timing_changed)
        v.addWidget(self._discover_at_startup_radio)
        v.addWidget(self._discover_on_demand_radio)

        timing_note = QLabel(
            "On-demand scans only when you open the cast menu — no "
            "mDNS chatter at boot for users who rarely cast."
        )
        timing_note.setWordWrap(True)
        timing_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 22px;"
        )
        v.addWidget(timing_note)

        v.addSpacing(8)

        # ── Stream routing ─────────────────────────────────────────────
        # A cast device can only fetch a URL it can route to. When this
        # machine reaches the server over Tailscale / a remote domain /
        # a self-signed host, the speaker can't — so jellytoast can relay
        # the stream through a small local HTTP server instead. "Auto"
        # picks per-server; the manual modes are escape hatches.
        v.addWidget(self._section_header("Stream routing"))

        cast_form = QFormLayout()
        cast_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        cast_form.setHorizontalSpacing(16)
        cast_form.setVerticalSpacing(10)

        self._cast_routing_combo = _OpaqueComboBox()
        for label, key in CAST_ROUTING_MODES:
            self._cast_routing_combo.addItem(label, key)
        self._select_combo_by_data(self._cast_routing_combo, self.s.cast_stream_routing or "auto")
        self._cast_routing_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s,
                "cast_stream_routing",
                self._cast_routing_combo.currentData() or "auto",
            )
        )
        cast_form.addRow(self._field_label("Routing:"), self._cast_routing_combo)
        v.addLayout(cast_form)

        cast_note = QLabel(
            "Relaying streams the audio through this device — keep it "
            "running and on the same network as the speaker."
        )
        cast_note.setWordWrap(True)
        cast_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 2px;"
        )
        v.addWidget(cast_note)

        v.addStretch(1)
        return page

    def _on_replaygain_changed(self):
        mode = self._rg_combo.currentData() or "no"
        PlayerBus.get().replaygain_changed.emit(mode)

    # ── Page: Library ──────────────────────────────────────────────────
    def _build_library(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        # Trimmed 2026-05-11 to fit the full Library page within the
        # dialog viewport: every long descriptive paragraph dropped
        # (Loading, Shuffle, Tiles), and the Cache note collapsed to a
        # single line. Section headers retained — they're the visual
        # chunking that tells the user what each control affects.
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ── Loading ────────────────────────────────────────────────────
        v.addWidget(self._section_header("Loading"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        # Page-size dropdown — `data=0` is the "load all" sentinel
        # (LibraryGrid chains pages of 500 internally so Subsonic's
        # 500-per-call cap doesn't truncate big libraries).
        self._page_size_combo = _OpaqueComboBox()
        for label, key in (
            ("Load all at once", 0),
            ("100 per page", 100),
            ("200 per page", 200),
            ("500 per page", 500),
            ("1000 per page", 1000),
        ):
            self._page_size_combo.addItem(label, key)
        self._select_combo_by_data(
            self._page_size_combo,
            self.s.library_page_size,
        )
        self._page_size_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s,
                "library_page_size",
                int(self._page_size_combo.currentData() or 200),
            )
        )
        form.addRow(
            self._field_label("Album / artist grids:"),
            self._page_size_combo,
        )
        v.addLayout(form)

        # ── Shuffle ────────────────────────────────────────────────────
        v.addWidget(self._section_header("Shuffle"))

        shuffle_form = QFormLayout()
        shuffle_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        shuffle_form.setHorizontalSpacing(16)
        shuffle_form.setVerticalSpacing(10)

        self._shuffle_size_combo = _OpaqueComboBox()
        for label, key in (
            ("50 tracks", 50),
            ("100 tracks", 100),
            ("250 tracks", 250),
            ("500 tracks", 500),
            ("1000 tracks", 1000),
        ):
            self._shuffle_size_combo.addItem(label, key)
        self._select_combo_by_data(
            self._shuffle_size_combo,
            self.s.shuffle_queue_size,
        )
        self._shuffle_size_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s,
                "shuffle_queue_size",
                int(self._shuffle_size_combo.currentData() or 100),
            )
        )
        shuffle_form.addRow(
            self._field_label("Queue size:"),
            self._shuffle_size_combo,
        )
        v.addLayout(shuffle_form)

        # ── Tiles ──────────────────────────────────────────────────────
        v.addWidget(self._section_header("Tiles"))

        self._cover_prefetch_check = QCheckBox("Pre-load covers outside the viewport")
        self._cover_prefetch_check.setChecked(self.s.library_cover_prefetch)
        self._cover_prefetch_check.toggled.connect(
            lambda val: setattr(self.s, "library_cover_prefetch", val)
        )
        v.addWidget(self._cover_prefetch_check)

        self._tile_fade_check = QCheckBox("Fade tiles in as covers load")
        self._tile_fade_check.setChecked(self.s.library_tile_fade)
        self._tile_fade_check.toggled.connect(lambda val: setattr(self.s, "library_tile_fade", val))
        v.addWidget(self._tile_fade_check)

        # ── Cache ──────────────────────────────────────────────────────
        # Surfaces the cover-art disk cache. The 200 MB LRU cap lives
        # in `image_cache`; here we expose footprint + a Clear button
        # so a user with stale art after switching servers can wipe it
        # without nuking the whole config dir.
        v.addWidget(self._section_header("Cache"))

        # Footprint label + Clear button on one row so the cache
        # readout reads as a single status strip rather than two
        # stacked rows. Button right-aligned for the typical
        # "info-left / action-right" form pattern.
        cache_row = QHBoxLayout()
        cache_row.setSpacing(12)
        self._cache_size_label = QLabel("Calculating…")
        self._cache_size_label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        cache_row.addWidget(self._cache_size_label)
        cache_row.addStretch(1)
        clear_cache_btn = QPushButton("Refresh album art")
        clear_cache_btn.setObjectName("ghost")
        clear_cache_btn.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(clear_cache_btn)
        v.addLayout(cache_row)
        # Compute on dialog open (cheap — directory walk over a few
        # hundred files at most).
        QTimer.singleShot(0, self._refresh_cache_size_label)

        cache_note = QLabel(
            "Drops cached covers + re-fetches the visible ones now. "
            "Use after updating album art on the server."
        )
        cache_note.setWordWrap(True)
        cache_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 2px;")
        v.addWidget(cache_note)

        v.addStretch(1)
        return page

    def _refresh_cache_size_label(self):
        """Sum every PNG in the cover cache dir and render a human-
        readable footprint string. Best-effort — surfaces 'Unavailable'
        if we can't read the dir for any reason."""
        try:
            from modules import image_cache as _ic

            total = 0
            count = 0
            cache_dir = _ic._cache_dir()
            for entry in cache_dir.iterdir():
                if not entry.is_file() or entry.suffix != ".png":
                    continue
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
            mb = total / (1024 * 1024)
            self._cache_size_label.setText(f"On disk: {mb:.1f} MB across {count} files")
        except Exception:
            self._cache_size_label.setText("On disk: unavailable")

    def _on_clear_cache(self):
        # Both legs of the cache: disk (image_cache) + in-memory
        # (ui_helpers pixmap + raw caches). Without the in-memory
        # leg, tiles painted in the current session keep displaying
        # the old art until next launch. The bus signal nudges every
        # visible cover surface to re-issue its loads — those land
        # on a cold cache and refetch from the server, picking up
        # whatever the user fixed (album art uploads, retags, etc.)
        # since the previous fetch.
        from modules import image_cache as _ic
        from modules.ui_helpers import clear_image_caches

        _ic.clear()
        clear_image_caches()
        try:
            from modules.player_state import PlayerBus

            PlayerBus.get().image_cache_cleared.emit()
        except Exception:
            pass
        self._refresh_cache_size_label()

    # ── Page: Display ──────────────────────────────────────────────────
    # Unified page covering everything that affects how the UI looks:
    # theme mode (was Appearance), lyrics text size (was Lyrics), and
    # the placeholder scaling sliders. Three sections separated by
    # section headers + blank space, so the visual hierarchy reads as
    # one settings page rather than three pages stitched together.
    def _build_display(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # Restart notice — page-level alert pinned to the top so it
        # doesn't shove itself between controls when the user picks
        # a new theme / accent. Visible only after a dirty change.
        _ar2, _ag2, _ab2 = _hex_to_rgb(ACCENT)
        self._theme_restart_notice = QLabel("Restart jellytoast to apply the new theme.")
        self._theme_restart_notice.setWordWrap(True)
        self._theme_restart_notice.setStyleSheet(
            f"color: {TEXT}; "
            f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
            f"border-radius: 6px; "
            f"padding: 10px 14px; "
            f"{type_qss(TYPE_CAPTION)}"
        )
        self._theme_restart_notice.setMinimumHeight(36)
        self._theme_restart_notice.hide()
        v.addWidget(self._theme_restart_notice)

        # ── Theme ──────────────────────────────────────────────────────
        v.addWidget(self._section_header("Theme"))

        theme_form = QFormLayout()
        theme_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        theme_form.setHorizontalSpacing(16)
        theme_form.setVerticalSpacing(10)

        self._theme_combo = _OpaqueComboBox()
        self._initial_theme = self.s.theme_mode
        for label, key, enabled in _THEME_CHOICES:
            self._theme_combo.addItem(label, key)
            if not enabled:
                # Disable through the underlying model — QComboBox itself
                # doesn't expose per-item enable. The item still renders
                # in the popup but is not selectable.
                idx = self._theme_combo.count() - 1
                item = self._theme_combo.model().item(idx)
                if item is not None:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self._select_combo_by_data(self._theme_combo, self._initial_theme)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_form.addRow(self._field_label("Mode:"), self._theme_combo)
        v.addLayout(theme_form)

        theme_note = QLabel("Light theme coming in a future build.")
        theme_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 2px;")
        v.addWidget(theme_note)

        # ── Accent color ───────────────────────────────────────────────
        v.addWidget(self._section_header("Accent"))
        v.addLayout(self._build_accent_row())
        v.addSpacing(6)
        accent_note = QLabel("Tints buttons, sliders, scrollbars, and active icons.")
        accent_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(accent_note)

        # ── Scaling ────────────────────────────────────────────────────
        # Font size scales every design-token font size + button
        # geometry via `modules.design_tokens` (restart required).
        # Lyrics font size lives here too since it's a type-size knob
        # in spirit; it applies live via PlayerBus. UI scale is NOT
        # exposed — KDE Plasma's system-wide scale slider already
        # handles it via Qt 6's `wp_fractional_scale_v1` integration,
        # so adding a duplicate app-level knob would just diverge.
        v.addWidget(self._section_header("Scaling"))

        scaling_form = QFormLayout()
        scaling_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        scaling_form.setHorizontalSpacing(16)
        scaling_form.setVerticalSpacing(10)

        # Font size — app-wide font scale. Writes to settings.font_scale
        # and triggers the restart notice; design_tokens reads the
        # setting at module import and bakes the multiplier into every
        # TypeTier / ButtonTier.
        self._font_size_combo = _OpaqueComboBox()
        for label, key in LYRICS_FONT_SIZES:
            self._font_size_combo.addItem(label, key)
        self._initial_font_scale = self.s.font_scale
        self._select_combo_by_data(self._font_size_combo, self._initial_font_scale)
        self._font_size_combo.currentIndexChanged.connect(self._on_font_scale_changed)
        scaling_form.addRow(self._field_label("Font size:"), self._font_size_combo)

        # Lyrics font size — wires live to PlayerBus, no restart needed.
        self._lyrics_size_combo = _OpaqueComboBox()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        scaling_form.addRow(self._field_label("Lyrics font size:"), self._lyrics_size_combo)

        v.addLayout(scaling_form)

        # ── Interface ──────────────────────────────────────────────────
        v.addSpacing(4)
        v.addWidget(self._section_header("Interface"))

        self._tooltips_check = QCheckBox("Show hover tooltips")
        self._tooltips_check.setChecked(self.s.show_tooltips)
        self._tooltips_check.toggled.connect(lambda val: setattr(self.s, "show_tooltips", val))
        v.addWidget(self._tooltips_check)

        tooltips_note = QLabel(
            "The little labels that pop up when you hover a button. Applies immediately."
        )
        tooltips_note.setWordWrap(True)
        tooltips_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 22px;"
        )
        v.addWidget(tooltips_note)

        v.addStretch(1)
        return page

    def _on_lyrics_size_changed(self):
        key = self._lyrics_size_combo.currentData() or "default"
        self.s.lyrics_font_size = key
        PlayerBus.get().lyrics_font_size_changed.emit(key)

    def _on_font_scale_changed(self):
        # Persist immediately; design_tokens picks it up on the next
        # module import (i.e. next launch). Show the same restart
        # notice the theme + accent path uses so the user gets one
        # consolidated reminder regardless of which display setting
        # they changed.
        key = self._font_size_combo.currentData() or "default"
        self.s.font_scale = key
        self._refresh_restart_notice_visibility()

    def _refresh_restart_notice_visibility(self):
        """Show the restart banner if a setting that bakes into module
        state differs from the value loaded when the dialog opened.
        Accent intentionally NOT included — it live-applies via
        ``refresh_theme()`` + ``PlayerBus.theme_changed`` so the user
        sees the new colour immediately and doesn't need a restart."""
        dirty = (
            self._theme_combo.currentData() != self._initial_theme
            or self.s.font_scale != self._initial_font_scale
        )
        self._theme_restart_notice.setVisible(dirty)

    def _on_theme_changed(self):
        chosen = self._theme_combo.currentData() or "frosted_dark"
        self.s.theme_mode = chosen
        self._refresh_restart_notice_visibility()

    # ── Page: Hotkeys ──────────────────────────────────────────────────
    # Read-only list of every keyboard shortcut the app responds to.
    # Customization (rebinding) is a future enhancement; for now the
    # page exists to make discoverable what's already wired in
    # JellytoastWindow.__init__ — Ctrl+F, /, Ctrl+Shift+L, etc.
    def _build_hotkeys(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        v.addWidget(self._section_header("Navigation"))
        v.addLayout(self._hotkey_row("Search", "Ctrl+F  ·  /"))
        v.addLayout(self._hotkey_row("All music", "Ctrl+Shift+L"))

        v.addWidget(self._section_header("Playback"))
        v.addLayout(self._hotkey_row("Play / Pause", "Media Play"))
        v.addLayout(self._hotkey_row("Next track", "Media Next"))
        v.addLayout(self._hotkey_row("Previous track", "Media Previous"))

        note = QLabel("Media keys route through MPRIS. Customization coming soon.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)
        v.addStretch(1)
        return page

    def _hotkey_row(self, label: str, keys: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)
        binding = QLabel(keys)
        binding.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            "background: rgba(255,255,255,0.06); padding: 3px 9px; "
            "border-radius: 5px;"
        )
        row.addWidget(binding)
        return row

    # ── Page: Scrobbling (placeholder) ─────────────────────────────────
    # Stub for the future Last.fm + ListenBrainz integration. Most
    # surveyed players have a Scrobbling settings page; we put the
    # nav slot in now so it's discoverable and so adding the actual
    # forms later doesn't require navigation churn.
    def _build_scrobbling(self) -> QWidget:
        from modules.scrobble import lastfm as _lastfm

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        # ── Double-scrobble guidance / detection banner ──────────────
        # Three states drive the banner copy + style:
        # 1. Server-side scrobbling DETECTED for at least one service:
        #    confirmatory tone — "your server is scrobbling for you,
        #    in-app is off". Per-service enable checkboxes are also
        #    disabled below to make the chosen state obvious.
        # 2. Server is Navidrome but the native-API probe couldn't read
        #    the user record (older Navidrome version, network blip):
        #    strong reminder phrased toward Navidrome specifically.
        # 3. Anything else: generic guidance — same tone as the original
        #    warning, just covering the manual-config case.
        srv_lf = self.s.server_scrobbles_lastfm
        srv_lb = self.s.server_scrobbles_listenbrainz
        is_navidrome = self.s.server_is_navidrome
        check_done = self.s.server_scrobble_check_done
        if srv_lf or srv_lb:
            services = []
            if srv_lf:
                services.append("Last.fm")
            if srv_lb:
                services.append("ListenBrainz")
            joined = " and ".join(services)
            warning_text = (
                f"Your server is scrobbling {joined} for you — "
                "the in-app option for that service is turned off so "
                "tracks aren't counted twice."
            )
            border = "rgba(150, 125, 225, 0.30)"
            bg = "rgba(150, 125, 225, 0.06)"
        elif is_navidrome and not check_done:
            warning_text = (
                "This is a Navidrome server. If you've already enabled "
                "Last.fm or ListenBrainz scrobbling there, leave these "
                "off — otherwise every track is counted twice."
            )
            border = "rgba(255, 255, 255, 0.14)"
            bg = "rgba(255, 255, 255, 0.03)"
        else:
            warning_text = (
                "If your music server already scrobbles "
                "(e.g. Navidrome's built-in Last.fm / ListenBrainz "
                "integration), leave these off — otherwise every "
                "track is counted twice."
            )
            border = "rgba(255, 255, 255, 0.10)"
            bg = "rgba(255, 255, 255, 0.02)"
        warning = QLabel(warning_text)
        warning.setWordWrap(True)
        warning.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"padding: 10px 12px; border: 1px solid {border};"
            f"border-radius: 8px; background: {bg};"
        )
        v.addWidget(warning)

        # ── ListenBrainz ──────────────────────────────────────────────
        v.addWidget(self._section_header("ListenBrainz"))

        self._lb_enabled = QCheckBox("Enable ListenBrainz scrobbling")
        self._lb_enabled.setChecked(self.s.listenbrainz_enabled)
        self._lb_enabled.toggled.connect(lambda v: setattr(self.s, "listenbrainz_enabled", v))
        v.addWidget(self._lb_enabled)
        if srv_lb:
            # Lock the in-app toggle off when the server has it
            # covered. Tooltip surfaces the *why* on hover.
            self._lb_enabled.setEnabled(False)
            self._lb_enabled.setToolTip(
                "Your Navidrome server is already scrobbling to "
                "ListenBrainz. Disable it there to use jellytoast's "
                "ListenBrainz client instead."
            )

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self._field_label("User token"))
        self._lb_token_edit = QLineEdit()
        self._lb_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._lb_token_edit.setText(self.s.listenbrainz_token)
        self._lb_token_edit.setPlaceholderText("Paste from listenbrainz.org/profile/")
        self._lb_token_edit.editingFinished.connect(self._on_lb_token_changed)
        token_row.addWidget(self._lb_token_edit, 1)
        self._lb_validate_btn = QPushButton("Validate")
        self._lb_validate_btn.setObjectName("ghost")
        self._lb_validate_btn.clicked.connect(self._on_lb_validate)
        token_row.addWidget(self._lb_validate_btn)
        v.addLayout(token_row)

        # Status line: "Connected as <name>" or "Not validated yet".
        self._lb_status = QLabel(self._lb_status_text())
        self._lb_status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(self._lb_status)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(self._field_label("Server URL"))
        self._lb_url_edit = QLineEdit()
        self._lb_url_edit.setText(self.s.listenbrainz_url)
        self._lb_url_edit.setPlaceholderText("https://api.listenbrainz.org")
        self._lb_url_edit.editingFinished.connect(
            lambda: setattr(
                self.s,
                "listenbrainz_url",
                self._lb_url_edit.text().strip() or "https://api.listenbrainz.org",
            )
        )
        url_row.addWidget(self._lb_url_edit, 1)
        v.addLayout(url_row)

        # ── Last.fm ───────────────────────────────────────────────────
        v.addWidget(self._section_header("Last.fm"))

        if not _lastfm.is_configured():
            # API key/secret aren't built into this build yet — surface
            # honestly rather than showing a Connect button that errors.
            placeholder = QLabel(
                "Last.fm scrobbling will be available in a future build "
                "once the in-app API credentials are configured."
            )
            placeholder.setWordWrap(True)
            placeholder.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            v.addWidget(placeholder)
        else:
            self._lf_enabled = QCheckBox("Enable Last.fm scrobbling")
            self._lf_enabled.setChecked(self.s.lastfm_enabled)
            self._lf_enabled.toggled.connect(lambda v: setattr(self.s, "lastfm_enabled", v))
            v.addWidget(self._lf_enabled)
            if srv_lf:
                self._lf_enabled.setEnabled(False)
                self._lf_enabled.setToolTip(
                    "Your Navidrome server is already scrobbling to "
                    "Last.fm. Disable it there to use jellytoast's "
                    "Last.fm client instead."
                )

            lf_row = QHBoxLayout()
            lf_row.setSpacing(8)
            self._lf_status = QLabel(self._lf_status_text())
            self._lf_status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            lf_row.addWidget(self._lf_status, 1)
            if self.s.lastfm_session_key:
                self._lf_action_btn = QPushButton("Disconnect")
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_disconnect)
            else:
                self._lf_action_btn = QPushButton("Connect to Last.fm…")
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_connect)
            lf_row.addWidget(self._lf_action_btn)
            v.addLayout(lf_row)

        v.addStretch(1)
        return page

    # ── Scrobbling helpers ─────────────────────────────────────────────

    def _lb_status_text(self) -> str:
        name = self.s.listenbrainz_username
        if name:
            return f"Connected as {name}."
        if self.s.listenbrainz_token:
            return "Token saved — click Validate to confirm."
        return "Not validated yet."

    def _lf_status_text(self) -> str:
        name = self.s.lastfm_username
        if name:
            return f"Connected as {name}."
        return "Not connected."

    def _on_lb_token_changed(self):
        new_token = (self._lb_token_edit.text() or "").strip()
        if new_token == self.s.listenbrainz_token:
            return
        self.s.listenbrainz_token = new_token
        # Username on file no longer matches the new token — clear it
        # so the status line goes back to "Token saved — click Validate".
        self.s.listenbrainz_username = ""
        self._lb_status.setText(self._lb_status_text())

    def _on_lb_validate(self):
        from modules.async_io import run_async
        from modules.scrobble import listenbrainz as _lb

        token = (self._lb_token_edit.text() or "").strip()
        url = (self._lb_url_edit.text() or "").strip() or _lb.DEFAULT_BASE_URL
        if not token:
            self._lb_status.setText("Enter a token, then click Validate.")
            return
        # Persist whatever the user typed so a successful validate can
        # be acted on immediately by the manager.
        self.s.listenbrainz_token = token
        self.s.listenbrainz_url = url
        self._lb_status.setText("Validating…")
        self._lb_validate_btn.setEnabled(False)
        run_async(
            _lb.validate_token,
            token,
            url,
            on_result=self._on_lb_validated,
            on_error=lambda _e: self._on_lb_validated(None),
        )

    def _on_lb_validated(self, username):
        self._lb_validate_btn.setEnabled(True)
        if username:
            self.s.listenbrainz_username = username
            self._lb_status.setText(f"Connected as {username}.")
        else:
            self.s.listenbrainz_username = ""
            self._lb_status.setText("Couldn't validate — check the token and try again.")

    def _on_lf_connect(self):
        from modules.scrobble import lastfm as _lf
        from modules.async_io import run_async
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        # Step 1: get a request token. Step 2: open browser. Step 3:
        # poll auth.getSession every 3s until the user authorizes (or
        # cancels via the dialog's Close).
        self._lf_action_btn.setEnabled(False)
        self._lf_status.setText("Requesting token…")

        def _on_token(token):
            if not token:
                self._lf_status.setText("Couldn't reach Last.fm — try again later.")
                self._lf_action_btn.setEnabled(True)
                return
            QDesktopServices.openUrl(QUrl(_lf.auth_url(token)))
            self._lf_open_auth_modal(token)

        run_async(
            _lf.get_token,
            on_result=_on_token,
            on_error=lambda _e: _on_token(None),
        )

    def _lf_open_auth_modal(self, token: str):
        from modules.scrobble import lastfm as _lf
        from modules.async_io import run_async

        dlg = QDialog(self)
        dlg.setWindowTitle("Connect to Last.fm")
        dlg.setFixedWidth(380)
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(10)
        msg = QLabel(
            'We\'ve opened Last.fm in your browser. Click "Allow" '
            "there, then come back — jellytoast will detect the "
            "approval automatically."
        )
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        v.addWidget(msg)
        status = QLabel("Waiting for authorization…")
        status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(status)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        v.addLayout(btn_row)

        # Poll auth.getSession every 3 seconds. Each tick spawns a
        # run_async; the dialog timer is what gets cancelled when the
        # user hits Cancel — pending workers just become no-ops by the
        # time their result lands (we re-check dlg.isVisible).
        timer = QTimer(dlg)
        timer.setInterval(3000)

        def _poll():
            if not dlg.isVisible():
                return

            def _on_session(sess):
                if not dlg.isVisible():
                    return
                if sess and sess.get("key"):
                    self.s.lastfm_session_key = sess["key"]
                    self.s.lastfm_username = sess.get("name", "")
                    self.s.lastfm_enabled = True
                    self._lf_action_btn.setText("Disconnect")
                    try:
                        self._lf_action_btn.clicked.disconnect()
                    except Exception:
                        pass
                    self._lf_action_btn.clicked.connect(self._on_lf_disconnect)
                    self._lf_action_btn.setEnabled(True)
                    self._lf_status.setText(self._lf_status_text())
                    timer.stop()
                    dlg.accept()

            run_async(
                _lf.get_session,
                token,
                on_result=_on_session,
                on_error=lambda _e: None,
            )

        timer.timeout.connect(_poll)
        timer.start()
        dlg.exec()
        timer.stop()
        if not self.s.lastfm_session_key:
            self._lf_status.setText("Connection cancelled.")
            self._lf_action_btn.setEnabled(True)

    def _on_lf_disconnect(self):
        self.s.lastfm_session_key = ""
        self.s.lastfm_username = ""
        self.s.lastfm_enabled = False
        self._lf_action_btn.setText("Connect to Last.fm…")
        try:
            self._lf_action_btn.clicked.disconnect()
        except Exception:
            pass
        self._lf_action_btn.clicked.connect(self._on_lf_connect)
        self._lf_status.setText(self._lf_status_text())

    # ── About overlay (replaces the old About page) ────────────────────
    def _show_about(self):
        """Modal info dialog launched from the titlebar info button.
        Lighter than a full settings page since the content is just
        version + blurb — most surveyed music players (Strawberry,
        Supersonic, Feishin, Plexamp) put About in a small dialog
        rather than a settings tab."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About jellytoast")
        dlg.setFixedWidth(380)
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(8)

        title = QLabel("jellytoast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        v.addWidget(title)

        version = QLabel("v0.1.0")
        version.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(version)

        v.addSpacing(8)

        desc = QLabel(
            "Native desktop client for Jellyfin and Subsonic / Navidrome — "
            "all-PySide6 surfaces with bit-perfect mpv playback, MPRIS2, "
            "system tray, floating mini player, and Chromecast/AirPlay "
            "casting."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(desc)

        v.addSpacing(12)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("ghost")
        close_btn.clicked.connect(dlg.accept)
        v.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)

        dlg.exec()

    # ── helpers ────────────────────────────────────────────────────────
    def _section_header(self, text: str) -> QLabel:
        # font(TYPE_MICRO) handles uppercase + letter-spacing via QFont,
        # so we pass mixed-case text here — Qt's QSS doesn't actually
        # honor text-transform/letter-spacing, only QFont does.
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        return label

    def _build_colors(self) -> QWidget:
        """Debug / power-user color editor. Lives in its own module
        (modules.settings_colors_page) — settings_dialog.py is already
        big and this is a self-contained feature."""
        from modules.settings_colors_page import build_colors_page

        return build_colors_page()

    def _build_accent_row(self) -> QHBoxLayout:
        """Row of color swatches matching ACCENT_PRESETS. Selecting one
        writes the hex into ``settings.accent_color`` and surfaces the
        same restart-required notice the theme dropdown uses (theme +
        accent are baked at module-import time today).
        """
        from modules.theme import ACCENT_PRESETS as _PRESETS

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(10)
        self._accent_buttons: list[tuple[str, QPushButton]] = []
        current = (self.s.accent_color or "").strip().lower()
        self._initial_accent = current
        for label, hex_value in _PRESETS:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setToolTip(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(hex_value.lower() == current)
            btn.setStyleSheet(self._accent_swatch_qss(hex_value, btn.isChecked()))
            btn.clicked.connect(lambda _=False, h=hex_value: self._on_accent_picked(h))
            self._accent_buttons.append((hex_value, btn))
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        row.addStretch(1)
        return row

    def _accent_swatch_qss(self, hex_value: str, selected: bool) -> str:
        # Selected swatch gets a thicker white ring; idle gets a faint
        # border so the swatch reads against any body color.
        ring = "rgba(255,255,255,0.85)" if selected else "rgba(255,255,255,0.18)"
        ring_w = 2 if selected else 1
        return f"""
            QPushButton {{
                background: {hex_value};
                border: {ring_w}px solid {ring};
                border-radius: 14px;
            }}
            QPushButton:hover {{
                border-color: rgba(255,255,255,0.55);
            }}
        """

    def _build_and_apply_dialog_stylesheet(self):
        """Compose the dialog-level QSS that styles every QComboBox
        and QPushButton#ghost inside the dialog. Pulled into its own
        method so we can rebuild it from `_on_accent_picked` to
        live-apply a new accent — re-reads `ACCENT` from
        `modules.ui_helpers` at call time, so the refresh_theme()
        path that ran just before this sees its new value."""
        from modules.icons import icon_svg_path
        from modules.ui_helpers import ACCENT as _ACCENT_NOW

        # Brighter chevron (TEXT, not TEXT_DIM) so the dropdown affordance
        # actually reads as a dropdown — the dim version was too easy to
        # miss against the frosted background.
        chevron_path = icon_svg_path("chevron_down", TEXT)
        chevron_url = chevron_path.replace("\\", "/")
        _ar, _ag, _ab = _hex_to_rgb(_ACCENT_NOW)
        self.setStyleSheet(f"""
            QComboBox {{
                background: rgba(255,255,255,0.06);
                color: {TEXT};
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: 6px;
                padding: 6px 12px;
                {type_qss(TYPE_BODY)}
                min-height: 22px;
            }}
            QComboBox:hover {{
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QComboBox:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QComboBox:disabled {{
                color: {TEXT_FAINT};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 28px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox::down-arrow {{
                image: url({chevron_url});
                width: 14px;
                height: 14px;
            }}
            QComboBox QAbstractItemView {{
                background: rgb(20, 22, 26);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px 0px;
                outline: 0;
                selection-background-color: rgba({_ar},{_ag},{_ab},0.85);
                selection-color: white;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 7px 14px;
                min-height: 22px;
                border-radius: 4px;
                margin: 1px 4px;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: rgba({_ar},{_ag},{_ab},0.22);
                color: {TEXT};
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: rgba({_ar},{_ag},{_ab},0.85);
                color: white;
            }}
            QPushButton#ghost {{
                background: rgba(255,255,255,0.04);
                color: {TEXT};
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: 6px;
                padding: 6px 14px;
                {type_qss(TYPE_BODY)}
            }}
            QPushButton#ghost:hover {{
                background: rgba({_ar},{_ag},{_ab},0.10);
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QPushButton#ghost:pressed {{
                background: rgba({_ar},{_ag},{_ab},0.18);
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
        """)

    def _on_accent_picked(self, hex_value: str):
        # 1. Persist the pick.
        self.s.accent_color = hex_value
        # 2. Refresh module-level theme constants + rebuild
        #    GLOBAL_STYLE + clear ICON_ACCENT cache, so anything that
        #    re-reads `ui_helpers.ACCENT` / calls `accent_icon(name)`
        #    after this line sees the new colour.
        from modules import ui_helpers as _uih
        from modules import icons as _icons

        new_global_style = _uih.refresh_theme()
        _icons.refresh_theme()
        # 3. Push the new GLOBAL_STYLE + palette onto the QApplication
        #    so every widget that inherits from app-wide styling (most
        #    checkboxes, tooltips, generic Qt buttons) picks up the
        #    accent immediately. Also re-stamps QPalette.Highlight so
        #    Qt-style-painted selections (QLineEdit text selection,
        #    list views, etc.) flow through.
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(new_global_style)
            from modules.theme import _hex_to_rgb as _h2r

            ar, ag, ab = _h2r(_uih.ACCENT)
            pal = app.palette()
            pal.setColor(QPalette.ColorRole.Highlight, QColor(ar, ag, ab))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
            app.setPalette(pal)
            # Force-repolish QCheckBox + QRadioButton instances so their
            # ::indicator:checked rule (which bakes ACCENT_DEEP / ACCENT)
            # picks up the new colour synchronously instead of waiting
            # for the next style event. Without this the check fill
            # stays the old accent until the user hovers or focuses
            # the box.
            for w in app.allWidgets():
                if isinstance(w, (QCheckBox, QRadioButton)):
                    w.style().unpolish(w)
                    w.style().polish(w)
                    w.update()
        # 4. Update the swatch ring states in the picker so the new
        #    selection reads as checked + others as idle.
        for h, btn in self._accent_buttons:
            checked = h.lower() == hex_value.lower()
            btn.setChecked(checked)
            btn.setStyleSheet(self._accent_swatch_qss(h, checked))
        # 5. Rebuild the dialog's own accent-derived QSS (combo
        #    borders, ghost buttons, restart-notice banner). Without
        #    this the settings dialog itself would lag the rest of
        #    the app until reopened.
        self._reapply_dialog_accent_styling()
        # 6. Broadcast — top-level surfaces that opt in will rebuild
        #    their accent-using stylesheets on this signal.
        PlayerBus.get().theme_changed.emit()
        # 7. Theme + font_scale are still bake-at-import-time changes;
        #    keep the restart notice visible if any of those differ
        #    from the initial values when the dialog was opened.
        self._refresh_restart_notice_visibility()

    def _reapply_dialog_accent_styling(self):
        """Rebuild every QSS string in the dialog that bakes ACCENT.
        Called on accent change so the dialog reflects the new colour
        immediately instead of waiting for a reopen."""
        # Reapply the top-level dialog stylesheet (QComboBox borders,
        # ghost button outlines, popup item styling). The stylesheet
        # is built once in __init__ from `ACCENT` literals; rebuilding
        # means re-running the same code with the now-refreshed
        # module-level constant.
        self._build_and_apply_dialog_stylesheet()
        # Restart notice fill / left bar use the active accent. Rebuild
        # its inline stylesheet so its colour matches the new accent.
        from modules.ui_helpers import ACCENT as _ACCENT_NOW

        _ar2, _ag2, _ab2 = _hex_to_rgb(_ACCENT_NOW)
        self._theme_restart_notice.setStyleSheet(
            f"color: {TEXT}; "
            f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
            f"border-radius: 6px; "
            f"padding: 10px 14px; "
            f"{type_qss(TYPE_CAPTION)}"
        )
        # The _OpaqueComboBox popups cache their stylesheets on first
        # showPopup. Reset the cache flag so the next open picks up
        # the new accent for the selection / hover capsules.
        for combo in self.findChildren(_OpaqueComboBox):
            combo._popup_opaque = False

    def _select_combo_by_data(self, combo: QComboBox, key: str):
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def keyPressEvent(self, e):
        # Esc closes the dialog. QDialog wires this by default, but the
        # frameless + WA_TranslucentBackground combo on KDE Wayland
        # doesn't reliably route the key event to QDialog's own
        # handler, so make the binding explicit. An open combo popup
        # still eats Esc first (closing just the popup), which is the
        # behaviour we want.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
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
            p.setBrush(QColor(*DIALOG_BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
