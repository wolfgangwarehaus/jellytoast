"""
JellyToast settings dialog. Frameless + frosted to match the main window
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
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QStackedWidget, QFormLayout,
    QComboBox, QCheckBox, QSlider,
)

from modules.icons import icon
from modules.ui_helpers import TEXT, TEXT_DIM, TEXT_FAINT, DIALOG_BODY_COLOR, enable_kde_blur
from modules.design_tokens import (
    TYPE_TITLE, TYPE_SUBHEAD, TYPE_BODY, TYPE_CAPTION, TYPE_MICRO,
    font, type_qss,
)
from modules.player_state import PlayerBus
from modules.settings import get_settings
from modules.theme import THEMES as _THEME_REGISTRY
from modules.kwin_rules import (
    install_mini_player_rule,
    remove_mini_player_rule,
    is_supported as kwin_rules_supported,
)
from modules import autostart as _autostart


# Native music surfaces the top-bar Home button can route to. Mirrors
# the keys consumed by JellyToastWindow._route_home. The same setting
# also drives the launch landing — JellyToast always boots into the
# user's chosen Home surface.
HOME_DESTINATIONS = [
    ("Albums",       "albums"),
    ("Playlists",    "playlists"),
    ("Artists",      "artists"),
    ("Songs",        "songs"),
    ("Genres",       "genres"),
    ("Suggestions",  "suggestions"),
]

# Themes the user can pick from. Entries flagged `enabled=False` show
# up in the dropdown but can't be selected — placeholder slots for
# palettes we haven't shipped yet.
_THEME_CHOICES = [
    (_THEME_REGISTRY["frosted_dark"].label, "frosted_dark", True),
    (_THEME_REGISTRY["dark"].label,         "dark",         True),
    (_THEME_REGISTRY["transparent"].label,  "transparent",  True),
    ("Light (coming soon)",                 "light",        False),
]

LYRICS_FONT_SIZES = [
    ("Smaller",  "small"),
    ("Default",  "default"),
    ("Larger",   "large"),
    ("Largest",  "largest"),
]

# (visible label, mpv "replaygain" property value)
REPLAYGAIN_MODES = [
    ("Off",                "no"),
    ("Track (per song)",   "track"),
    ("Album (preserve relative loudness)", "album"),
]


class SettingsDialog(QDialog):
    BODY_RADIUS = 14

    sign_out_requested = Signal()
    server_change_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.s = get_settings()
        self.setWindowTitle("JellyToast Settings")
        self.setFixedSize(820, 540)

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtSettingsDialog")
        self.setModal(True)

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
                outline: none;
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

        self._add_page("General",    self._build_general())
        self._add_page("Account",    self._build_account())
        self._add_page("Playback",   self._build_playback())
        self._add_page("Lyrics",     self._build_lyrics())
        self._add_page("Appearance", self._build_appearance())
        self._add_page("Display",    self._build_display())
        self._add_page("About",      self._build_about())

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def _add_page(self, title: str, content: QWidget):
        QListWidgetItem(title, self.nav)
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        v = QVBoxLayout(wrap)
        v.setContentsMargins(20, 14, 20, 14)
        v.setSpacing(0)
        v.addWidget(content)
        v.addStretch(1)
        self.stack.addWidget(wrap)

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

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; font-size: 12px;
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
        v.setSpacing(14)

        v.addWidget(self._section_header("Startup"))

        # Disk truth (whether the autostart .desktop file exists) wins
        # over the persisted flag — they can drift if the user nukes
        # the file from a file manager.
        self._autostart_check = QCheckBox("Launch JellyToast at login")
        self._autostart_check.setChecked(_autostart.is_enabled())
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        v.addWidget(self._autostart_check)

        v.addSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._home_combo = QComboBox()
        for label, key in HOME_DESTINATIONS:
            self._home_combo.addItem(label, key)
        self._select_combo_by_data(self._home_combo, self.s.home_destination)
        self._home_combo.currentIndexChanged.connect(
            lambda _: setattr(self.s, "home_destination", self._home_combo.currentData() or "albums")
        )
        form.addRow(
            self._field_label("Home button & launch open:"),
            self._home_combo,
        )
        v.addLayout(form)

        v.addSpacing(6)
        v.addWidget(self._section_header("Window"))

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
        if kwin_rules_supported():
            self._keep_above_check = QCheckBox(
                "Keep mini player on top (KDE Wayland)"
            )
            self._keep_above_check.setChecked(self.s.mini_player_keep_above)
            self._keep_above_check.toggled.connect(self._on_keep_above_toggled)
            v.addWidget(self._keep_above_check)

            keep_above_note = QLabel(
                "Installs a KWin window rule scoped to JellyToast's mini "
                "player. Stored in ~/.config/kwinrulesrc; toggle off to "
                "remove it cleanly."
            )
            keep_above_note.setWordWrap(True)
            keep_above_note.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 2px 0 0 22px;"
            )
            v.addWidget(keep_above_note)
        return page

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

    # ── Page: Account ──────────────────────────────────────────────────
    def _build_account(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        v.addWidget(self._section_header("Server"))
        url = self.s.server_url or "Not configured"
        url_label = QLabel(url)
        url_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(url_label)

        change_btn = QPushButton("Change server URL…")
        change_btn.setObjectName("ghost")
        change_btn.setFixedWidth(220)
        change_btn.clicked.connect(self.server_change_requested.emit)
        v.addWidget(change_btn)

        v.addSpacing(14)
        v.addWidget(self._section_header("Signed in"))

        username = self.s.username
        signed_in = bool(self.s.access_token)
        if signed_in:
            text = f"Signed in as {username}." if username else "Signed in."
        else:
            text = "Not signed in."
        status = QLabel(text)
        status.setWordWrap(True)
        status.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(status)

        signout_btn = QPushButton("Sign out")
        signout_btn.setObjectName("ghost")
        signout_btn.setFixedWidth(220)
        signout_btn.clicked.connect(self.sign_out_requested.emit)
        v.addWidget(signout_btn)

        note = QLabel(
            "Signing out revokes this device's session on the server "
            "and returns you to the JellyToast sign-in screen."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 6px;")
        v.addWidget(note)
        return page

    # ── Page: Playback ─────────────────────────────────────────────────
    def _build_playback(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("ReplayGain"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._rg_combo = QComboBox()
        for label, key in REPLAYGAIN_MODES:
            self._rg_combo.addItem(label, key)
        self._select_combo_by_data(self._rg_combo, self.s.replaygain)
        self._rg_combo.currentIndexChanged.connect(self._on_replaygain_changed)
        form.addRow(self._field_label("Mode:"), self._rg_combo)
        v.addLayout(form)

        note = QLabel(
            "Normalises loudness across tracks using ReplayGain tags written "
            "by your tagger. Track mode evens out every song; album mode "
            "preserves an album's intended dynamics. Changes apply instantly "
            "to the next decode."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)
        return page

    def _on_replaygain_changed(self):
        mode = self._rg_combo.currentData() or "no"
        PlayerBus.get().replaygain_changed.emit(mode)

    # ── Page: Lyrics ───────────────────────────────────────────────────
    def _build_lyrics(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("Font size"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._lyrics_size_combo = QComboBox()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        form.addRow(self._field_label("Lyrics size:"), self._lyrics_size_combo)
        v.addLayout(form)

        note = QLabel(
            "Sets both the active and surrounding lyric line sizes on the "
            "now-playing page. Smaller fits more lines when the window is "
            "compact; Larger reads more comfortably at full width."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)
        return page

    def _on_lyrics_size_changed(self):
        key = self._lyrics_size_combo.currentData() or "default"
        self.s.lyrics_font_size = key
        PlayerBus.get().lyrics_font_size_changed.emit(key)

    # ── Page: Appearance ───────────────────────────────────────────────
    def _build_appearance(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("Theme"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._theme_combo = QComboBox()
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
        form.addRow(self._field_label("Mode:"), self._theme_combo)
        v.addLayout(form)

        # Restart notice — visible only after the user picks a theme
        # different from the one currently rendering.
        self._theme_restart_notice = QLabel(
            "Restart JellyToast to apply the new theme."
        )
        self._theme_restart_notice.setWordWrap(True)
        self._theme_restart_notice.setStyleSheet(
            f"color: {TEXT}; background: rgba(0,164,220,0.16);"
            f"border-radius: 6px; padding: 8px 12px; {type_qss(TYPE_CAPTION)}"
        )
        self._theme_restart_notice.hide()
        v.addWidget(self._theme_restart_notice)

        note = QLabel(
            "Frosted dark, solid dark, and transparent are wired up. "
            "Light is coming in a future build."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)
        return page

    def _on_theme_changed(self):
        chosen = self._theme_combo.currentData() or "frosted_dark"
        self.s.theme_mode = chosen
        self._theme_restart_notice.setVisible(chosen != self._initial_theme)

    # ── Page: Display ──────────────────────────────────────────────────
    def _build_display(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("Scaling"))

        v.addLayout(self._slider_row("Font size", 100))
        v.addLayout(self._slider_row("UI scale",  100))

        note = QLabel(
            "Display scaling controls are placeholders. Wire-up will follow "
            "once the theme system supports per-component metrics."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 8px;")
        v.addWidget(note)
        return page

    # ── Page: About ────────────────────────────────────────────────────
    def _build_about(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        title = QLabel("JellyToast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        v.addWidget(title)

        version = QLabel("v0.1.0")
        version.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(version)

        v.addSpacing(8)

        desc = QLabel(
            "Native desktop client for Jellyfin — Jellyfin Web inside a Qt "
            "shell, with bit-perfect mpv playback, MPRIS2, system tray, "
            "floating mini player, and Chromecast/AirPlay casting."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)} line-height: 1.5;")
        v.addWidget(desc)
        return page

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

    def _slider_row(self, label_text: str, value: int) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        label.setFixedWidth(110)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(85, 150)
        slider.setValue(value)
        slider.setEnabled(False)
        pct = QLabel(f"{value}%")
        pct.setFixedWidth(46)
        pct.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(pct)
        return row

    def _select_combo_by_data(self, combo: QComboBox, key: str):
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            path = QPainterPath()
            path.addRoundedRect(
                0.0, 0.0, float(self.width()), float(self.height()),
                self.BODY_RADIUS, self.BODY_RADIUS,
            )
            p.setBrush(QColor(*DIALOG_BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(50, lambda: enable_kde_blur(self))
