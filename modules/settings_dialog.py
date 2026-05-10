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
from modules.ui_helpers import BORDER, TEXT, TEXT_DIM, TEXT_FAINT, DIALOG_BODY_COLOR, enable_kde_blur
from modules.design_tokens import (
    TYPE_TITLE, TYPE_SUBHEAD, TYPE_BODY, TYPE_CAPTION, TYPE_MICRO,
    font, type_qss,
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

# (visible label, audio_quality setting value)
# "original" maps to DirectStream (Jellyfin) / format=raw (Subsonic).
# Numeric values are kbps and trigger server-side transcode.
AUDIO_QUALITIES = [
    ("Original (no transcode)", "original"),
    ("320 kbps",                "320"),
    ("256 kbps",                "256"),
    ("192 kbps",                "192"),
    ("128 kbps",                "128"),
    ("96 kbps",                 "96"),
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

        # Dialog-level styling for QComboBox + its popup. The dialog
        # uses WA_TranslucentBackground for the rounded card look, and
        # without explicit popup styling the Theme combo / ReplayGain
        # combo / etc. inherit that translucency — the popup ghosts
        # over the field below it. An opaque background and a clear
        # down-arrow icon (Qt's default arrow renders invisible against
        # the dark surface here) fix both issues for every combo in
        # the dialog without per-combo styling.
        from modules.icons import icon_svg_path
        chevron_path = icon_svg_path("chevron_down", TEXT_DIM)
        chevron_url = chevron_path.replace("\\", "/")
        self.setStyleSheet(f"""
            QComboBox {{
                background: rgba(255,255,255,0.06);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 6px 12px;
                {type_qss(TYPE_BODY)}
                min-height: 22px;
            }}
            QComboBox:focus {{
                border-color: rgba(255,255,255,0.32);
            }}
            QComboBox:disabled {{
                color: {TEXT_FAINT};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 26px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox::down-arrow {{
                image: url({chevron_url});
                width: 12px;
                height: 12px;
            }}
            QComboBox QAbstractItemView {{
                background: rgb(20, 22, 26);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px 0px;
                outline: 0;
                selection-background-color: rgba(255,255,255,0.10);
                selection-color: {TEXT};
            }}
            QComboBox QAbstractItemView::item {{
                padding: 7px 14px;
                min-height: 22px;
            }}
        """)

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
        self._add_page("General",    self._build_general())
        self._add_page("Playback",   self._build_playback())
        self._add_page("Library",    self._build_library())
        # Display rolls in what was previously Appearance + Lyrics —
        # all three pages controlled how the UI looks, so they live
        # under one nav entry now.
        self._add_page("Display",    self._build_display())
        self._add_page("Hotkeys",    self._build_hotkeys())
        self._add_page("Scrobbling", self._build_scrobbling())

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

        # About button (info circle) — opens a small overlay with the
        # name + version + blurb. Replaces the prior About settings page.
        about_btn = QPushButton()
        about_btn.setIcon(icon("info"))
        about_btn.setIconSize(QSize(18, 18))
        about_btn.setFixedSize(32, 28)
        about_btn.setToolTip("About JellyToast")
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
        v.setSpacing(8)

        # ── Server (folded in from the old Account page) ───────────────
        # Most surveyed players (Supersonic, Apple Music) treat server
        # / account as a row in General rather than its own peer page.
        v.addWidget(self._section_header("Server"))
        url = self.s.server_url or "Not configured"
        url_label = QLabel(url)
        url_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_label.setWordWrap(True)
        v.addWidget(url_label)

        username = self.s.username
        signed_in = bool(self.s.access_token)
        if signed_in:
            status_text = (
                f"Signed in as {username}." if username else "Signed in."
            )
        else:
            status_text = "Not signed in."
        status = QLabel(status_text)
        status.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} padding-top: 2px;")
        v.addWidget(status)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 6, 0, 0)
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
        self._autostart_check = QCheckBox("Launch JellyToast at login")
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

        v.addSpacing(18)

        # Home destination at the bottom — the only form-style row on
        # this page, separated from the checkbox stack.
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

    # ── Page: Playback ─────────────────────────────────────────────────
    def _build_playback(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        # ── Streaming quality ─────────────────────────────────────────
        v.addWidget(self._section_header("Streaming"))

        sform = QFormLayout()
        sform.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        sform.setHorizontalSpacing(16)
        sform.setVerticalSpacing(10)

        self._quality_combo = QComboBox()
        for label, key in AUDIO_QUALITIES:
            self._quality_combo.addItem(label, key)
        self._select_combo_by_data(self._quality_combo, self.s.audio_quality or "original")
        self._quality_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s, "audio_quality",
                self._quality_combo.currentData() or "original",
            )
        )
        sform.addRow(self._field_label("Quality:"), self._quality_combo)
        v.addLayout(sform)

        quality_note = QLabel(
            "Original passes the source file through untouched (DirectStream / "
            "format=raw). Picking a kbps value asks the server to transcode "
            "down — useful on slow networks. Applies to the next track."
        )
        quality_note.setWordWrap(True)
        quality_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(quality_note)

        v.addSpacing(14)

        # ── ReplayGain ─────────────────────────────────────────────────
        v.addWidget(self._section_header("ReplayGain"))

        rgform = QFormLayout()
        rgform.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        rgform.setHorizontalSpacing(16)
        rgform.setVerticalSpacing(10)

        self._rg_combo = QComboBox()
        for label, key in REPLAYGAIN_MODES:
            self._rg_combo.addItem(label, key)
        self._select_combo_by_data(self._rg_combo, self.s.replaygain)
        self._rg_combo.currentIndexChanged.connect(self._on_replaygain_changed)
        rgform.addRow(self._field_label("Mode:"), self._rg_combo)
        v.addLayout(rgform)

        note = QLabel(
            "Normalises loudness across tracks using ReplayGain tags written "
            "by your tagger. Track mode evens out every song; album mode "
            "preserves an album's intended dynamics. Changes apply instantly "
            "to the next decode."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)

        v.addSpacing(14)

        # ── Behavior toggles ──────────────────────────────────────────
        v.addWidget(self._section_header("Behavior"))

        self._gapless_check = QCheckBox("Gapless playback")
        self._gapless_check.setChecked(self.s.gapless)
        self._gapless_check.toggled.connect(
            lambda val: setattr(self.s, "gapless", val)
        )
        v.addWidget(self._gapless_check)

        gapless_note = QLabel(
            "Pre-buffer the next queued track so transitions are seamless. "
            "Turn off if you'd rather not hold a second decoder warm in the "
            "background."
        )
        gapless_note.setWordWrap(True)
        gapless_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 2px 0 0 22px;"
        )
        v.addWidget(gapless_note)

        self._media_keys_check = QCheckBox(
            "OS media keys & system media controls"
        )
        self._media_keys_check.setChecked(self.s.media_controls_enabled)
        self._media_keys_check.toggled.connect(
            lambda val: setattr(self.s, "media_controls_enabled", val)
        )
        v.addWidget(self._media_keys_check)

        mk_note = QLabel(
            "Lets keyboard play/pause/next keys and the desktop media-control "
            "widget (KDE Plasma media bar, GNOME Shell, etc.) drive playback "
            "via MPRIS. Restart JellyToast for changes to take effect."
        )
        mk_note.setWordWrap(True)
        mk_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 2px 0 0 22px;"
        )
        v.addWidget(mk_note)
        return page

    def _on_replaygain_changed(self):
        mode = self._rg_combo.currentData() or "no"
        PlayerBus.get().replaygain_changed.emit(mode)

    # ── Page: Library ──────────────────────────────────────────────────
    def _build_library(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("Loading"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        # Page-size dropdown — `data=0` is the "load all" sentinel
        # (LibraryGrid chains pages of 500 internally so Subsonic's
        # 500-per-call cap doesn't truncate big libraries).
        self._page_size_combo = QComboBox()
        for label, key in (
            ("Load all at once", 0),
            ("100 per page",     100),
            ("200 per page",     200),
            ("500 per page",     500),
            ("1000 per page",    1000),
        ):
            self._page_size_combo.addItem(label, key)
        self._select_combo_by_data(
            self._page_size_combo, self.s.library_page_size,
        )
        self._page_size_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s, "library_page_size",
                int(self._page_size_combo.currentData() or 200),
            )
        )
        form.addRow(
            self._field_label("Album / artist grids:"),
            self._page_size_combo,
        )
        v.addLayout(form)

        note = QLabel(
            "Smaller pages paint faster on cold-launch but require "
            "scrolling to see more. \"Load all\" fetches the entire "
            "library up-front in chunks. Changes apply on the next "
            "browse."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(note)

        v.addSpacing(6)
        v.addWidget(self._section_header("Tiles"))

        self._cover_prefetch_check = QCheckBox(
            "Pre-load covers for tiles outside the viewport"
        )
        self._cover_prefetch_check.setChecked(self.s.library_cover_prefetch)
        self._cover_prefetch_check.toggled.connect(
            lambda val: setattr(self.s, "library_cover_prefetch", val)
        )
        v.addWidget(self._cover_prefetch_check)

        prefetch_note = QLabel(
            "On (default), covers warm in the background after the "
            "grid renders so a later scroll is instant. Off keeps "
            "covers viewport-only — fewer requests, useful on metered "
            "connections."
        )
        prefetch_note.setWordWrap(True)
        prefetch_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 2px 0 0 22px;"
        )
        v.addWidget(prefetch_note)

        self._tile_fade_check = QCheckBox(
            "Fade tiles in as covers load"
        )
        self._tile_fade_check.setChecked(self.s.library_tile_fade)
        self._tile_fade_check.toggled.connect(
            lambda val: setattr(self.s, "library_tile_fade", val)
        )
        v.addWidget(self._tile_fade_check)

        # ── Cache ─────────────────────────────────────────────────────
        # Surfaces the cover-art disk cache (the four-tier
        # L1/L2-mem/L2-disk/L3 in ui_helpers + image_cache). The cap is
        # configured in code (200 MB LRU); we expose a Clear button +
        # current footprint label so a user with a stale cache after
        # switching servers can wipe it without nuking the config dir.
        v.addSpacing(8)
        v.addWidget(self._section_header("Cache"))

        self._cache_size_label = QLabel("Calculating…")
        self._cache_size_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}"
        )
        v.addWidget(self._cache_size_label)
        # Compute on dialog open (cheap — directory walk over a few
        # hundred files at most).
        QTimer.singleShot(0, self._refresh_cache_size_label)

        clear_cache_btn = QPushButton("Clear cover-art cache")
        clear_cache_btn.setObjectName("ghost")
        clear_cache_btn.setFixedWidth(220)
        clear_cache_btn.clicked.connect(self._on_clear_cache)
        v.addWidget(clear_cache_btn)

        cache_note = QLabel(
            "Wipes every per-size pixmap and raw-source variant from disk. "
            "Useful after switching servers if old artwork is sticking around. "
            "Tiles will re-fetch as you browse."
        )
        cache_note.setWordWrap(True)
        cache_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(cache_note)
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
            self._cache_size_label.setText(
                f"On disk: {mb:.1f} MB across {count} files"
            )
        except Exception:
            self._cache_size_label.setText("On disk: unavailable")

    def _on_clear_cache(self):
        from modules import image_cache as _ic
        _ic.clear()
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
        v.setSpacing(14)

        # ── Theme ──────────────────────────────────────────────────────
        v.addWidget(self._section_header("Theme"))

        theme_form = QFormLayout()
        theme_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        theme_form.setHorizontalSpacing(16)
        theme_form.setVerticalSpacing(10)

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
        theme_form.addRow(self._field_label("Mode:"), self._theme_combo)
        v.addLayout(theme_form)

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

        theme_note = QLabel(
            "Frosted dark, solid dark, and transparent are wired up. "
            "Light is coming in a future build."
        )
        theme_note.setWordWrap(True)
        theme_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(theme_note)

        v.addSpacing(18)

        # ── Lyrics ─────────────────────────────────────────────────────
        v.addWidget(self._section_header("Lyrics"))

        lyrics_form = QFormLayout()
        lyrics_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        lyrics_form.setHorizontalSpacing(16)
        lyrics_form.setVerticalSpacing(10)

        self._lyrics_size_combo = QComboBox()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        lyrics_form.addRow(self._field_label("Lyrics size:"), self._lyrics_size_combo)
        v.addLayout(lyrics_form)

        lyrics_note = QLabel(
            "Sets both the active and surrounding lyric line sizes on the "
            "now-playing page. Smaller fits more lines when the window is "
            "compact; Larger reads more comfortably at full width."
        )
        lyrics_note.setWordWrap(True)
        lyrics_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;"
        )
        v.addWidget(lyrics_note)

        v.addSpacing(18)

        # ── Scaling (placeholder) ──────────────────────────────────────
        v.addWidget(self._section_header("Scaling"))

        v.addLayout(self._slider_row("Font size", 100))
        v.addLayout(self._slider_row("UI scale",  100))

        scaling_note = QLabel(
            "Display scaling controls are placeholders. Wire-up will follow "
            "once the theme system supports per-component metrics."
        )
        scaling_note.setWordWrap(True)
        scaling_note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 8px;"
        )
        v.addWidget(scaling_note)
        return page

    def _on_lyrics_size_changed(self):
        key = self._lyrics_size_combo.currentData() or "default"
        self.s.lyrics_font_size = key
        PlayerBus.get().lyrics_font_size_changed.emit(key)

    def _on_theme_changed(self):
        chosen = self._theme_combo.currentData() or "frosted_dark"
        self.s.theme_mode = chosen
        self._theme_restart_notice.setVisible(chosen != self._initial_theme)

    # ── Page: Hotkeys ──────────────────────────────────────────────────
    # Read-only list of every keyboard shortcut the app responds to.
    # Customization (rebinding) is a future enhancement; for now the
    # page exists to make discoverable what's already wired in
    # JellyToastWindow.__init__ — Ctrl+F, /, Ctrl+Shift+L, etc.
    def _build_hotkeys(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        v.addWidget(self._section_header("Navigation"))
        v.addLayout(self._hotkey_row("Search",        "Ctrl+F  ·  /"))
        v.addLayout(self._hotkey_row("All music",     "Ctrl+Shift+L"))

        v.addSpacing(8)
        v.addWidget(self._section_header("Playback"))
        v.addLayout(self._hotkey_row(
            "Play / Pause", "Media Play (system media key)",
        ))
        v.addLayout(self._hotkey_row(
            "Next track",   "Media Next (system media key)",
        ))
        v.addLayout(self._hotkey_row(
            "Previous track", "Media Previous (system media key)",
        ))

        v.addSpacing(8)
        note = QLabel(
            "Media keys flow through the desktop's MPRIS service — "
            "disable \"OS media keys & system media controls\" on the "
            "Playback page if you'd rather another app receive them. "
            "Hotkey customization is on the roadmap."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
        )
        v.addWidget(note)
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
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        v.addWidget(self._section_header("Coming soon"))

        body = QLabel(
            "Last.fm and ListenBrainz scrobbling will live here in a "
            "future build. For now, your Jellyfin or Navidrome server's "
            "own play-history reports the same data — open the server "
            "admin to see what's been played."
        )
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(body)
        return page

    # ── About overlay (replaces the old About page) ────────────────────
    def _show_about(self):
        """Modal info dialog launched from the titlebar info button.
        Lighter than a full settings page since the content is just
        version + blurb — most surveyed music players (Strawberry,
        Supersonic, Feishin, Plexamp) put About in a small dialog
        rather than a settings tab."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About JellyToast")
        dlg.setFixedWidth(380)
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(8)

        title = QLabel("JellyToast")
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
