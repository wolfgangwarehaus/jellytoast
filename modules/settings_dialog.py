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

import json

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpacerItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


class _AccentSwatch(QPushButton):
    """Circular accent-picker swatch painted by hand.

    QSS ``border-radius`` on a small QPushButton has a long-standing
    aliasing bug — Qt's stylesheet painter doesn't antialias the
    rounded edge, leaving visible ``dots`` / stair-stepping at each
    45° point on the perimeter. Custom-painting with QPainter +
    RenderHint.Antialiasing produces a smooth circle.

    The swatch carries its own fill colour and selection state; the
    parent dialog calls :meth:`set_accent_state` after the user picks
    a new accent so every swatch refreshes its ring without rebuilding
    the row."""

    def __init__(self, hex_color: str, parent=None):
        super().__init__(parent)
        self._fill = QColor(hex_color)
        self._hovered = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(28, 28)

    def set_accent_state(self, hex_color: str, checked: bool) -> None:
        """Refresh fill + checked from the live picker. Hex doesn't
        actually change today (presets are fixed), but accepting it
        keeps the call symmetric with the QSS-builder it replaces and
        leaves room for future user-custom accents."""
        self._fill = QColor(hex_color)
        self.setChecked(checked)
        self.update()

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        from PySide6.QtGui import QPen

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Fixed inset for every state — the fill stays the same
            # diameter whether the swatch is idle, hovered, or selected.
            # The earlier version inset by half the stroke width, so a
            # 2-px selected ring shrank the visible fill by 1 px relative
            # to the 1-px idle ring; the selected swatch read as smaller
            # than its siblings. Now the ring sits over the fill edge
            # instead of biting into the inset.
            r = self.rect()
            cx = r.center().x() + 0.5
            cy = r.center().y() + 0.5
            radius = min(r.width(), r.height()) / 2.0 - 1.5

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._fill)
            p.drawEllipse(QPointF(cx, cy), radius, radius)

            # Selection / hover ring — thin in every state so the fill
            # dominates the visual. Selected uses near-opaque ink as a
            # crisp outline; hover is slightly stronger than idle.
            if self.isChecked():
                ring_color = QColor(0, 0, 0, int(0.85 * 255))
                ring_w = 1.5
            elif self._hovered:
                ring_color = QColor(0, 0, 0, int(0.55 * 255))
                ring_w = 1.0
            else:
                ring_color = QColor(0, 0, 0, int(0.18 * 255))
                ring_w = 1.0
            pen = QPen(ring_color)
            pen.setWidthF(ring_w)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
        finally:
            p.end()


# Selector ("dropdown") used to live here as ``_Selector``; it moved
# to modules/selector.py so the login view (and any future surfaces)
# can reuse it. Imported below as ``_Selector`` so the local
# call-sites in this file keep working without a rename pass.
from modules import autostart as _autostart
from modules.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_SUBHEAD,
    TYPE_TITLE,
    font,
    type_qss,
)
from modules.icons import icon
from modules.keep_above import (
    ABOUT_DIALOG_WINDOW_TITLE,
    install_mini_player_rule,
    remove_mini_player_rule,
)
from modules.keep_above import (
    is_supported as keep_above_supported,
)
from modules.player_state import PlayerBus
from modules.selector import (
    Selector as _Selector,  # noqa: E402  (deliberate post-class import for layout)
)
from modules.settings import get_settings
from modules.theme import THEMES as _THEME_REGISTRY
from modules.theme import _hex_to_rgb
from modules.ui_helpers import (
    ACCENT,
    BORDER,
    DISABLED_FG,
    ERROR_FG,
    POPUP_OPAQUE_FILL,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WASH_HOVER,
    ink_alpha,
)

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
# palettes we haven't shipped yet. Dark family first, then light.
_THEME_CHOICES = [
    (_THEME_REGISTRY["frosted_dark"].label, "frosted_dark", True),
    (_THEME_REGISTRY["dark"].label, "dark", True),
    (_THEME_REGISTRY["transparent"].label, "transparent", True),
    (_THEME_REGISTRY["frosted_light"].label, "frosted_light", True),
    (_THEME_REGISTRY["light"].label, "light", True),
    (_THEME_REGISTRY["transparent_light"].label, "transparent_light", True),
]

LYRICS_FONT_SIZES = [
    ("Smaller", "small"),
    ("Default", "default"),
    ("Larger", "large"),
    ("Largest", "largest"),
]

# (visible label, mpv "replaygain" property value)
# Labels kept short for combo-box rhythm; the longer parentheticals
# ("per song" / "preserve relative loudness") moved to combo tooltips
# so the box width matches the AUDIO_QUALITIES combo without losing
# the explanatory copy.
REPLAYGAIN_MODES = [
    ("Off", "no"),
    ("Track", "track"),
    ("Album", "album"),
]

# (visible label, audio_quality setting value)
# "original" maps to DirectStream (Jellyfin) / format=raw (Subsonic).
# Numeric values are kbps and trigger server-side transcode.
AUDIO_QUALITIES = [
    ("Original", "original"),
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
    # (radio label, faint hint suffix or None, setting value)
    ("Auto", None, "auto"),
    ("Network cast", "(normal mode)", "direct"),
    ("Route locally", "(offline, special case)", "proxy"),
]


class _AboutDialog(QDialog):
    """Borderless frosted "About jellytoast" popup. Custom titlebar
    (drag + close button), rounded translucent body painted with the
    lifted tone (``popup_paint_qcolor`` — same colour as tooltips and
    the volume slider popup) so it reads as one of the elevated
    surface family instead of an opaque dialog. On KDE Wayland the
    KWin noborder rule (modules.keep_above, scoped by
    ``ABOUT_DIALOG_WINDOW_TITLE``) strips the server decoration; off
    KDE Wayland ``FramelessWindowHint`` does the same."""

    BODY_RADIUS = RADIUS_WINDOW

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(ABOUT_DIALOG_WINDOW_TITLE)
        self.setFixedWidth(380)
        self.setModal(True)

        from modules.platform_compat import is_kde_wayland

        # Same flag dance as SettingsDialog: KDE Wayland keeps the
        # server-side decoration so KWin blur survives drags, and a
        # noborder rule hides the chrome. Everywhere else we go
        # plain frameless.
        flags = Qt.WindowType.Dialog
        if not is_kde_wayland():
            flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtAboutDialog")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(20, 8, 20, 18)
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
        close_btn.clicked.connect(self.accept)
        v.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)

        outer.addWidget(body, 1)

    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(40)
        tb.setObjectName("jtAboutTitle")
        tb.setStyleSheet("""
            QWidget#jtAboutTitle { background: transparent; }
            QWidget#jtAboutTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(16, 0, 8, 0)
        h.setSpacing(10)

        title = QLabel("About jellytoast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_CAPTION)}")
        h.addWidget(title)
        h.addStretch(1)

        # Icon-based close button — matches the main-window top-bar
        # win_close glyph + WASH_HOVER pill, so all close buttons in
        # the app read identically (text-glyph ✕ buttons on
        # KDE/Plasma pick up the default-button outline and read
        # heavier than icon buttons). autoDefault off so Qt doesn't
        # treat it as the dialog's default action and paint a
        # focus-ring frame even when unhovered.
        close_btn = QPushButton()
        close_btn.setIcon(icon("win_close"))
        close_btn.setIconSize(QSize(16, 16))
        close_btn.setFixedSize(32, 28)
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.setFlat(True)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 6px;
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
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

    def keyPressEvent(self, e):  # noqa: N802 — Qt naming
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):  # noqa: N802 — Qt naming
        """Paint the dialog body as a rounded rect filled with the
        active theme's ``popup_opaque_fill`` token (translucent on
        frosted themes, opaque on solid). Pairs with the KWin
        noborder rule + compositor blur to read as one of the
        elevated frosted surfaces."""
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            from modules.ui_helpers import popup_paint_qcolor

            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            p.setBrush(popup_paint_qcolor())
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    def showEvent(self, e):  # noqa: N802 — Qt naming
        super().showEvent(e)
        # Install compositor blur on frosted themes so the translucent
        # popup_paint_qcolor fill composites over a blurred wallpaper
        # backdrop — the same lifted-glass look the tooltip uses.
        try:
            from modules import blur as _blur
            from modules.theme import get_active_theme

            if get_active_theme().blur:
                _blur.apply(self, True, corner_radius=self.BODY_RADIUS)
        except Exception:
            pass


class SettingsDialog(QDialog):
    # Matched to the host-OS window-corner radius (see RADIUS_WINDOW).
    BODY_RADIUS = RADIUS_WINDOW

    sign_out_requested = Signal()
    # Carries the new URL the user committed inline. Main window
    # reads it directly rather than re-prompting — the field above
    # the button IS the prompt.
    server_change_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.s = get_settings()
        # Distinct title so the KWin `noborder` rule (modules.keep_above)
        # can scope-match this dialog without catching the main window.
        from modules.keep_above import SETTINGS_WINDOW_TITLE

        self.setWindowTitle(SETTINGS_WINDOW_TITLE)
        # 820→720: pulls a chunk of empty real-estate off the right
        # side of the dialog. Playback is the widest page (EQ band
        # grid + trailing Save/Delete buttons); the rest of the
        # pages adapt to the tighter content width page by page.
        # Height 540→620: lets the Playback page (Audio / Crossfade /
        # EQ / Shuffle) sit fully visible without invoking the page
        # scroller.
        self.setFixedSize(720, 620)

        # Independent top-level window (not ``Qt.Dialog``): KWin treats
        # ``Qt.Dialog`` as transient-for-parent, which pins it above
        # the main window and renders the main window as a faded
        # "background" — fine for a modal dialog, wrong for a side-
        # by-side configuration surface where the user wants to tweak
        # colours and watch the main UI react in real time. With
        # ``Qt.Window`` + ``NonModal`` the user can raise either
        # window, alt-tab between them, and the main UI keeps its
        # active-window styling. Mini player is also a separate
        # top-level so all three coexist as peers.
        # Frameless everywhere EXCEPT KDE Wayland, where the dialog is
        # server-side-decorated and a KWin `noborder` rule
        # (modules.keep_above.install_noborder_rules) strips the chrome.
        # KWin's blur effect drops blur for *undecorated* windows while
        # they move, so a frameless dialog flickers when dragged — a
        # decorated + noborder window keeps its blur.
        from modules.platform_compat import is_kde_wayland

        _dlg_flags = Qt.WindowType.Window
        if not is_kde_wayland():
            _dlg_flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(_dlg_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtSettingsDialog")
        self.setWindowModality(Qt.WindowModality.NonModal)

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
        # 2→12 gap between nav column and the page stack: gives the
        # right pane visible room rather than touching the sidebar's
        # selected-row chip.
        body_h.setSpacing(12)

        self.nav = QListWidget()
        # 170→128 over two passes: trims negative space after the
        # longest label ("Scrobbling") to a snug-but-not-cramped
        # column. ~12 px of breathing room past "Scrobbling" — any
        # tighter and the right-padded item chip starts grazing the
        # text.
        self.nav.setFixedWidth(128)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setStyleSheet(self._nav_qss())
        body_h.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        body_h.addWidget(self.stack, 1)

        outer.addWidget(body, 1)

        # Pages build LAZILY — `_add_page` stores the builder callable;
        # the content widget isn't constructed until the user first
        # navigates to that page. Building all nine up front is ~900
        # widgets, and since the theme combo lives on the Display page
        # the dialog is always open during a live theme switch — so
        # every switch would otherwise re-style those 900 widgets
        # (~150ms). Lazy pages keep a switch closer to ~15ms and also
        # make the dialog itself open faster.
        #
        # Account is folded into General as a Server section; About
        # moved to the titlebar info button. Downloads / Colors expand
        # to fill the page (they hold scrolling lists); Colors is the
        # power-user colour editor, last so day-to-day pages sit first.
        self._page_builders: list = []
        # Indices whose content widget has been built. A light↔dark
        # theme switch must tear these down + rebuild (see
        # _rebuild_pages_for_theme); unbuilt pages stay lazy.
        self._built_pages: set[int] = set()
        # General is ``expand=True`` so it doesn't get wrapped in a
        # QScrollArea. Its content is sized to fit the 820×540 dialog
        # exactly — without this it overflowed by ~10 px and rendered
        # a hair-trigger scrollbar that the user could nudge a few
        # pixels each way, which read as the page jiggling.
        self._add_page("General", self._build_general, expand=True)
        self._add_page("Playback", self._build_playback)
        self._add_page("Casting", self._build_casting)
        self._add_page("Downloads", self._build_downloads, expand=True)
        self._add_page("Display", self._build_display)
        self._add_page("Hotkeys", self._build_hotkeys)
        self._add_page("Scrobbling", self._build_scrobbling)
        self._add_page("Colors", self._build_colors, expand=True)
        # Colors page is power-user only — reachable from the Display
        # page's "Customize" button. Hide its left-nav row so the side
        # bar stays focused on the day-to-day pages. setHidden leaves
        # the stack index intact so _jump_to_colors_page's setCurrentRow
        # still flips to it.
        self.nav.item(self.nav.count() - 1).setHidden(True)

        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)  # fires _on_nav_changed → builds page 0

        # Live-apply: when the Colors page (or accent picker) fires
        # theme_changed, re-stamp every accent-baked surface in the
        # dialog — combo borders, ghost-button outlines, restart
        # notice banner, EQ slider handles. _on_accent_picked already
        # runs this directly; this hook makes Colors-page slider drag
        # behave identically.
        try:
            PlayerBus.get().theme_changed.connect(
                self._reapply_dialog_accent_styling
            )
        except Exception:
            pass

    def _nav_qss(self) -> str:
        """Sidebar nav QSS — bakes TEXT / TEXT_DIM / ink_alpha(), so
        rebuilt on a live theme switch (see `_rebuild_pages_for_theme`,
        which re-applies it — the nav isn't a page)."""
        return f"""
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
                background: {ink_alpha(0.05)};
                color: {TEXT};
            }}
            QListWidget::item:selected {{
                background: {ink_alpha(0.10)};
                color: {TEXT};
            }}
        """

    def _add_page(self, title: str, builder, expand: bool = False):
        """Register a settings page. ``builder`` is a zero-arg callable
        returning the page's content widget — invoked lazily on first
        navigation (``_on_nav_changed``), not here. An empty wrapper is
        added to the stack now so nav indices line up.

        Non-``expand`` pages are wrapped in a QScrollArea so a page
        taller than the fixed dialog (Playback's EQ stack, the Casting
        device list) scrolls instead of clipping off the bottom edge.
        ``expand`` pages manage their own vertical space (they hold
        their own scrolling lists) and are added to the stack as-is."""
        QListWidgetItem(title, self.nav)
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        v = QVBoxLayout(wrap)
        # Top = 15 to line the first section header (e.g. SERVER) up
        # with the nav's first item text (e.g. "General"). The math:
        # QListWidget QSS sets padding 4 + per-item margin 2 +
        # per-item padding-top 9 = 15 from the body's top edge down
        # to the nav text. Matching that here makes the two columns
        # read as peer rows starting at the same baseline rather
        # than the right pane drifting up or down.
        v.setContentsMargins(20, 15, 20, 14)
        v.setSpacing(0)
        if expand:
            self.stack.addWidget(wrap)
        else:
            scroller = QScrollArea()
            scroller.setWidget(wrap)
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QFrame.Shape.NoFrame)
            scroller.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            scroller.setStyleSheet("background: transparent;")
            scroller.viewport().setStyleSheet("background: transparent;")
            self.stack.addWidget(scroller)
        # (builder, expand, wrapper-layout) — wrapper-layout is where the
        # built content is added; the scroll wrapping is transparent to
        # _on_nav_changed / _rebuild_pages_for_theme.
        self._page_builders.append((builder, expand, v))

    def _on_nav_changed(self, idx: int):
        """Build the page's content on first visit, then show it."""
        if 0 <= idx < len(self._page_builders) and idx not in self._built_pages:
            builder, expand, layout = self._page_builders[idx]
            content = builder()
            if expand:
                # Page owns its vertical space (a scrolling list) —
                # let it fill rather than pinning it to the top.
                layout.addWidget(content, 1)
            else:
                layout.addWidget(content)
                layout.addStretch(1)
            self._built_pages.add(idx)
            # Force Qt to polish the lazy-built widgets against the
            # dialog's stylesheet immediately. Without this, combos
            # constructed AFTER the dialog was shown sometimes don't
            # get the dialog-level QSS pass on first show — the user
            # symptom was Playback dropdowns "not working" on first
            # click until you navigated away and back, which is just
            # Qt's natural polish-on-hide-show kicking in.
            content.ensurePolished()
            for child in content.findChildren(QComboBox):
                child.ensurePolished()
        self.stack.setCurrentIndex(idx)

    def _rebuild_pages_for_theme(self):
        """Tear down + rebuild every built page after a light↔dark
        theme switch.

        Pages bake the text-token colours (TEXT / TEXT_DIM /
        TEXT_FAINT / ERROR_FG) into label stylesheets at build time.
        This module imported those names by value, so `refresh_theme()`
        updating `ui_helpers` doesn't reach them and already-built
        pages keep stale colours — invisible until now because the
        three dark themes shared one text palette. Rebind the module
        constants from the refreshed `ui_helpers`, drop the built
        pages, and rebuild the visible one; the rest stay lazy.

        Safe to call from the theme combo's own signal handler only
        when deferred (QTimer.singleShot) — the combo lives on the
        Display page this tears down.
        """
        global TEXT, TEXT_DIM, TEXT_FAINT, ERROR_FG, BORDER, POPUP_OPAQUE_FILL
        from modules import ui_helpers as _u

        TEXT, TEXT_DIM, TEXT_FAINT = _u.TEXT, _u.TEXT_DIM, _u.TEXT_FAINT
        ERROR_FG, BORDER = _u.ERROR_FG, _u.BORDER
        POPUP_OPAQUE_FILL = _u.POPUP_OPAQUE_FILL

        current = self.stack.currentIndex()
        for idx in list(self._built_pages):
            _builder, _expand, layout = self._page_builders[idx]
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._built_pages.clear()
        # The sidebar nav isn't a page — re-stamp its QSS directly.
        self.nav.setStyleSheet(self._nav_qss())
        # The titlebar is built once and never rebuilt — re-stamp the
        # "Settings" heading and re-issue the cog glyph in the new tint.
        self._title_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        self._title_cog.setPixmap(icon("settings").pixmap(QSize(18, 18)))
        # Rebuild the page in view now; the rest rebuild lazily on
        # their next visit, which keeps the switch cheap.
        self._on_nav_changed(current)

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
        self._title_cog = cog

        title = QLabel("Settings")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        self._title_label = title
        h.addStretch(1)

        # About button (info circle) — opens a small overlay with the
        # name + version + blurb. Replaces the prior About settings page.
        about_btn = QPushButton()
        about_btn.setIcon(icon("info"))
        about_btn.setIconSize(QSize(18, 18))
        about_btn.setFixedSize(32, 28)
        about_btn.setToolTip("About jellytoast")
        about_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        about_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; }}
            QPushButton:hover {{ background: {ink_alpha(0.08)}; border-radius: 6px; }}
        """)
        about_btn.clicked.connect(self._show_about)
        h.addWidget(about_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; border-radius: 6px; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; color: {TEXT}; }}
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
        # other settings page (Playback, Display, Hotkeys, Scrobbling)
        # so the dialog reads as one consistent surface.
        v.setSpacing(12)

        # ── Server (folded in from the old Account page) ───────────────
        # Most surveyed players (Supersonic, Apple Music) treat server
        # / account as a row in General rather than its own peer page.
        v.addWidget(self._section_header("SERVER"))
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

        # URL + connection status dot on the same row. The dot:
        # green = reachable, orange = degraded, red = unreachable,
        # gray = checking / not signed in. Hover for details.
        #
        # The URL is a single QLineEdit — readonly by default with
        # transparent border/background so it reads as text, switching
        # to the dialog's default chrome (1-px border + tinted fill)
        # when the user enters edit mode. One widget instead of a
        # label + hidden edit swap so the text doesn't jump size or
        # baseline when the box pops in. We used to bounce to a
        # QInputDialog popup, but a modal-over-modal hiding the very
        # field you're editing is worse than letting the row accept
        # text inline.
        url = self.s.server_url or "Not configured"
        self._url_edit = QLineEdit(url)
        self._url_edit.setObjectName("serverUrlField")
        self._url_edit.setReadOnly(True)
        self._url_edit.setCursorPosition(0)
        self._url_edit.returnPressed.connect(self._commit_server_url_edit)
        # Size the field to its current text + chrome so the dot lands
        # at a fixed pixel position regardless of edit state — without
        # this, switching to setStyleSheet("") in edit mode lets Qt
        # re-compute sizeHint and the dot shifts. Use the line edit's
        # own font metrics (already inheriting the dialog font) so a
        # later theme change picks up the right size after we re-init.
        fm = self._url_edit.fontMetrics()
        # Dialog QSS at line ~3346: padding 6 12, border 1 — total
        # horizontal chrome is (12 + 12 + 1 + 1) = 26 px. Plus a
        # small 6-px slack: ``fm.horizontalAdvance`` returns the
        # advance width sum, which underestimates the actual
        # rendered run by a pixel or two on fonts with subpixel
        # anti-aliasing, and Qt's QLineEdit also reserves a tiny
        # cursor cell at end-of-text. Without the slack the final
        # digit of a tight URL gets clipped.
        url_chrome_px = 26
        text_px = fm.horizontalAdvance(url) if url else fm.horizontalAdvance("http://example.com")
        self._url_edit.setFixedWidth(text_px + url_chrome_px + 6)
        self._apply_url_edit_chrome(editing=False)

        self._conn_dot = QLabel()
        self._conn_dot.setFixedSize(8, 8)
        self._set_conn_dot_state("checking")

        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.setSpacing(0)
        url_row.addWidget(self._url_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        # 6 px past the field's right border — the field itself
        # already carries 12 px of right padding inside the chrome,
        # so the total visual gap from the end of the URL text to
        # the dot is ~18 px (roughly two character widths) without
        # the dot looking glued to the border.
        self._url_dot_spacer = QSpacerItem(
            6, 0,
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum,
        )
        url_row.addItem(self._url_dot_spacer)
        url_row.addWidget(self._conn_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        # Trailing stretch consumes the rest of the row, pinning the
        # field + dot to the left so the dot's pixel position doesn't
        # drift with row width.
        url_row.addStretch(1)
        v.addLayout(url_row)

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
            self._set_conn_dot_state("signed_out")

        # Explicit fixed height on the outlined controls — the dialog
        # QSS gives QComboBox a 22-px min-height that bumps it ~4 px
        # taller than a ghost QPushButton's natural size. Setting the
        # same fixedHeight on both buttons AND the Home page combo
        # below makes the three controls read as one row of matching
        # outlined chips. 36 px (was 34) — QComboBox's QStyleSheetStyle
        # renders its bottom border at a sub-pixel position when the
        # widget is sized to 34 (content + padding + border lands on
        # an odd half-pixel), leaving the bottom edge visibly thinner.
        # 36 gives the bottom border a clean integer row to land in;
        # buttons grow by 2 px too so the row stays visually matched.
        _CTRL_H = 36
        # Fixed width on the Change-server-URL ghost button so the
        # Home page combo below can match it exactly — without it the
        # button sized to its content while the combo had setFixedWidth,
        # and the two outlined chips read at different widths. Sized
        # snug to the text rather than wide-and-airy so the page reads
        # as a tidier column of controls.
        _CHANGE_BTN_W = 170
        # Fixed width on Sign out too — read by the URL row above to
        # land the connection dot at this column's horizontal centre,
        # so the dot reads as "the indicator for this server" lined up
        # over the action you'd take on that server.
        _SIGNOUT_BTN_W = 110
        _BTN_ROW_GAP = 8
        btn_row = QHBoxLayout()
        # Top margin trimmed from 8→4 — clawing back vertical budget
        # so the bottom Home-page combo isn't clipped on the
        # fixed-height (820×540) General page.
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.setSpacing(_BTN_ROW_GAP)
        self._change_btn = QPushButton("Change server URL…")
        self._change_btn.setObjectName("ghost")
        self._change_btn.setFixedHeight(_CTRL_H)
        self._change_btn.setFixedWidth(_CHANGE_BTN_W)
        self._change_btn.clicked.connect(self._on_change_server_clicked)
        btn_row.addWidget(self._change_btn)
        signout_btn = QPushButton("Sign out")
        signout_btn.setObjectName("ghost")
        signout_btn.setFixedHeight(_CTRL_H)
        signout_btn.setFixedWidth(_SIGNOUT_BTN_W)
        signout_btn.clicked.connect(self.sign_out_requested.emit)
        btn_row.addWidget(signout_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        self._general_ctrl_h = _CTRL_H
        self._general_change_btn_w = _CHANGE_BTN_W

        # Section gaps below were 18 — trimmed to 12 to claw back
        # vertical budget on the fixed-height General page so the
        # Home-page combo at the bottom isn't clipped.
        v.addSpacing(12)

        # ── Startup ───────────────────────────────────────────────────
        # Boot-time behaviour — what happens when jellytoast launches.
        v.addWidget(self._section_header("STARTUP"))

        # Disk truth (whether the autostart .desktop file exists) wins
        # over the persisted flag — they can drift if the user nukes
        # the file from a file manager.
        self._autostart_check = QCheckBox("Launch jellytoast at login")
        self._autostart_check.setChecked(_autostart.is_enabled())
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        v.addWidget(self._autostart_check)

        self._mini_check = QCheckBox("Show mini player on startup")
        self._mini_check.setChecked(self.s.show_mini_on_start)
        self._mini_check.toggled.connect(lambda val: setattr(self.s, "show_mini_on_start", val))
        v.addWidget(self._mini_check)

        v.addSpacing(12)

        # ── Window ────────────────────────────────────────────────────
        # Behaviour of the open windows themselves — stacking + close
        # semantics. Both toggles change WHERE jellytoast lives on the
        # desktop, not what it does at boot.
        v.addWidget(self._section_header("WINDOW"))

        # Wayland-only: xdg-shell forbids apps from setting their own
        # stacking, so Qt.WindowStaysOnTopHint is a no-op there. We
        # install a KWin window rule to do it compositor-side. The
        # toggle only appears on KDE Wayland — outside that the X11
        # hint already works and there's nothing for us to expose.
        # (On Windows / macOS this row will be replaced by the host-OS
        # equivalent when those backends land.)
        if keep_above_supported():
            self._keep_above_check = QCheckBox("Keep mini player on top")
            self._keep_above_check.setChecked(self.s.mini_player_keep_above)
            self._keep_above_check.toggled.connect(self._on_keep_above_toggled)
            v.addWidget(self._keep_above_check)

        # Tray-on-close lives last — it's the most behavior-changing
        # toggle of the group (kills the close-X exit semantics) so it
        # gets the bottom slot where the eye naturally lands on a
        # "biggest commitment" row.
        self._tray_check = QCheckBox("Hide to system tray when window is closed")
        self._tray_check.setChecked(self.s.minimize_to_tray)
        self._tray_check.toggled.connect(lambda val: setattr(self.s, "minimize_to_tray", val))
        v.addWidget(self._tray_check)

        v.addSpacing(12)

        # ── Home page ─────────────────────────────────────────────────
        # The view jellytoast lands on at launch. Section header alone
        # labels what the combo does, so no inline label needed.
        v.addWidget(self._section_header("HOME PAGE"))

        self._home_combo = _Selector()
        for label, key in HOME_DESTINATIONS:
            self._home_combo.addItem(label, key)
        self._select_combo_by_data(self._home_combo, self.s.home_destination)
        self._home_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s, "home_destination", self._home_combo.currentData() or "albums"
            )
        )
        # Same fixedHeight as the ghost buttons above — see the
        # _CTRL_H comment in the button row. sizeHint() can't be
        # relied on here because nested children aren't polished
        # against the dialog QSS until they're shown.
        self._home_combo.setFixedHeight(self._general_ctrl_h)
        # Match the Change-server-URL button width exactly so the two
        # outlined chips read as a balanced visual pair across the
        # General page.
        self._home_combo.setFixedWidth(self._general_change_btn_w)
        home_row = QHBoxLayout()
        home_row.setContentsMargins(0, 6, 0, 0)
        home_row.addWidget(self._home_combo)
        home_row.addStretch(1)
        v.addLayout(home_row)

        v.addStretch(1)
        return page

    # Status-dot palette — kept here so a future probe enrichment
    # (orange for partial connectivity, slow response, auth-degraded)
    # has a single place to extend. Tooltip text is what the user sees
    # on hover, so it should be a complete sentence, not a token name.
    # Slightly translucent fills — the dot reads as a status pip, not
    # a primary control, so dropping alpha keeps it from competing
    # with the URL text next to it. Red holds higher alpha since it's
    # the only state the user needs to act on; checking / signed_out
    # sit faintest because they're transient / inactive.
    _CONN_DOT_STATES = {
        "checking": ("rgba(136, 136, 136, 0.65)", "Checking connection…"),
        "reachable": ("rgba(47, 190, 138, 0.75)", "Connected — server is reachable."),
        "issue": ("rgba(224, 115, 92, 0.80)", "Connection issue — degraded response from server."),
        "disconnected": ("rgba(220, 38, 38, 0.85)", "Disconnected — server unreachable. Check URL or network."),
        "signed_out": ("rgba(102, 102, 102, 0.55)", "Not signed in."),
    }

    def _set_conn_dot_state(self, state: str):
        dot = getattr(self, "_conn_dot", None)
        if dot is None:
            return
        color, tip = self._CONN_DOT_STATES.get(state, self._CONN_DOT_STATES["checking"])
        # 8×8 with border-radius 4 → circle.
        dot.setStyleSheet(
            f"QLabel {{ background: {color}; border-radius: 4px; }}"
        )
        dot.setToolTip(tip)

    def _on_connection_probe(self, info):
        """Update the connection-status dot after the probe lands.
        info is the dict provider.probe returned on success, or None
        on any failure (network error, wrong port, non-backend
        response). The dot may have been destroyed if the user closed
        the dialog before the probe came back."""
        try:
            self._set_conn_dot_state("reachable" if info else "disconnected")
        except RuntimeError:
            # QLabel was destroyed (dialog closed) — drop silently.
            pass

    def _on_change_server_clicked(self):
        """First click drops the readonly flag on the URL field and
        paints in the input chrome (border + tinted fill) — the text
        position/size/colour don't move because the readonly style
        already reserves the same padding. Second click commits via
        the shared save path below. Pressing Enter in the field
        also commits."""
        if not self._url_edit.isReadOnly():
            self._commit_server_url_edit()
            return
        self._url_edit.setReadOnly(False)
        self._apply_url_edit_chrome(editing=True)
        self._url_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        # setCursorPosition (not selectAll) so the user can type/paste
        # at the end without first clearing the field.
        self._url_edit.setCursorPosition(len(self._url_edit.text()))
        self._change_btn.setText("Save URL")

    def _commit_server_url_edit(self):
        """Apply the inline edit. Emits with the trimmed URL; the
        consumer (jellytoast main window) signs out + routes to the
        login view, which also closes this dialog. Empty / unchanged
        values silently restore readonly display without firing —
        same bail-out the old QInputDialog path had."""
        new_url = self._url_edit.text().strip().rstrip("/")
        current = (self.s.server_url or "").rstrip("/")
        if not new_url or new_url == current:
            self._exit_url_edit_mode()
            return
        # Down-stack: the main window slot reads this URL, persists
        # it, and runs sign-out which closes the dialog. No need to
        # restore display state here — the dialog is going away.
        self.server_change_requested.emit(new_url)

    def _exit_url_edit_mode(self):
        self._url_edit.setReadOnly(True)
        self._url_edit.setText(self.s.server_url or "Not configured")
        self._url_edit.setCursorPosition(0)
        self._apply_url_edit_chrome(editing=False)
        self._change_btn.setText("Change server URL…")

    def _apply_url_edit_chrome(self, *, editing: bool) -> None:
        """Toggle the field's border + fill between "looks like text"
        (readonly) and "looks like an input box" (editing). Padding
        and font are constant in both modes — the box pops *around*
        the URL text rather than shifting it. Inline stylesheet
        overrides the dialog-wide QLineEdit QSS just for this widget
        via the ``serverUrlField`` objectName."""
        if editing:
            # Empty inline → fall back to the dialog's QLineEdit rule
            # (1-px solid border + ink_alpha(0.06) fill + 6/12 padding).
            self._url_edit.setStyleSheet("")
            return
        # Readonly: transparent border + transparent fill, keeping the
        # exact same padding (6 12) and border *width* (1 px) the
        # dialog QSS sets so the cursor's x-position is identical
        # between modes. Border-radius is inherited.
        self._url_edit.setStyleSheet(
            "QLineEdit#serverUrlField { "
            "background: transparent; "
            "border: 1px solid transparent; "
            "padding: 6px 12px; "
            f"color: {TEXT}; "
            "}"
        )

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
        # tag-spec name. Gapless playback and smart shuffle were once
        # toggleable here; both are now always-on (no setting), so the
        # page is one form + a couple of behaviour rows.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        # Tighter rhythm than the other pages (10 px vs 12) — the
        # Playback page packs four sections (form / crossfade / EQ /
        # shuffle) into the dialog's fixed height. Section headers
        # already give visual chunking; the extra 2 px between
        # siblings wasn't load-bearing.
        v.setSpacing(10)

        # ── Bit-perfect mode ─────────────────────────────────────────
        # Master "no DSP, no resample, no attenuation" lock. When on:
        # Normalization, EQ, Crossfade, and the volume slider are all
        # gated to their bit-perfect-safe values. Sits at the top of
        # the page because it overrides the controls below it; the
        # surfaces it gates listen to PlayerBus.bit_perfect_changed.
        # See `docs/research/bit_perfect_playback.md` §7 (T2) and the
        # user-facing `docs/bit_perfect.md` for the PipeWire side.
        v.addWidget(self._section_header("BIT-PERFECT"))
        self._bit_perfect_check = QCheckBox("Bit-perfect mode")
        self._bit_perfect_check.setChecked(self.s.bit_perfect_mode)
        self._bit_perfect_check.toggled.connect(self._on_bit_perfect_toggled)
        v.addWidget(self._bit_perfect_check)
        bp_caption = QLabel(
            "Locks volume at 100% and disables Normalization, Equalizer, and "
            "Crossfade. PipeWire session-rate matching is the user's side — "
            "see docs/bit_perfect.md."
        )
        bp_caption.setWordWrap(True)
        bp_caption.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
        )
        v.addWidget(bp_caption)

        # Exclusive output — T3 sub-toggle. Indented (a sub-option of
        # bit-perfect) and only meaningful when the parent is on. The
        # parent gate's _refresh_bit_perfect_gating handler enables /
        # disables this row in lockstep.
        excl_row = QHBoxLayout()
        excl_row.setContentsMargins(20, 0, 0, 0)  # indent under parent
        excl_row.setSpacing(0)
        self._audio_exclusive_check = QCheckBox("Exclusive output")
        self._audio_exclusive_check.setChecked(self.s.audio_exclusive)
        self._audio_exclusive_check.toggled.connect(self._on_audio_exclusive_toggled)
        excl_row.addWidget(self._audio_exclusive_check)
        excl_row.addStretch(1)
        v.addLayout(excl_row)
        excl_caption = QLabel(
            "Takes the DAC over (WASAPI Exclusive · CoreAudio HogMode · "
            "PipeWire sink-cork). Other apps go silent during playback. "
            "Applies on the next track; falls back to shared mode if the "
            "DAC refuses exclusive open."
        )
        excl_caption.setWordWrap(True)
        excl_caption.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-left: 20px;"
        )
        v.addWidget(excl_caption)
        self._audio_exclusive_caption = excl_caption

        # T4 — PipeWire conf installer. Linux-only; on other platforms
        # the whole row stays hidden. Drops a small conf file that lets
        # PipeWire follow the source sample rate (most CD-quality
        # material stops resampling 44.1 → 48 kHz). See
        # ``modules.pipewire_setup`` for the helper and
        # ``docs/research/bit_perfect_playback.md`` §4.2 / §7 (T4).
        from modules import pipewire_setup as _pws

        if _pws.is_supported():
            pw_row = QHBoxLayout()
            pw_row.setContentsMargins(0, 6, 0, 0)
            pw_row.setSpacing(8)
            self._pw_install_btn = QPushButton()
            self._pw_install_btn.clicked.connect(self._on_pipewire_install_clicked)
            pw_row.addWidget(self._pw_install_btn)
            pw_row.addStretch(1)
            v.addLayout(pw_row)
            pw_caption = QLabel(
                "Lets PipeWire follow the source sample rate so 44.1 kHz "
                "files stop resampling to 48 kHz. Restart pipewire or log "
                "out / in for the change to take effect."
            )
            pw_caption.setWordWrap(True)
            pw_caption.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
            )
            v.addWidget(pw_caption)
            # Small inline status flash — populated by the click
            # handler after each install / remove so the user sees the
            # action took (without a modal interrupting their flow).
            self._pw_status_label = QLabel("")
            self._pw_status_label.setWordWrap(True)
            self._pw_status_label.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            self._pw_status_label.setVisible(False)
            v.addWidget(self._pw_status_label)
            self._refresh_pipewire_button_label()

        # ── Audio ─────────────────────────────────────────────────────
        # Quality (transcode tier) + Normalization (ReplayGain mode)
        # belong together: both decide what the SOURCE stream looks like
        # before it hits the decoder. Header gives the page a consistent
        # rhythm with the Crossfade / Equalizer blocks below.
        v.addWidget(self._section_header("AUDIO"))

        # Single shared form so Quality and Normalization labels +
        # combo boxes column-align cleanly.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)

        # Fixed combo width — both AUDIO combos match each other AND
        # the EQ Preset combo's fixed 180-px width below, so the three
        # combos read as one tidy stacked column of outlined chips
        # rather than two wide ones over a narrow one.
        _AUDIO_COMBO_W = 120
        # Fixed label-column width shared between the AUDIO form and
        # the EQ Preset row, so all three combos (Quality, Normalization,
        # Preset) align in one vertical column. Tuned to fit
        # "Normalization:" at TYPE_BODY with a few px of breathing room.
        # Stashed on self so _build_eq_section can reuse it.
        self._playback_label_w = 130

        q_label = self._field_label("Quality:")
        q_label.setMinimumWidth(self._playback_label_w)
        self._quality_combo = _Selector()
        for label, key in AUDIO_QUALITIES:
            self._quality_combo.addItem(label, key)
        self._select_combo_by_data(self._quality_combo, self.s.audio_quality or "original")
        def _on_quality_changed():
            new_q = self._quality_combo.currentData() or "original"
            self.s.audio_quality = new_q
            # PlayerBackend listens to recompute the runtime bit-perfect
            # contract — switching to a transcoded tier breaks it
            # before the codec arrives on the next track.
            PlayerBus.get().audio_quality_changed.emit(new_q)
        self._quality_combo.currentIndexChanged.connect(lambda _: _on_quality_changed())
        self._quality_combo.setFixedWidth(_AUDIO_COMBO_W)
        form.addRow(q_label, self._quality_combo)

        # ReplayGain is the canonical tag spec, but the user-facing
        # label reads as "Normalization" since that's the effect users
        # recognise (and what other players call it).
        n_label = self._field_label("Normalization:")
        n_label.setMinimumWidth(self._playback_label_w)
        self._rg_combo = _Selector()
        for label, key in REPLAYGAIN_MODES:
            self._rg_combo.addItem(label, key)
        self._select_combo_by_data(self._rg_combo, self.s.replaygain)
        self._rg_combo.currentIndexChanged.connect(self._on_replaygain_changed)
        self._rg_combo.setFixedWidth(_AUDIO_COMBO_W)
        # Short labels lose context; tooltips spell out the longer
        # description we used to bake into the label.
        self._rg_combo.setToolTip(
            "Normalization mode:\n"
            "  Off — play untouched.\n"
            "  Track — match each song to a common loudness target.\n"
            "  Album — preserve relative loudness within an album."
        )
        form.addRow(n_label, self._rg_combo)

        v.addLayout(form)
        v.addWidget(self._build_crossfade_section())

        # ── Shuffle ───────────────────────────────────────────────────
        # Lives on Playback rather than Library because it shapes
        # playback behaviour, not what the grid looks like. Single
        # combo — the queue-size cap — kept tight to match the page's
        # other label/combo rows. Sits between Crossfade and EQ so
        # the heaviest block (EQ — 11 sliders + header row) anchors
        # the bottom of the page.
        v.addWidget(self._section_header("SHUFFLE"))
        shuffle_form = QFormLayout()
        shuffle_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        shuffle_form.setHorizontalSpacing(16)
        shuffle_form.setVerticalSpacing(8)
        sq_label = self._field_label("Queue size:")
        sq_label.setMinimumWidth(self._playback_label_w)
        self._shuffle_size_combo = _Selector()
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
        self._shuffle_size_combo.setFixedWidth(120)
        shuffle_form.addRow(sq_label, self._shuffle_size_combo)
        v.addLayout(shuffle_form)

        # Extra air before Equalizer — the EQ block is the visually
        # heaviest section on the page (11 sliders + a header row),
        # and a tighter sibling gap made it sit on top of whatever
        # is above. The form section above already has the default
        # 10 px from the page-level setSpacing.
        v.addSpacing(8)
        v.addWidget(self._build_eq_section())

        v.addStretch(1)
        # Initial gating pass — if bit_perfect_mode was persisted as on,
        # apply the disabled state to every dependent now that they all
        # exist (EQ + Crossfade build inside this function so they were
        # nil when the section header was added at the top of the page).
        self._refresh_bit_perfect_gating()
        return page

    def _build_crossfade_section(self) -> QWidget:
        """Crossfade controls — enable toggle, fade-duration slider, and
        the same-album continuity escape hatch. Cast-greyed like the EQ
        section: crossfade ping-pongs two local mpv handles, so it has
        no effect on a cast device's own decoder."""
        wrap = QFrame()
        wrap.setObjectName("jtCrossfadeSection")
        wrap.setStyleSheet(
            "QFrame#jtCrossfadeSection { background: transparent; border: none; }"
        )
        wv = QVBoxLayout(wrap)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(6)

        wv.addWidget(self._section_header("CROSSFADE"))
        # Breathing room under the section header before the toggles —
        # without it, the small-caps header sits visually attached to
        # the first checkbox.
        wv.addSpacing(4)

        # Both toggles on one row: parent "Crossfade between tracks"
        # left, "Skip on albums" sibling right. Duration sits beneath
        # both as a sub-option of the parent (only meaningful when
        # crossfade is on; greyed via _refresh_xf_enabled_state).
        toggle_row = QHBoxLayout()
        # 16→28: extra breathing room between the parent "Crossfade
        # between tracks" toggle and its "Skip on albums" sibling, so
        # the sub-option reads as a distinct choice rather than
        # crowding the parent label.
        toggle_row.setSpacing(28)
        self._xf_enabled = QCheckBox("Crossfade between tracks")
        self._xf_enabled.setChecked(self.s.crossfade_enabled)
        self._xf_enabled.toggled.connect(self._on_xf_enabled_toggled)
        toggle_row.addWidget(self._xf_enabled)

        self._xf_smart_album = QCheckBox("Skip on albums")
        self._xf_smart_album.setToolTip(
            "Skip the crossfade between tracks on the same album so "
            "albums that flow as one piece (live sets, concept records) "
            "stay seamless."
        )
        self._xf_smart_album.setChecked(self.s.crossfade_smart_album_continuity)
        self._xf_smart_album.toggled.connect(
            lambda val: setattr(self.s, "crossfade_smart_album_continuity", val)
        )
        toggle_row.addWidget(self._xf_smart_album)
        toggle_row.addStretch(1)
        wv.addLayout(toggle_row)
        # Air between the toggles and the duration slider — the
        # slider's visual weight (the accent rail) needs separation
        # from the checkboxes above or the row reads as one cluster.
        wv.addSpacing(8)

        # Duration — stored in ms (1000–10000), shown to the user in
        # seconds. The setting getter/setter clamp, so the slider range
        # only needs to match for a tidy UI.
        # Layout: label sits in the same column as Quality / Normalization
        # / Preset (min-width matches `_playback_label_w`, spacing 16
        # matches the AUDIO form's setHorizontalSpacing) so all four
        # labels and all four field-column starts line up vertically.
        # Right margin caps the slider at the same column as the EQ
        # band area below. With dialog 720 and nav 128, content
        # width ≈ 516. EQ grid 16k right edge sits ~482 px in; a
        # 46-px trailing pad leaves a 12-px breath inside the 16k
        # boundary and mirrors what the preset row below uses.
        _CONTENT_RIGHT_PAD = 46  # mirrors preset_row's trailing pad
        dur_row = QHBoxLayout()
        dur_row.setContentsMargins(0, 0, _CONTENT_RIGHT_PAD, 0)
        dur_row.setSpacing(16)
        self._xf_duration_lbl = self._field_label("Duration:")
        self._xf_duration_lbl.setMinimumWidth(self._playback_label_w)
        dur_row.addWidget(self._xf_duration_lbl)
        self._xf_duration = QSlider(Qt.Orientation.Horizontal)
        self._xf_duration.setRange(1000, 10000)
        self._xf_duration.setSingleStep(500)
        self._xf_duration.setPageStep(1000)
        self._xf_duration.setValue(self.s.crossfade_duration_ms)
        self._xf_duration.valueChanged.connect(self._on_xf_duration_changed)
        # Explicit slider QSS — without this the dialog falls back to
        # the native QStyle for the handle (a blue Fusion dot) instead
        # of the white-on-accent treatment GLOBAL_STYLE defines for
        # every other horizontal slider. Rebuilt on accent change via
        # _reapply_dialog_accent_styling.
        self._xf_duration.setStyleSheet(self._horiz_slider_qss())
        dur_row.addWidget(self._xf_duration, 1)
        self._xf_duration_label = QLabel()
        self._xf_duration_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} min-width: 42px;"
        )
        dur_row.addWidget(self._xf_duration_label)
        wv.addLayout(dur_row)

        self._xf_caption = QLabel("")
        self._xf_caption.setWordWrap(True)
        self._xf_caption.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        # Hidden by default — same logic as the EQ caption. Only shows
        # while a cast is active; reclaims the row's line height when
        # the message is empty.
        self._xf_caption.setVisible(False)
        wv.addWidget(self._xf_caption)

        # Cast-greying — crossfade is local-playback only.
        try:
            bus = PlayerBus.get()
            bus.cast_started.connect(self._on_xf_cast_active)
            bus.cast_stopped.connect(self._on_xf_cast_cleared)
        except Exception:
            pass

        self._update_xf_duration_label()
        self._refresh_xf_enabled_state()
        return wrap

    # ── Crossfade helpers ───────────────────────────────────────────────────

    def _on_xf_enabled_toggled(self, on: bool):
        self.s.crossfade_enabled = on
        self._refresh_xf_enabled_state()
        PlayerBus.get().crossfade_changed.emit(bool(on))

    def _on_xf_duration_changed(self, ms: int):
        self.s.crossfade_duration_ms = ms
        self._update_xf_duration_label()

    def _update_xf_duration_label(self):
        self._xf_duration_label.setText(f"{self.s.crossfade_duration_ms / 1000.0:.1f} s")

    def _refresh_xf_enabled_state(self):
        """Gate the duration slider + same-album toggle on the enable
        checkbox, and hard-disable the whole section while casting."""
        on = bool(self.s.crossfade_enabled)
        casting = getattr(self, "_xf_cast_blocking", False)
        active = on and not casting
        self._xf_duration.setEnabled(active)
        self._xf_smart_album.setEnabled(active)
        self._xf_enabled.setEnabled(not casting)
        # Duration label + value-readout aren't widgets Qt natively
        # greys (they're QLabels with explicit QSS colour overriding
        # the palette), so restyle them in lockstep so the row reads
        # as inactive when crossfade is off.
        dim_colour = DISABLED_FG if not active else TEXT_DIM
        self._xf_duration_lbl.setStyleSheet(
            f"color: {dim_colour}; {type_qss(TYPE_BODY)}"
        )
        self._xf_duration_label.setStyleSheet(
            f"color: {dim_colour}; {type_qss(TYPE_CAPTION)} min-width: 42px;"
        )
        if casting:
            self._xf_caption.setText(
                "Casting — crossfade applies to local playback only and is inactive now."
            )
            self._xf_caption.setVisible(True)
        else:
            self._xf_caption.setText("")
            self._xf_caption.setVisible(False)

    def _on_xf_cast_active(self, *_args):
        self._xf_cast_blocking = True
        self._refresh_xf_enabled_state()

    def _on_xf_cast_cleared(self, *_args):
        self._xf_cast_blocking = False
        self._refresh_xf_enabled_state()

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
        wv.setSpacing(6)

        wv.addWidget(self._section_header("EQUALIZER"))
        # Same header-to-content gap pattern as Crossfade.
        wv.addSpacing(4)

        # Enable checkbox + Linear-phase sub-toggle on one row — the
        # master toggle plus EQ T2's opt-in mode. "Enable" reads
        # rather than "Equalizer" since the section header already
        # labels the section.
        eq_toggle_row = QHBoxLayout()
        eq_toggle_row.setSpacing(20)
        self._eq_enabled_check = QCheckBox("Enable")
        self._eq_enabled_check.setChecked(self.s.eq_enabled)
        self._eq_enabled_check.toggled.connect(self._on_eq_enabled_toggled)
        eq_toggle_row.addWidget(self._eq_enabled_check)
        self._eq_linear_phase_check = QCheckBox("Linear phase")
        self._eq_linear_phase_check.setChecked(self.s.eq_linear_phase)
        self._eq_linear_phase_check.setToolTip(
            "Linear-phase FIR mode — preserves transient response through "
            "the EQ (audible on drums + plucked strings). Costs ~3× CPU "
            "and adds ~20 ms internal latency. Off by default."
        )
        self._eq_linear_phase_check.toggled.connect(self._on_eq_linear_phase_toggled)
        eq_toggle_row.addWidget(self._eq_linear_phase_check)
        eq_toggle_row.addStretch(1)
        # EQ T3b — view-mode toggle. "Curve" replaces the 10-band
        # slider strip with the parametric curve editor; same band
        # data, different surface. Settings persists the choice so the
        # user's preferred view comes back next session.
        self._eq_advanced_check = QCheckBox("Curve")
        self._eq_advanced_check.setChecked(self.s.eq_view_advanced)
        self._eq_advanced_check.setToolTip(
            "Parametric curve editor — drag nodes on a log-frequency "
            "canvas. AutoEQ profiles allow horizontal drag (movable "
            "centres); graphic mode keeps freqs locked, gain only."
        )
        self._eq_advanced_check.toggled.connect(self._on_eq_advanced_toggled)
        eq_toggle_row.addWidget(self._eq_advanced_check)
        wv.addLayout(eq_toggle_row)
        wv.addSpacing(6)

        # Preset row: combo + Save / Delete. Right-padded so the
        # Delete button sits a pinch inside the 16k band's right
        # edge. Content width ≈ 516 after the dialog 820→720 cut;
        # band-grid 16k right edge at ~482 → 46-px trailing pad
        # leaves a 12-px breath inside the 16k boundary.
        # Label uses the same width + body-type styling as Quality /
        # Normalization / Duration above so the four labels line up
        # in one column and the four field starts in another.
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 46, 0)
        preset_row.setSpacing(16)

        preset_lbl = self._field_label("Preset:")
        preset_lbl.setMinimumWidth(self._playback_label_w)
        preset_row.addWidget(preset_lbl)

        self._eq_preset_combo = _Selector()
        self._populate_eq_preset_combo()
        self._select_combo_by_data(self._eq_preset_combo, self.s.eq_preset)
        self._eq_preset_combo.currentIndexChanged.connect(self._on_eq_preset_changed)
        # Sized to fit the longest preset name comfortably without
        # gobbling all remaining horizontal space — at full stretch the
        # box read absurdly wide for the one short word "Flat". Stretch
        # is added AFTER the combo so the trailing Save / Delete buttons
        # still pin to the right edge of the row.
        self._eq_preset_combo.setFixedWidth(120)
        preset_row.addWidget(self._eq_preset_combo)
        preset_row.addStretch(1)

        self._eq_save_btn = QPushButton("Save…")
        self._eq_save_btn.clicked.connect(self._on_eq_save_preset)
        preset_row.addWidget(self._eq_save_btn)

        self._eq_delete_btn = QPushButton("Delete")
        self._eq_delete_btn.clicked.connect(self._on_eq_delete_preset)
        preset_row.addWidget(self._eq_delete_btn)

        wv.addLayout(preset_row)

        # Caption row — empty by default; populated during cast-greying
        # with "Casting — EQ inactive". Hidden when empty so it costs
        # zero vertical space on the default layout — the EQ section
        # is the tallest block on this page and the dialog has no
        # room to spare.
        self._eq_caption = QLabel("")
        self._eq_caption.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 22px;"
        )
        self._eq_caption.setVisible(False)
        wv.addWidget(self._eq_caption)

        # ── Slider grid: pre-amp + 10 bands ─────────────────────────────────
        # Vertical sliders in a QGridLayout. Row 0 = live dB readout,
        # row 1 = slider, row 2 = band-centre label. Indices: column 0
        # is pre-amp, columns 1..10 are bands 31Hz..16kHz.
        self._eq_sliders: list[QSlider] = []
        self._eq_readouts: list[QLabel] = []
        # Band labels kept on instance so _refresh_eq_enabled_state can
        # restyle them (faint when EQ is off, dim when on) for the
        # "draw it in when checked" effect.
        self._eq_band_labels: list[QLabel] = []

        # Each column is its own QWidget with a QVBoxLayout —
        # using a single QGridLayout for all 11 columns let the slider
        # widget visually overflow into the band-label row at min/max
        # value. Per-column widgets give Qt strict bounds per column
        # so the layout can't bleed across rows.
        slider_frame = QFrame()
        slider_frame.setStyleSheet("QFrame { background: transparent; }")
        sf_layout = QHBoxLayout(slider_frame)
        # Left margin 0 (was 4) so the Pre column starts flush with
        # the page's content edge, lining up under the Preset: label
        # above. Horizontal spacing 2 (was 6) packs the bands tight
        # so the eleven columns read as a single equalizer block
        # rather than 11 separate widgets.
        sf_layout.setContentsMargins(0, 0, 4, 0)
        sf_layout.setSpacing(2)

        def _fmt_freq(hz: int) -> str:
            return f"{hz // 1000}k" if hz >= 1000 else str(hz)

        labels = ["Pre"] + [_fmt_freq(f) for f in BAND_FREQUENCIES]
        initial = [self.s.eq_preamp] + list(self.s.eq_bands)

        for label_text, val in zip(labels, initial):
            col_widget = QWidget()
            col_widget.setStyleSheet("background: transparent;")
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(0, 0, 0, 0)
            # Default spacing is 0 here; explicit ``addSpacing()`` calls
            # below the readout / slider give precise control over how
            # far the handle sits from the readout and band labels.
            # Layout-spacing alone wasn't enough — the handle's vertical
            # range is the full slider widget, so the dot at -12 hits
            # the widget's bottom edge regardless of QSS groove margin.
            col_layout.setSpacing(0)

            readout = QLabel(self._fmt_db_readout(val))
            readout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            readout.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            readout.setFixedHeight(16)
            col_layout.addWidget(readout)
            self._eq_readouts.append(readout)

            # Small gap above the slider so the readout doesn't kiss
            # the +12 dot.
            col_layout.addSpacing(4)

            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setSingleStep(1)
            slider.setPageStep(3)
            slider.setTickPosition(QSlider.TickPosition.TicksRight)
            slider.setTickInterval(6)  # ticks at -12, -6, 0, +6, +12
            slider.setValue(int(round(float(val))))
            # With groove margin = handle radius (7 px) in the QSS,
            # the +12 handle's top edge sits flush with the slider
            # widget's top edge and the -12 handle's bottom edge sits
            # flush with the widget bottom. ``addSpacing`` on either
            # side of the widget gives the dots breathing room from
            # the readout / band labels — without that, the handle
            # at -12 lands on the "31" / "62" label below.
            slider.setFixedHeight(88)
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

            # Gap below the slider so the -12 dot doesn't sit on the
            # band label.
            col_layout.addSpacing(8)

            band_lbl = QLabel(label_text)
            band_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            # TEXT_DIM (not TEXT_FAINT) so the freq label is legible
            # against the dialog background — the row sits right under
            # the slider's accent-purple fill and needed more contrast.
            band_lbl.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            band_lbl.setFixedHeight(16)
            col_layout.addWidget(band_lbl)
            self._eq_band_labels.append(band_lbl)

            col_widget.setFixedWidth(42)
            sf_layout.addWidget(col_widget)

        sf_layout.addStretch(1)
        wv.addWidget(slider_frame)
        self._eq_slider_frame = slider_frame

        # ── EQ T3b — parametric curve editor (alternative view) ───────
        # Same height envelope as the slider strip so toggling between
        # the two views doesn't reflow the rest of the dialog.
        from modules.eq_curve_editor import EqCurveEditor

        self._eq_curve_editor = EqCurveEditor()
        self._eq_curve_editor.setFixedHeight(slider_frame.sizeHint().height() or 130)
        self._eq_curve_editor.band_dragging.connect(self._on_curve_band_dragging)
        self._eq_curve_editor.band_edited.connect(self._on_curve_band_edited)
        # T3c — Q wheel, add band (double-click empty), remove band
        # (right-click on node). All three only fire in PEQ mode.
        self._eq_curve_editor.band_q_changed.connect(self._on_curve_band_q_changed)
        self._eq_curve_editor.band_added.connect(self._on_curve_band_added)
        self._eq_curve_editor.band_removed.connect(self._on_curve_band_removed)
        self._eq_curve_editor.setVisible(False)
        wv.addWidget(self._eq_curve_editor)

        # ── AutoEQ profile (EQ T3a) ──────────────────────────────────
        # Headphone-correction profiles from autoeq.app or similar.
        # When loaded, the parametric path takes precedence over the
        # 10-band graphic gains; the sliders dim to make that clear.
        # See `docs/research/eq_dsp_v2.md` §6 (T3a) for the rationale.
        wv.addSpacing(4)
        autoeq_row = QHBoxLayout()
        autoeq_row.setSpacing(8)
        self._autoeq_status = QLabel("AutoEQ: not loaded")
        self._autoeq_status.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        autoeq_row.addWidget(self._autoeq_status, 1)
        self._autoeq_import_btn = QPushButton("Import…")
        self._autoeq_import_btn.clicked.connect(self._on_autoeq_import_clicked)
        autoeq_row.addWidget(self._autoeq_import_btn)
        self._autoeq_clear_btn = QPushButton("Clear")
        self._autoeq_clear_btn.clicked.connect(self._on_autoeq_clear_clicked)
        autoeq_row.addWidget(self._autoeq_clear_btn)
        wv.addLayout(autoeq_row)
        self._refresh_autoeq_status()

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

        # EQ T3b — restore the persisted view (Simple sliders vs
        # Advanced curve editor). Done after _refresh_eq_enabled_state
        # so any disabled-state styling already applies.
        if bool(self.s.eq_view_advanced):
            self._eq_slider_frame.setVisible(False)
            self._eq_curve_editor.setVisible(True)
            self._sync_curve_editor_from_bands()

        # Stash refs to enable bulk en/disable in cast-greying.
        self._eq_section_widget = wrap
        self._eq_preset_count_builtin = len(PRESETS)
        return wrap

    # ── Slider helpers ──────────────────────────────────────────────────────

    def _horiz_slider_qss(self) -> str:
        """Horizontal slider QSS — same accent-on-track + white-handle
        treatment GLOBAL_STYLE defines for the app, inlined here so
        sliders inside the dialog don't fall back to the native
        QStyle's blue Fusion handle (the dialog stylesheet's presence
        seems to suppress the app-level QSlider cascade on Wayland).
        Reads ACCENT live so a theme change rebuilds it on the next
        dialog open / theme_changed."""
        from modules.ui_helpers import ACCENT as _ACCENT_NOW

        return f"""
            QSlider::groove:horizontal {{
                height: 3px; background: {ink_alpha(0.12)}; border-radius: 1px;
            }}
            QSlider::sub-page:horizontal {{
                background: {_ACCENT_NOW}; border-radius: 1px;
            }}
            QSlider::add-page:horizontal {{
                background: transparent;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -5px 0;
                background: {TEXT}; border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {_ACCENT_NOW};
            }}
            /* Disabled state — when the parent crossfade toggle is
               off, strip the accent fill and dim the handle so the
               slider obviously reads as inactive. Matches the EQ
               slider's :disabled treatment. */
            QSlider::sub-page:horizontal:disabled {{
                background: rgba(255, 255, 255, 0.15);
            }}
            QSlider::handle:horizontal:disabled {{
                background: rgba(255, 255, 255, 0.25);
            }}
            QSlider::groove:horizontal:disabled {{
                background: rgba(255, 255, 255, 0.05);
            }}
        """

    # ── EQ helpers ──────────────────────────────────────────────────────────

    def _eq_slider_qss(self) -> str:
        """Vertical EQ slider QSS. Reads accent live so a theme change
        rebuilds it on the next dialog open / theme_changed.

        The handle was previously solid ACCENT + 2px white border —
        too bright and high-contrast compared to the rest of the
        dialog's subtle accent treatment (combo borders use
        rgba(accent, 0.45)). Toned down to ACCENT_DEEP fill + 1px
        rgba(accent, 0.55) border so the dot reads as part of the
        dialog family instead of a hot spotlight."""
        from modules.theme import _hex_to_rgb
        from modules.ui_helpers import ACCENT_DEEP as _ACCENT_DEEP

        try:
            ar, ag, ab = _hex_to_rgb(_ACCENT_DEEP)
        except Exception:
            ar, ag, ab = 124, 102, 208

        return f"""
            QSlider:vertical {{
                background: transparent;
            }}
            QSlider::groove:vertical {{
                width: 4px;
                /* Margin matches the handle radius (7 px) so the
                   handle's top edge at +12 sits flush with the rail
                   top and the bottom edge at -12 sits flush with the
                   rail bottom — no overshoot. Breathing room from
                   the readout / band labels comes from
                   ``addSpacing`` calls around the slider widget, not
                   from this margin. */
                margin: 7px 0;
                background: {ink_alpha(0.10)};
                border-radius: 2px;
            }}
            /* Sub-page / add-page intentionally transparent. Qt paints
               them on top of the groove without honouring the groove's
               margin, so any non-transparent fill leaks past the rail
               ends as faint over-drawn bits at top / bottom. The
               groove background is the rail; that's enough. */
            QSlider::sub-page:vertical,
            QSlider::add-page:vertical {{
                background: transparent;
                border: none;
            }}
            QSlider::handle:vertical {{
                width: 14px; height: 14px; margin: 0 -5px;
                background: {_ACCENT_DEEP}; border-radius: 7px;
                border: 1px solid rgba({ar},{ag},{ab},0.55);
            }}
            /* When EQ is disabled (master toggle off OR cast active),
               the accent handle reads as still-active. Strip the accent
               and use an ink wash + lighter border so the whole grid
               obviously feels OFF — "draws in" the accent when the
               user re-enables. */
            QSlider::handle:vertical:disabled {{
                background: rgba(255, 255, 255, 0.18);
                border: 1px solid rgba(255, 255, 255, 0.10);
            }}
            QSlider::groove:vertical:disabled {{
                background: rgba(255, 255, 255, 0.05);
            }}
            QSlider::tick:vertical {{
                background: {ink_alpha(0.18)};
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

    def _on_eq_linear_phase_toggled(self, val: bool):
        """EQ T2 — flip between IIR (``anequalizer``) and linear-phase
        FIR (``firequalizer``). ``apply_eq`` reads the setting at apply
        time and picks the right formatter; firing ``eq_changed`` here
        triggers the re-apply (the ``_last_eq_state`` cache key includes
        ``linear_phase`` so the rewrite isn't short-circuited)."""
        self.s.eq_linear_phase = bool(val)
        self._emit_eq_changed()

    # ── AutoEQ profile import (EQ T3a) ──────────────────────────────

    def _refresh_autoeq_status(self):
        """Update the status label + Clear button enabled state based
        on whether a profile is loaded. Called on page build, after
        every import, and after clear. The 10-band slider grid greying
        is handled by ``_refresh_eq_enabled_state`` which reads the
        same setting."""
        if not hasattr(self, "_autoeq_status"):
            return
        raw = self.s.eq_autoeq_profile_json or ""
        if not raw:
            self._autoeq_status.setText("AutoEQ: not loaded")
            self._autoeq_clear_btn.setEnabled(False)
            return
        try:
            parsed = json.loads(raw)
            n_bands = len(parsed.get("bands", []))
            n_skipped = len(parsed.get("skipped", []))
            preamp = parsed.get("preamp_db", 0.0)
            parts = [f"{n_bands} bands"]
            if abs(preamp) > 0.05:
                parts.append(f"preamp {preamp:+.1f} dB")
            if n_skipped:
                parts.append(f"{n_skipped} skipped")
            self._autoeq_status.setText("AutoEQ: " + " · ".join(parts))
            self._autoeq_clear_btn.setEnabled(True)
        except (json.JSONDecodeError, TypeError):
            self._autoeq_status.setText("AutoEQ: (corrupt — Clear to reset)")
            self._autoeq_clear_btn.setEnabled(True)

    def _on_autoeq_import_clicked(self):
        """Open a small paste-area dialog. On accept, parse the input as
        an AutoEQ ``ParametricEQ.txt`` profile, save it to settings, and
        re-apply the EQ. Validation errors surface in a status line in
        the dialog rather than as a separate message box — the user can
        edit and retry without dismissing."""
        from modules.eq_presets import parse_autoeq_profile

        dlg = QDialog(self)
        dlg.setWindowTitle("Import AutoEQ profile")
        dlg.setModal(True)
        dlg.resize(560, 360)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        instructions = QLabel(
            "Paste a ParametricEQ.txt-style profile from autoeq.app or "
            "your headphone correction source. Each line should look "
            "like: <code>Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41</code>. "
            "Shelf filters (LSC / HSC) are skipped — most headphone "
            "profiles are predominantly peaking filters."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        text_edit = QPlainTextEdit()
        text_edit.setPlaceholderText(
            "Preamp: -6.6 dB\nFilter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41\n…"
        )
        layout.addWidget(text_edit, 1)

        preview = QLabel("")
        preview.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        preview.setWordWrap(True)
        layout.addWidget(preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Import")
        ok_btn.setEnabled(False)
        layout.addWidget(btns)

        # Live preview as the user types/pastes — shows band count +
        # preamp + skipped count without committing the profile.
        def _on_text_changed():
            text = text_edit.toPlainText()
            if not text.strip():
                preview.setText("")
                ok_btn.setEnabled(False)
                return
            parsed = parse_autoeq_profile(text)
            if not parsed["bands"]:
                preview.setText(
                    "No peaking filters detected — check the format."
                )
                ok_btn.setEnabled(False)
                return
            n_bands = len(parsed["bands"])
            n_skipped = len(parsed["skipped"])
            parts = [f"{n_bands} bands"]
            if abs(parsed["preamp_db"]) > 0.05:
                parts.append(f"preamp {parsed['preamp_db']:+.1f} dB")
            if n_skipped:
                parts.append(f"{n_skipped} non-peaking filters will be skipped")
            preview.setText("Ready: " + " · ".join(parts))
            ok_btn.setEnabled(True)

        text_edit.textChanged.connect(_on_text_changed)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        text = text_edit.toPlainText()
        parsed = parse_autoeq_profile(text)
        if not parsed["bands"]:
            return  # safety — UI shouldn't have enabled Ok
        # Persist the full parsed dict (including skipped) so the
        # status row can surface "N skipped" without re-parsing the
        # raw text. apply_eq reads bands + preamp_db only.
        self.s.eq_autoeq_profile_json = json.dumps(parsed)
        self._refresh_autoeq_status()
        self._refresh_eq_enabled_state()  # grey the sliders
        self._sync_curve_editor_from_bands()  # T3b — update editor view
        self._emit_eq_changed()  # trigger re-apply

    def _on_autoeq_clear_clicked(self):
        """Drop the active profile. Sliders un-grey; graphic EQ takes
        the floor again. Doesn't touch the slider gains — the user's
        last-saved curve resumes immediately."""
        if not self.s.eq_autoeq_profile_json:
            return
        self.s.eq_autoeq_profile_json = ""
        self._refresh_autoeq_status()
        self._refresh_eq_enabled_state()
        # Re-seed the curve editor with the (now-active) graphic bands.
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    # ── EQ T3b — curve editor view toggle + sync ────────────────────

    def _on_eq_advanced_toggled(self, on: bool):
        """Swap the slider grid for the curve editor (or back). Bands
        are the same data underneath, just visualized differently."""
        self.s.eq_view_advanced = bool(on)
        self._eq_slider_frame.setVisible(not on)
        self._eq_curve_editor.setVisible(on)
        if on:
            self._sync_curve_editor_from_bands()

    def _sync_curve_editor_from_bands(self):
        """Push the active band list into the curve editor. Reads
        AutoEQ profile if loaded; else builds parametric bands from
        the graphic-mode slider gains. ``lock_freq`` follows mode:
        AutoEQ → False (movable centres); graphic → True (ISO-locked)."""
        if not hasattr(self, "_eq_curve_editor"):
            return
        from modules.eq_presets import build_default_parametric_bands

        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if autoeq_raw:
            try:
                parsed = json.loads(autoeq_raw)
                bands = list(parsed.get("bands", []))
                self._eq_curve_editor.set_bands(bands, lock_freq=False)
                return
            except (json.JSONDecodeError, TypeError):
                pass
        # Graphic mode — synthesize parametric bands from slider gains.
        try:
            gains = list(self.s.eq_bands)
            bands = build_default_parametric_bands(gains)
        except Exception:
            bands = []
        self._eq_curve_editor.set_bands(bands, lock_freq=True)

    def _on_curve_band_dragging(self, idx: int, freq: float, gain: float):
        """Live updates from the curve editor — mirror back into the
        corresponding slider so the user sees both surfaces move in
        lockstep. No settings write yet (cheap repaint only); the
        final settings persist happens on band_edited (release)."""
        if 0 <= idx < len(getattr(self, "_eq_sliders", [])):
            # _eq_sliders is [pre-amp, band0, band1, ..., band9]; band
            # at index idx maps to slider idx+1.
            slider = self._eq_sliders[idx + 1] if idx + 1 < len(self._eq_sliders) else None
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(int(round(gain)))
                slider.blockSignals(False)
        # Update the readout label in-place for the dragging band.
        if 0 <= idx < len(getattr(self, "_eq_readouts", []) or []) - 1:
            readout = self._eq_readouts[idx + 1]
            readout.setText(self._fmt_db_readout(gain))

    def _on_curve_band_edited(self, idx: int, freq: float, gain: float):
        """Drag-release — persist the change. In graphic mode the
        slider state IS the persistence; in AutoEQ mode we re-serialise
        the modified profile."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if autoeq_raw:
            try:
                parsed = json.loads(autoeq_raw)
                bands = list(parsed.get("bands", []))
                if 0 <= idx < len(bands):
                    bands[idx]["f"] = int(round(freq))
                    bands[idx]["g"] = float(gain)
                    # If the centre moved, the user-set Q stays put —
                    # recompute w under the same Q so the bandwidth in
                    # octaves stays consistent as the band slides.
                    from modules.eq_curve_editor import width_to_q

                    old_q = width_to_q(
                        float(bands[idx].get("f", freq)),
                        float(bands[idx].get("w", freq)),
                    )
                    bands[idx]["w"] = bands[idx]["f"] / max(0.1, old_q)
                    parsed["bands"] = bands
                    self.s.eq_autoeq_profile_json = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                return
        else:
            # Graphic mode — persist the modified slider gain. Freq is
            # locked in this mode so freq changes are ignored.
            try:
                gains = list(self.s.eq_bands)
                if 0 <= idx < len(gains):
                    gains[idx] = float(gain)
                    self.s.eq_bands = gains
            except Exception:
                return
        self._emit_eq_changed()

    def _on_curve_band_q_changed(self, idx: int, new_w_hz: float):
        """Wheel-over-node — adjust the band's bandwidth. PEQ mode
        only (the editor blocks wheel in graphic mode), so we always
        write to the AutoEQ profile."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            if 0 <= idx < len(bands):
                bands[idx]["w"] = float(new_w_hz)
                parsed["bands"] = bands
                self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._emit_eq_changed()

    def _on_curve_band_added(self, freq: float, gain: float):
        """Double-click-on-empty — append a new band at the clicked
        spot. Default Q = 1.0 (one-octave wide). PEQ mode only — the
        editor blocks the gesture in graphic mode."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            f = int(round(freq))
            bands.append({"f": f, "w": float(f), "g": float(gain), "t": 0})
            parsed["bands"] = bands
            self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    def _on_curve_band_removed(self, idx: int):
        """Right-click-on-node — drop the band. The editor refuses to
        remove the last one, so this can't end up with an empty list."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            if 0 <= idx < len(bands):
                del bands[idx]
                parsed["bands"] = bands
                self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._sync_curve_editor_from_bands()
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
        # AutoEQ profile loaded → graphic-band controls dim so the
        # user knows the slider gains aren't currently driving the
        # filter. Preset combo + Save/Delete dim for the same reason
        # (they only affect the graphic curve).
        autoeq_active = bool(self.s.eq_autoeq_profile_json)
        graphic_active = section_active and eq_on and not autoeq_active
        self._eq_preset_combo.setEnabled(graphic_active)
        self._eq_save_btn.setEnabled(graphic_active)
        self._eq_delete_btn.setEnabled(
            graphic_active and self._current_preset_is_user()
        )
        # Linear-phase sub-toggle is only meaningful when EQ is on +
        # not gated by cast or bit-perfect. The bit-perfect gating
        # path adds its own override below.
        if hasattr(self, "_eq_linear_phase_check"):
            self._eq_linear_phase_check.setEnabled(section_active and eq_on)
        for s in self._eq_sliders:
            s.setEnabled(graphic_active)
        for r in self._eq_readouts:
            # Readouts stay visible even when EQ is off so the user can
            # see what curve they have queued; just dim them further.
            r.setStyleSheet(
                f"color: {DISABLED_FG if not eq_on else TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
        # Band labels mirror the readouts — TEXT_DIM when on, deeper
        # DISABLED_FG when off — so the entire grid reads as one
        # cohesive "drawn in" cluster when the user enables EQ.
        for b in self._eq_band_labels:
            b.setStyleSheet(
                f"color: {DISABLED_FG if not eq_on else TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
        if cast_active:
            self._eq_caption.setText(
                "Casting — EQ applies to local playback only and is inactive now."
            )
            self._eq_caption.setVisible(True)
        else:
            self._eq_caption.setText("")
            self._eq_caption.setVisible(False)

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
        # EQ T3b — keep the curve editor in sync with slider edits so
        # toggling Simple → Curve doesn't show stale data. Cheap when
        # the editor is hidden (no paint until visible).
        self._sync_curve_editor_from_bands()
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
        self._discover_on_demand_radio = QRadioButton("Discover on cast")
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

        v.addSpacing(8)

        # ── Stream routing ─────────────────────────────────────────────
        # A cast device can only fetch a URL it can route to. "Network
        # cast" lets the device fetch from the server directly (normal
        # mode); "Route locally" relays the stream through a small
        # local HTTP server on this machine, the escape hatch for
        # offline / Tailscale / self-signed-cert / cross-VLAN setups
        # where the cast device can't reach the server itself. "Auto"
        # picks per-server based on URL reachability. Three-radio form
        # matches the Discovery timing section above so the page reads
        # as a consistent stack of single-pick groups.
        v.addWidget(self._section_header("Stream routing"))

        self._cast_routing_radios: dict = {}
        routing_group = QButtonGroup(self)
        current_routing = self.s.cast_stream_routing or "auto"
        # Fixed radio width for the two hinted rows so their faint
        # hint suffixes start at the same X position — aligns
        # "(normal mode)" and "(offline, special case)" in one
        # vertical column, easier to scan than ragged-right hints
        # tacked onto whatever each radio label happens to be wide.
        # 150 ≈ widest radio label + a tab's worth of breathing room.
        _HINT_RADIO_W = 150
        for label, hint, key in CAST_ROUTING_MODES:
            rb = QRadioButton(label)
            rb.setChecked(key == current_routing)
            routing_group.addButton(rb)
            if hint:
                # Faint hint suffix laid out as its own QLabel so the
                # parenthetical reads dim grey like Display's
                # "(needs restart)" suffix — QRadioButton's label
                # doesn't render rich text, so the suffix has to be a
                # separate widget to style independently.
                rb.setMinimumWidth(_HINT_RADIO_W)
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(6)
                row.addWidget(rb)
                hint_lbl = QLabel(hint)
                hint_lbl.setStyleSheet(
                    f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
                )
                row.addWidget(hint_lbl)
                row.addStretch(1)
                v.addLayout(row)
            else:
                v.addWidget(rb)
            self._cast_routing_radios[key] = rb
        # Keep the group alive on self so it doesn't get GC'd — see
        # the timing_group note above for the same trap.
        self._cast_routing_group = routing_group

        def _on_routing_changed(_checked: bool):
            for key, rb in self._cast_routing_radios.items():
                if rb.isChecked():
                    self.s.cast_stream_routing = key
                    return

        for rb in self._cast_routing_radios.values():
            rb.toggled.connect(_on_routing_changed)

        v.addStretch(1)
        return page

    def _on_replaygain_changed(self):
        mode = self._rg_combo.currentData() or "no"
        PlayerBus.get().replaygain_changed.emit(mode)

    def _on_bit_perfect_toggled(self, on: bool):
        """Master gate. On enable: force-persist the safe values for
        every dependent setting and broadcast the corresponding signals
        so live surfaces (volume slider, mpv replaygain, EQ filter
        chain, crossfade engine) catch up immediately. On disable: just
        flip the flag — dependents stay at their safe values until the
        user re-enables them explicitly. See
        ``docs/research/bit_perfect_playback.md`` §7 (T2)."""
        self.s.bit_perfect_mode = bool(on)
        bus = PlayerBus.get()
        if on:
            # Volume → 100. set_volume's own bit-perfect guard would
            # clamp future calls regardless, but pushing 100 now keeps
            # the slider widget + volume_state observers in sync.
            # Snapshot the pre-toggle volume so the OFF leg can restore
            # it — avoids the "vol stuck at 100 after toggling off"
            # surprise loudness.
            self.s.volume_pre_bit_perfect = int(self.s.volume)
            # Skip the push when casting — set_volume routes the value
            # to the cast device, which has its own independent volume
            # we don't want to blast. PlayerBackend's
            # ``_refresh_bit_perfect_active`` covers the local pipeline
            # whenever the cast ends and the contract takes hold.
            if not bus.cast_active:
                bus.volume_changed.emit(100)
            # ReplayGain → 'no'. Settings setter doesn't fire the bus
            # signal; mpv listens to replaygain_changed via
            # PlayerBackend.set_replaygain, which also persists.
            # Snapshot the prior mode so the OFF leg can restore it —
            # symmetric with the EQ remember-and-restore.
            self.s.replaygain_pre_bit_perfect = self.s.replaygain
            # Same shape for audio_quality. We DON'T force "original"
            # on enable (that would silently change the next stream's
            # URL — a big FLAC over a slow link is not what every user
            # wants). The combo is greyed by ``_refresh_bit_perfect_gating``
            # so the user can't pick a transcoded tier while the mode
            # is on; the snapshot exists so a user who deliberately
            # moved to "original" pre-toggle keeps that choice, and a
            # user who did NOT can rebound to whatever they had.
            self.s.audio_quality_pre_bit_perfect = self.s.audio_quality or "original"
            if self.s.replaygain != "no":
                bus.replaygain_changed.emit("no")
                self._select_combo_by_data(self._rg_combo, "no")
            # EQ → off. eq_changed payload carries (enabled, bands);
            # bands stay the user's curve so re-enabling later replays
            # it. PlayerBackend.apply_eq clears the filter on enabled=
            # False. Snapshot the prior ``eq_enabled`` so the OFF leg
            # of this toggle can restore the user's previous choice —
            # a user who lives with their EQ shouldn't have to re-tick
            # the box every time they flip the master gate.
            self.s.eq_enabled_pre_bit_perfect = bool(self.s.eq_enabled)
            if self.s.eq_enabled:
                self.s.eq_enabled = False
                bus.eq_changed.emit(False, list(self.s.eq_bands))
                if hasattr(self, "_eq_enabled_check"):
                    self._eq_enabled_check.blockSignals(True)
                    self._eq_enabled_check.setChecked(False)
                    self._eq_enabled_check.blockSignals(False)
                    self._refresh_eq_enabled_state()
            # Crossfade → off. crossfade.Crossfader observes
            # crossfade_enabled via the settings property directly each
            # gapless-handoff tick; the bus signal is for the now-playing
            # badge's live re-render. Snapshot the prior state symmetric
            # with EQ + ReplayGain.
            self.s.crossfade_enabled_pre_bit_perfect = bool(self.s.crossfade_enabled)
            if self.s.crossfade_enabled:
                self.s.crossfade_enabled = False
                bus.crossfade_changed.emit(False)
                if hasattr(self, "_xf_enabled"):
                    self._xf_enabled.blockSignals(True)
                    self._xf_enabled.setChecked(False)
                    self._xf_enabled.blockSignals(False)
                    self._refresh_xf_enabled_state()
        else:
            # OFF leg — emit ``bit_perfect_changed`` BEFORE any restore
            # emits. The backend's ``_on_bit_perfect_setting_changed``
            # listener refreshes ``bus.bit_perfect_active`` to False
            # synchronously, which is what ``set_volume``'s guard reads.
            # Without this ordering, the volume restore below would
            # route through ``bus.volume_changed`` → ``set_volume``,
            # find the still-True runtime flag, and clamp the prior
            # value back to 100 — leaving the user stuck at full
            # volume after they disabled the mode. The other restores
            # (EQ / RG / Crossfade / quality) don't have an equivalent
            # guard, so the ordering only matters for volume; emit
            # early so the whole leg sees a consistent state.
            bus.bit_perfect_changed.emit(False)

            # Restore the pre-bit-perfect DSP state so a user who runs
            # with EQ / ReplayGain / Crossfade enabled doesn't have to
            # re-tick three boxes every time they cycle the master
            # gate. We only restore when the snapshot says "was
            # active"; an unset / OFF / "no" snapshot means "leave
            # alone" so a user who deliberately turned a DSP off
            # before enabling bit-perfect isn't surprised by it coming
            # back. Each snapshot is cleared once consumed so the next
            # ON leg captures fresh state.
            if self.s.eq_enabled_pre_bit_perfect and not self.s.eq_enabled:
                self.s.eq_enabled = True
                bus.eq_changed.emit(True, list(self.s.eq_bands))
                if hasattr(self, "_eq_enabled_check"):
                    self._eq_enabled_check.blockSignals(True)
                    self._eq_enabled_check.setChecked(True)
                    self._eq_enabled_check.blockSignals(False)
                    self._refresh_eq_enabled_state()
            self.s.eq_enabled_pre_bit_perfect = False

            prior_rg = self.s.replaygain_pre_bit_perfect
            if prior_rg != "no" and self.s.replaygain == "no":
                bus.replaygain_changed.emit(prior_rg)
                self._select_combo_by_data(self._rg_combo, prior_rg)
            self.s.replaygain_pre_bit_perfect = "no"

            prior_q = self.s.audio_quality_pre_bit_perfect
            if prior_q and prior_q != (self.s.audio_quality or ""):
                self.s.audio_quality = prior_q
                bus.audio_quality_changed.emit(prior_q)
                self._select_combo_by_data(self._quality_combo, prior_q)
            self.s.audio_quality_pre_bit_perfect = ""

            if self.s.crossfade_enabled_pre_bit_perfect and not self.s.crossfade_enabled:
                self.s.crossfade_enabled = True
                bus.crossfade_changed.emit(True)
                if hasattr(self, "_xf_enabled"):
                    self._xf_enabled.blockSignals(True)
                    self._xf_enabled.setChecked(True)
                    self._xf_enabled.blockSignals(False)
                    self._refresh_xf_enabled_state()
            self.s.crossfade_enabled_pre_bit_perfect = False

            # Restore the pre-toggle volume — set_volume can now honour
            # values < 100 again. -1 sentinel means "no snapshot
            # active" (first-ever toggle, or already consumed).
            # Same cast guard as the ON leg: emitting while a cast is
            # live would route the value to the cast device's volume,
            # which we deliberately didn't touch. settings.volume
            # stayed at its pre-toggle value the whole time (cast's
            # set_volume early-returns without persisting), so mpv
            # already resumes correctly when the cast ends.
            prior_vol = self.s.volume_pre_bit_perfect
            if (
                prior_vol >= 0
                and prior_vol != self.s.volume
                and not bus.cast_active
            ):
                bus.volume_changed.emit(prior_vol)
            self.s.volume_pre_bit_perfect = -1
        self._refresh_bit_perfect_gating()
        # ON leg: emit AFTER snapshots so the snapshot setters in the
        # ON branch capture pre-toggle values. OFF leg already emitted
        # at the top of the branch so the volume-restore guard sees
        # the refreshed runtime flag.
        if on:
            bus.bit_perfect_changed.emit(True)

    def _refresh_bit_perfect_gating(self):
        """Grey out (or un-grey) every Playback control the bit-perfect
        toggle gates. Idempotent — safe to call from the toggle handler
        and from page-rebuild paths (live theme switch).

        The Exclusive sub-toggle is gated the inverse way to the rest:
        it's a SUB-option of Bit-perfect, so it's only enabled when
        Bit-perfect itself is on AND the source isn't transcoded —
        ``_make_mpv_handle`` requires ``audio_quality=="original"`` for
        exclusive to actually apply, so enabling the checkbox on a
        transcoded tier would just be a flag that does nothing."""
        on = bool(self.s.bit_perfect_mode)
        # ``_quality_combo`` is included alongside the DSP gates: the
        # bit-perfect contract requires ``audio_quality=="original"``
        # so a transcoded tier silently breaks it. Greying the combo
        # keeps the user from flipping it mid-session and ending up
        # in the "setting says Bit Perfect but the runtime flag is
        # off" zombie state. Sub-options (``_quality_*_combo`` for
        # the per-tier bitrate dropdowns, if any) ride the combo's
        # own enabled-state via their existing layout.
        for w in ("_quality_combo", "_rg_combo", "_xf_enabled",
                  "_xf_smart_album", "_xf_duration", "_eq_enabled_check",
                  "_eq_linear_phase_check"):
            widget = getattr(self, w, None)
            if widget is not None:
                widget.setEnabled(not on)
        quality_is_original = (
            (self.s.audio_quality or "").strip().lower() == "original"
        )
        if hasattr(self, "_audio_exclusive_check"):
            # Defence in depth: even with bit-perfect on, a stale
            # persisted audio_quality="low" (upgrade path, manual
            # QSettings edit) would let the user tick exclusive while
            # the underlying ``_make_mpv_handle`` gate refuses to honour
            # it. Gate the widget here too so the UI and the runtime
            # stay aligned.
            self._audio_exclusive_check.setEnabled(on and quality_is_original)
        if on:
            tip = "Disabled while Bit-perfect mode is on."
        else:
            tip = ""
        for w in ("_quality_combo", "_rg_combo", "_xf_enabled", "_eq_enabled_check"):
            widget = getattr(self, w, None)
            if widget is not None and on:
                widget.setToolTip(tip)
        if hasattr(self, "_audio_exclusive_check"):
            if not on:
                self._audio_exclusive_check.setToolTip(
                    "Enable Bit-perfect mode to use exclusive output."
                )
            elif not quality_is_original:
                self._audio_exclusive_check.setToolTip(
                    "Exclusive output requires audio quality 'Original' — "
                    "the server can't transcode and still ship bit-identical bits."
                )
            else:
                self._audio_exclusive_check.setToolTip("")
        # ReplayGain's existing long-form tooltip is informative; only
        # overwrite it while bit-perfect is on, restore on toggle-off
        # via a separate path (page rebuild + the tooltip restore below).
        if not on and hasattr(self, "_rg_combo"):
            self._rg_combo.setToolTip(
                "Normalization mode:\n"
                "  Off — play untouched.\n"
                "  Track — match each song to a common loudness target.\n"
                "  Album — preserve relative loudness within an album."
            )

    def _on_audio_exclusive_toggled(self, on: bool):
        """Persist + push to the live mpv handle. The new value takes
        effect on the next track (mpv's audio output is opened per
        play()). The shared-mode fallback in ``_make_mpv_handle`` covers
        DACs that refuse exclusive open (Windows WASAPI #11600/#11733).
        See ``docs/research/bit_perfect_playback.md`` §7 (T3)."""
        self.s.audio_exclusive = bool(on)
        PlayerBus.get().audio_exclusive_changed.emit(bool(on))

    def _refresh_pipewire_button_label(self):
        """Reflect current install state in the button label + tooltip.
        Idempotent — safe to call from the click handler and from the
        page-build path. No-op on non-Linux (the row doesn't exist)."""
        if not hasattr(self, "_pw_install_btn"):
            return
        from modules import pipewire_setup as _pws

        if _pws.is_installed():
            self._pw_install_btn.setText("Remove PipeWire bit-perfect config")
            self._pw_install_btn.setToolTip(
                f"Removes {_pws.conf_path()}. Restart pipewire to revert."
            )
        else:
            self._pw_install_btn.setText("Install PipeWire bit-perfect config")
            self._pw_install_btn.setToolTip(
                f"Writes {_pws.conf_path()} with rate-matching properties."
            )

    def _on_pipewire_install_clicked(self):
        """Toggle the conf file on disk. The label flips between
        Install / Remove on each click; a small inline status line
        confirms the action. T4 of
        ``docs/research/bit_perfect_playback.md`` §7."""
        from modules import pipewire_setup as _pws

        if _pws.is_installed():
            removed = _pws.uninstall()
            if removed:
                msg = "Removed. Restart pipewire to revert the rate-matching properties."
            else:
                # uninstall() returns False if the file lacks our
                # ID-stamp header — surface that honestly rather than
                # claiming success.
                msg = (
                    "Did not remove — the file at that path wasn't "
                    "installed by jellytoast. Edit it by hand if you "
                    "want to revert."
                )
        else:
            try:
                _pws.install()
                msg = (
                    "Installed. Restart pipewire (or log out / in) for "
                    "the rate-matching properties to take effect."
                )
            except OSError as e:
                msg = f"Install failed: {e}"
        self._pw_status_label.setText(msg)
        self._pw_status_label.setVisible(True)
        self._refresh_pipewire_button_label()

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
        self._theme_restart_notice = QLabel(
            "Restart jellytoast to apply the new font size."
        )
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

        self._theme_combo = _Selector()
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
        # Width capped to the accent-swatch row's footprint (7 × 28 px
        # swatches + 6 × 10 px gaps = 256 px) so the Theme combo
        # ends right where the Red swatch ends below. Without this,
        # the combo stretched the full page width and read disconnected
        # from the swatches it pairs with.
        self._theme_combo.setFixedWidth(256)
        # Align left in the vbox so the fixed width sits flush with
        # the rest of the page content's left edge rather than
        # centering in the row.
        v.addWidget(self._theme_combo, 0, Qt.AlignmentFlag.AlignLeft)

        # ── Accent color ───────────────────────────────────────────────
        v.addWidget(self._section_header("Accent color"))
        v.addLayout(self._build_accent_row())

        # Customize → jumps to the advanced (per-token) color editor.
        # That page is reachable only from this button; its left-nav
        # entry is hidden so casual users don't trip into a power-user
        # surface.
        advanced_row = QHBoxLayout()
        advanced_row.setContentsMargins(0, 8, 0, 0)
        self._open_colors_btn = QPushButton("Customize")
        self._open_colors_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_colors_btn.clicked.connect(self._jump_to_colors_page)
        advanced_row.addWidget(self._open_colors_btn)
        advanced_row.addStretch(1)
        v.addLayout(advanced_row)

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
        self._font_size_combo = _Selector()
        for label, key in LYRICS_FONT_SIZES:
            self._font_size_combo.addItem(label, key)
        self._initial_font_scale = self.s.font_scale
        self._select_combo_by_data(self._font_size_combo, self._initial_font_scale)
        self._font_size_combo.currentIndexChanged.connect(self._on_font_scale_changed)
        scaling_form.addRow(self._field_label("Font size:"), self._font_size_combo)

        # Lyrics font size — wires live to PlayerBus, no restart needed.
        self._lyrics_size_combo = _Selector()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        scaling_form.addRow(self._field_label("Lyrics font size:"), self._lyrics_size_combo)

        v.addLayout(scaling_form)

        # ── Now-playing info ────────────────────────────────────────────
        # Per-segment toggles for the streaming-info line that sits
        # above the transport row in the now-playing bar
        # ("Streaming · Bit Perfect · FLAC · 1411 kbps" etc.). Defaults
        # match typical listener expectations — codec + bitrate + the
        # active DSP that actually matters (Bit Perfect, EQ) on,
        # ReplayGain + Crossfade off because the people who use them
        # already know they're using them.
        v.addSpacing(4)
        v.addWidget(self._section_header("Now-playing info"))

        def _badge_toggle(attr: str, label: str) -> QCheckBox:
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.s, attr)))

            def _on(val, name=attr):
                setattr(self.s, name, bool(val))
                PlayerBus.get().streaming_info_badges_changed.emit()

            cb.toggled.connect(_on)
            return cb

        self._badge_bit_perfect = _badge_toggle(
            "streaming_info_show_bit_perfect", "Bit Perfect"
        )
        self._badge_eq = _badge_toggle("streaming_info_show_eq", "EQ")
        self._badge_replaygain = _badge_toggle(
            "streaming_info_show_replaygain", "Normalized"
        )
        self._badge_crossfade = _badge_toggle(
            "streaming_info_show_crossfade", "Crossfade"
        )
        self._badge_codec = _badge_toggle(
            "streaming_info_show_codec", "Codec (FLAC, MP3, …)"
        )
        self._badge_bitrate = _badge_toggle(
            "streaming_info_show_bitrate", "Bitrate (kbps)"
        )
        for w in (
            self._badge_bit_perfect, self._badge_eq, self._badge_replaygain,
            self._badge_crossfade, self._badge_codec, self._badge_bitrate,
        ):
            v.addWidget(w)

        # ── Interface ──────────────────────────────────────────────────
        v.addSpacing(4)
        v.addWidget(self._section_header("Interface"))

        self._tooltips_check = QCheckBox("Show hover tooltips")
        self._tooltips_check.setChecked(self.s.show_tooltips)
        self._tooltips_check.toggled.connect(lambda val: setattr(self.s, "show_tooltips", val))
        v.addWidget(self._tooltips_check)

        # Borderless main window — KDE Wayland only. Decoration is
        # decided when the window is constructed, so this is
        # restart-required; the toggle just persists the intent. The
        # "(needs restart)" suffix is a separate dim QLabel laid out
        # next to the checkbox so we can style it independently —
        # QCheckBox doesn't render rich text in its label.
        if keep_above_supported():
            nb_row = QHBoxLayout()
            nb_row.setContentsMargins(0, 0, 0, 0)
            nb_row.setSpacing(6)
            self._native_border_check = QCheckBox("Use native window border")
            self._native_border_check.setChecked(self.s.native_window_border)
            self._native_border_check.toggled.connect(
                lambda val: setattr(self.s, "native_window_border", val)
            )
            nb_row.addWidget(self._native_border_check)
            nb_hint = QLabel("(needs restart)")
            nb_hint.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            nb_row.addWidget(nb_hint)
            nb_row.addStretch(1)
            v.addLayout(nb_row)

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

        Only ``font_scale`` still requires a restart — ``design_tokens``
        reads it at import time. Theme mode and accent both live-apply
        via ``refresh_theme()`` + ``PlayerBus.theme_changed`` (see
        ``_on_theme_changed`` / ``_on_accent_picked``), so neither is
        part of the dirty check."""
        dirty = self.s.font_scale != self._initial_font_scale
        self._theme_restart_notice.setVisible(dirty)

    def _on_theme_changed(self):
        chosen = self._theme_combo.currentData() or "frosted_dark"
        if chosen == self.s.theme_mode:
            return
        self.s.theme_mode = chosen
        # Live-apply — theme mode now switches without a restart.
        # Refresh the ui_helpers + icon token constants BEFORE
        # broadcasting: theme_changed slots (per-surface _reapply_accent,
        # the window's _cascade_global_style, this dialog's
        # _reapply_dialog_accent_styling) re-stamp from the module-level
        # constants, and connection order isn't guaranteed — so the
        # values must already be fresh when the first slot fires.
        # Mirrors _on_accent_picked.
        from modules import icons as _icons
        from modules import ui_helpers as _uih

        _uih.refresh_theme()
        _icons.refresh_theme()
        PlayerBus.get().theme_changed.emit()
        self._refresh_restart_notice_visibility()
        # A light↔dark switch changes the text-token colours pages bake
        # into their labels — rebuild so the open dialog stays legible.
        # Deferred: we're inside the theme combo's signal and the combo
        # lives on the Display page _rebuild_pages_for_theme tears down.
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    # ── Page: Hotkeys ──────────────────────────────────────────────────
    # Read-only list of every keyboard shortcut the app responds to.
    # Customization (rebinding) is a future enhancement; for now the
    # page exists to make discoverable what's already wired in
    # JellytoastWindow.__init__ — Ctrl+F, /, Ctrl+Shift+L, etc.
    def _build_hotkeys(self) -> QWidget:
        from modules import hotkeys as _hotkeys

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # action_id -> (QKeySequenceEdit, warning QLabel, default_seq)
        self._hotkey_edits: dict = {}
        # action_id -> human label, for the conflict message.
        self._hotkey_labels = {
            e["action_id"]: e["label"] for e in _hotkeys.registry_actions()
        }

        v.addWidget(self._section_header("In-app shortcuts"))
        for entry in _hotkeys.registry_actions():
            v.addWidget(self._editable_hotkey_row(entry))

        reset_all = QPushButton("Reset all to defaults")
        reset_all.setObjectName("ghost")
        reset_all.clicked.connect(self._on_hotkeys_reset_all)
        v.addWidget(reset_all, 0, Qt.AlignmentFlag.AlignLeft)

        v.addSpacing(8)
        v.addWidget(self._section_header("Media keys"))
        v.addLayout(self._hotkey_row("Play / Pause", "Media Play"))
        v.addLayout(self._hotkey_row("Next track", "Media Next"))
        v.addLayout(self._hotkey_row("Previous track", "Media Previous"))
        note = QLabel(
            "Media keys route through MPRIS and your desktop's own "
            "shortcut settings — they can't be rebound here."
        )
        note.setWordWrap(True)
        # Cap the note width so it wraps onto two lines rather than
        # running edge-to-edge under the rebind rows; matches the
        # trailing right margin we use on the rows above so the
        # text column reads as one consistent block.
        note.setMaximumWidth(420)
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 4px;")
        v.addWidget(note)
        v.addStretch(1)
        return page

    def _editable_hotkey_row(self, entry: dict) -> QWidget:
        """One rebindable shortcut — label + a QKeySequenceEdit capture
        field + a per-row Reset, with a hidden inline conflict warning
        below the row."""
        from modules import hotkeys as _hotkeys

        action_id = entry["action_id"]
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        row = QHBoxLayout()
        # Trailing right margin pulls the QKeySequenceEdit + Reset
        # button leftward so they don't sit hard against the page's
        # right edge — leaves the right column of the page reading
        # as a column rather than a strip of edge-aligned widgets.
        row.setContentsMargins(0, 0, 140, 0)
        row.setSpacing(8)
        name = QLabel(entry["label"])
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)

        edit = QKeySequenceEdit()
        try:
            # One chord per action — no multi-step sequences.
            edit.setMaximumSequenceLength(1)
        except (AttributeError, TypeError):
            pass  # Qt < 6.5 — multi-chord accepted, harmless
        edit.setKeySequence(_hotkeys.get_sequence(action_id, entry["default_seq"]))
        edit.setMaximumWidth(170)
        edit.editingFinished.connect(
            lambda aid=action_id: self._on_hotkey_edited(aid)
        )
        row.addWidget(edit)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ghost")
        reset_btn.clicked.connect(
            lambda _=False, aid=action_id: self._on_hotkey_reset(aid)
        )
        row.addWidget(reset_btn)
        col.addLayout(row)

        warn = QLabel("")
        warn.setStyleSheet(f"color: {ERROR_FG}; {type_qss(TYPE_CAPTION)}")
        warn.setWordWrap(True)
        warn.setVisible(False)
        col.addWidget(warn)

        self._hotkey_edits[action_id] = (edit, warn, entry["default_seq"])
        return wrap

    # ── Hotkey helpers ──────────────────────────────────────────────────────

    def _hotkey_snapshot(self) -> dict:
        """Live page state — every row's *current* sequence, including
        edits not yet persisted — so the conflict check sees what the
        user is about to have, not just the saved set."""
        return {
            aid: edit.keySequence()
            for aid, (edit, _warn, _default) in self._hotkey_edits.items()
        }

    def _on_hotkey_edited(self, action_id: str):
        from modules import hotkeys as _hotkeys
        from modules.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        seq = edit.keySequence()
        if seq.isEmpty():
            # Cleared the field — treat as "reset to default".
            self._on_hotkey_reset(action_id)
            return
        conflict = _hotkeys.find_conflict(
            action_id, seq, bindings=self._hotkey_snapshot()
        )
        if conflict:
            other = self._hotkey_labels.get(conflict, conflict)
            warn.setText(f"Already used by “{other}” — pick another.")
            warn.setVisible(True)
            # Snap the field back to the persisted binding.
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
            edit.blockSignals(False)
            return
        warn.setVisible(False)
        _hotkeys.set_sequence(action_id, seq)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkey_reset(self, action_id: str):
        from modules import hotkeys as _hotkeys
        from modules.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        _hotkeys.reset(action_id)
        edit.blockSignals(True)
        edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
        edit.blockSignals(False)
        warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkeys_reset_all(self):
        from modules import hotkeys as _hotkeys
        from modules.player_state import PlayerBus

        _hotkeys.reset_all()
        for aid, (edit, warn, default_seq) in self._hotkey_edits.items():
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(aid, default_seq))
            edit.blockSignals(False)
            warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _hotkey_row(self, label: str, keys: str) -> QHBoxLayout:
        row = QHBoxLayout()
        # Trailing right margin matches `_editable_hotkey_row` so the
        # binding chips line up under the Reset buttons above rather
        # than drifting all the way to the page's right edge.
        row.setContentsMargins(0, 0, 140, 0)
        name = QLabel(label)
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)
        binding = QLabel(keys)
        binding.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"background: {ink_alpha(0.06)}; padding: 3px 9px; "
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
            border = f"{ink_alpha(0.14)}"
            bg = f"{ink_alpha(0.03)}"
        else:
            warning_text = (
                "If your music server already scrobbles "
                "(e.g. Navidrome's built-in Last.fm / ListenBrainz "
                "integration), leave these off — otherwise every "
                "track is counted twice."
            )
            border = f"{ink_alpha(0.10)}"
            bg = f"{ink_alpha(0.02)}"
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
        # Cap the token + URL line edits at a sensible width — at full
        # row stretch (the previous setup) they read absurdly wide
        # for the short string they hold. A trailing stretch on the
        # row pushes the Validate button left of the page's right
        # edge to match.
        self._lb_token_edit.setMaximumWidth(260)
        token_row.addWidget(self._lb_token_edit)
        self._lb_validate_btn = QPushButton("Validate")
        self._lb_validate_btn.setObjectName("ghost")
        self._lb_validate_btn.clicked.connect(self._on_lb_validate)
        token_row.addWidget(self._lb_validate_btn)
        token_row.addStretch(1)
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
        self._lb_url_edit.setMaximumWidth(260)
        url_row.addWidget(self._lb_url_edit)
        url_row.addStretch(1)
        v.addLayout(url_row)

        # ── Last.fm ───────────────────────────────────────────────────
        # Deferred 2026-05-20 — the in-app API credentials aren't
        # registered (Last.fm's signup WAF kept blocking it), so the
        # whole section stays hidden rather than showing an apologetic
        # "coming soon" placeholder. ``is_configured()`` is False while
        # the API key is empty; populate the key to bring it back.
        # ListenBrainz above is the supported scrobbling path.
        if _lastfm.is_configured():
            v.addWidget(self._section_header("Last.fm"))
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
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from modules.async_io import run_async
        from modules.scrobble import lastfm as _lf

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
        from modules.async_io import run_async
        from modules.scrobble import lastfm as _lf

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
        rather than a settings tab.

        Renders as a borderless frosted pill matching the tooltip /
        volume-popup lift tone (see ``popup_paint_qcolor``). On KDE
        Wayland a noborder KWin rule (modules.keep_above) strips the
        decoration so the custom titlebar reads as the only chrome;
        on other platforms ``FramelessWindowHint`` does the same.
        """
        dlg = _AboutDialog(self)
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

    def _jump_to_colors_page(self):
        """Jump from the Display page to the Colors page. Hooked to
        the "Open advanced color editor →" link on the Display page."""
        for i in range(self.nav.count()):
            if self.nav.item(i).text() == "Colors":
                self.nav.setCurrentRow(i)
                break

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
            btn = _AccentSwatch(hex_value)
            btn.setToolTip(label)
            btn.setChecked(hex_value.lower() == current)
            btn.clicked.connect(lambda _=False, h=hex_value: self._on_accent_picked(h))
            self._accent_buttons.append((hex_value, btn))
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        row.addStretch(1)
        return row

    def _build_and_apply_dialog_stylesheet(self):
        """Compose the dialog-level QSS that styles every QComboBox
        and QPushButton#ghost inside the dialog. Pulled into its own
        method so we can rebuild it from `_on_accent_picked` to
        live-apply a new accent — re-reads `ACCENT` from
        `modules.ui_helpers` at call time, so the refresh_theme()
        path that ran just before this sees its new value."""
        from modules.icons import icon_svg_path
        from modules.ui_helpers import ACCENT as _ACCENT_NOW
        from modules.ui_helpers import check_url_for_accent

        # Brighter chevron (TEXT, not TEXT_DIM) so the dropdown affordance
        # actually reads as a dropdown — the dim version was too easy to
        # miss against the frosted background.
        chevron_path = icon_svg_path("chevron_down", TEXT)
        chevron_url = chevron_path.replace("\\", "/")
        # Accent-tinted check mark. GLOBAL_STYLE styles QCheckBox, but
        # it's set on the main window and this dialog is a separate
        # top-level — without the rules below the dialog's checkboxes
        # fall back to the native OS rendering (a blue check on Windows,
        # never the accent).
        check_url = check_url_for_accent()
        _ar, _ag, _ab = _hex_to_rgb(_ACCENT_NOW)
        self.setStyleSheet(f"""
            QComboBox {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                /* Per-side border declarations rather than the shorthand
                   ``border:`` — Qt's QStyleSheetStyle renders QComboBox
                   bottom border at a sub-pixel position when the
                   shorthand drives all four sides, leaving the bottom
                   visibly thinner than the top/sides. Per-side rules
                   force consistent 1-px strokes on every edge. */
                border-top: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-bottom: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-left: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-right: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: 6px;
                padding: 6px 12px;
                {type_qss(TYPE_BODY)}
                /* No min-height — when paired with the General page's
                   setFixedHeight(34) the combined min-height 22 +
                   padding 12 + border 2 = 36 px natural exceeds the
                   34 px frame, so Qt clipped the bottom row of pixels
                   and the bottom border appeared half-missing.
                   setFixedHeight on callers that want a specific size,
                   natural sizing for the rest. */
                /* Suppress Qt's native focus / selection rectangle —
                   without this the combo paints a faint extra line at
                   the bottom (the focus indicator) that reads as a
                   double-border against our accent QSS border. */
                outline: 0;
                selection-background-color: transparent;
                selection-color: {TEXT};
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
            /* _Selector — QPushButton + QMenu replacement for combos.
               Visual treatment matches QComboBox above; the trailing
               chevron is a QSS background-image pinned to the right
               edge. Text-align left so the value reads from the left
               edge like a combo. Padding-right reserves space for the
               chevron. */
            QPushButton#jtSelector {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: 6px;
                padding: 6px 32px 6px 12px;
                {type_qss(TYPE_BODY)}
                text-align: left;
                outline: 0;
            }}
            QPushButton#jtSelector:hover {{
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QPushButton#jtSelector:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QPushButton#jtSelector:disabled {{
                color: {TEXT_FAINT};
            }}
            QComboBox QAbstractItemView {{
                background: {POPUP_OPAQUE_FILL};
                color: {TEXT};
                border: none;
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
                background: {ink_alpha(0.04)};
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
            QPushButton#ghost:disabled {{
                background: {ink_alpha(0.02)};
                color: {DISABLED_FG};
                border-color: {ink_alpha(0.10)};
            }}
            /* Plain (non-ghost) QPushButton — Save / Delete on the EQ
               row use this. :disabled greys background, text, and
               border so the buttons obviously read as inactive when
               the parent toggle (Equalizer Enable) is off. */
            QPushButton:disabled {{
                background: {ink_alpha(0.02)};
                color: {DISABLED_FG};
                border: 1px solid {ink_alpha(0.08)};
            }}
            QLineEdit, QKeySequenceEdit {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 6px 12px;
                min-height: 22px;
            }}
            QLineEdit:focus, QKeySequenceEdit:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QCheckBox {{
                color: {TEXT}; spacing: 8px; background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {BORDER}; border-radius: 3px;
                background: {ink_alpha(0.04)};
            }}
            QCheckBox::indicator:hover {{
                border-color: {ink_alpha(0.30)};
            }}
            QCheckBox::indicator:checked {{
                background: rgba({_ar},{_ag},{_ab},0.15);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                image: url({check_url});
            }}
            QCheckBox::indicator:checked:hover {{
                background: rgba({_ar},{_ag},{_ab},0.28);
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QCheckBox::indicator:disabled {{
                border-color: {ink_alpha(0.10)};
                background: {ink_alpha(0.02)};
            }}
            /* Checked + disabled — strip the accent fill so a checked
               "Skip on albums" doesn't read at full accent purple when
               its parent crossfade toggle is off. */
            QCheckBox::indicator:checked:disabled {{
                background: {ink_alpha(0.04)};
                border: 1px solid {ink_alpha(0.15)};
                image: none;
            }}
            QCheckBox:disabled {{
                color: {DISABLED_FG};
            }}
            QRadioButton {{
                color: {TEXT}; spacing: 8px; background: transparent;
            }}
            QRadioButton::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {BORDER}; border-radius: 9px;
                background: {ink_alpha(0.04)};
            }}
            QRadioButton::indicator:hover {{
                border-color: {ink_alpha(0.30)};
            }}
            QRadioButton::indicator:checked {{
                background: rgba({_ar},{_ag},{_ab},0.85);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.85);
            }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {ink_alpha(0.03)}; width: 8px;
                border-radius: 4px; margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({_ar},{_ag},{_ab},0.4);
                border-radius: 4px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

    def _on_accent_picked(self, hex_value: str):
        # 1. Persist the pick.
        self.s.accent_color = hex_value
        # 1a. Clear every accent-derived override set via Settings →
        #     Colors. ACCENT_DEEP (checkbox fills) + BORDER_ACCENT
        #     (focus rings) are conceptually downstream of ACCENT —
        #     picking a fresh preset means "I want this preset's whole
        #     accent family". Without this, the user picks purple but
        #     checkbox fills + focus rings stay at the previously-
        #     saved override (green) because refresh_theme's
        #     load_persisted_overlays re-applies them.
        from PySide6.QtCore import QSettings

        _qs = QSettings()
        for _tok in ("ACCENT", "ACCENT_DEEP", "BORDER_ACCENT"):
            _qs.remove(f"debug/colors/{_tok}")
        # 2. Refresh module-level theme constants + rebuild
        #    GLOBAL_STYLE + clear ICON_ACCENT cache, so anything that
        #    re-reads `ui_helpers.ACCENT` / calls `accent_icon(name)`
        #    after this line sees the new colour.
        from modules import icons as _icons
        from modules import ui_helpers as _uih

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
            #
            # Limit to *visible* widgets — invisible ones (collapsed
            # settings pages, the mini player while hidden, dialogs
            # that aren't open) get a fresh style polish from Qt's
            # showEvent chain when they next become visible, so
            # polishing them now is wasted work. On a fully populated
            # app this cuts the loop count significantly.
            for w in app.allWidgets():
                if isinstance(w, (QCheckBox, QRadioButton)) and w.isVisible():
                    w.style().unpolish(w)
                    w.style().polish(w)
                    w.update()
        # 4. Update the swatch ring states in the picker so the new
        #    selection reads as checked + others as idle.
        for h, btn in self._accent_buttons:
            btn.set_accent_state(h, h.lower() == hex_value.lower())
        # 5. Rebuild the dialog's own accent-derived QSS (combo
        #    borders, ghost buttons, restart-notice banner). Without
        #    this the settings dialog itself would lag the rest of
        #    the app until reopened. accent_only=True skips the body
        #    repaint + compositor blur toggle that only matter on a
        #    theme-mode flip.
        self._reapply_dialog_accent_styling(accent_only=True)
        # 6. Broadcast — top-level surfaces that opt in will rebuild
        #    their accent-using stylesheets on this signal.
        PlayerBus.get().theme_changed.emit()
        # 7. Theme + font_scale are still bake-at-import-time changes;
        #    keep the restart notice visible if any of those differ
        #    from the initial values when the dialog was opened.
        self._refresh_restart_notice_visibility()

    def _reapply_dialog_accent_styling(self, accent_only: bool = False):
        """Rebuild every QSS string in the dialog that bakes ACCENT.
        Called on accent change so the dialog reflects the new colour
        immediately instead of waiting for a reopen.

        ``accent_only=True`` is the fast path: the user picked a new
        accent but the theme mode + body fill haven't changed, so we
        can skip the compositor blur toggle (no-op for accent) and the
        body-paint refresh — both of which only matter when
        DIALOG_BODY_COLOR's opacity changes (theme-mode flip)."""
        # Reapply the top-level dialog stylesheet (QComboBox borders,
        # ghost button outlines, popup item styling). The stylesheet
        # is built once in __init__ from `ACCENT` literals; rebuilding
        # means re-running the same code with the now-refreshed
        # module-level constant.
        self._build_and_apply_dialog_stylesheet()
        # Restart notice fill / left bar use the active accent. Rebuild
        # its inline stylesheet so its colour matches the new accent.
        # hasattr-guarded — the Display page (which owns the notice) is
        # built lazily, so this slot can fire before it exists (e.g. a
        # Colors-page slider drag while Display was never opened).
        if hasattr(self, "_theme_restart_notice"):
            from modules.ui_helpers import ACCENT as _ACCENT_NOW

            _ar2, _ag2, _ab2 = _hex_to_rgb(_ACCENT_NOW)
            self._theme_restart_notice.setStyleSheet(
                f"color: {TEXT}; "
                f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
                f"border-radius: 6px; "
                f"padding: 10px 14px; "
                f"{type_qss(TYPE_CAPTION)}"
            )
        # _Selector builds its menu fresh on every open via opaque_menu(),
        # which re-reads the current accent each call — so a theme
        # change is picked up automatically. No cache to reset.
        # EQ slider handles bake ACCENT into their per-slider QSS at
        # construction time — re-stamp so the dot changes colour
        # live with the accent. No-op if the EQ section hasn't been
        # built yet (e.g. the user hasn't visited the Playback page).
        if hasattr(self, "_eq_sliders"):
            for s in self._eq_sliders:
                s.setStyleSheet(self._eq_slider_qss())
        # Crossfade duration slider bakes ACCENT too — re-stamp so its
        # filled track + hover-handle colour follows live.
        if hasattr(self, "_xf_duration"):
            self._xf_duration.setStyleSheet(self._horiz_slider_qss())
        # Re-polish every QCheckBox / QRadioButton inside the dialog.
        # The setStyleSheet call above on `self` invalidates child
        # styling and Qt won't re-evaluate the QApplication-level
        # `QCheckBox::indicator:checked { background: ACCENT_DEEP }`
        # rule without an explicit polish kick. Without this, the
        # main window's _cascade_global_style does run + repolish all
        # checkboxes app-wide, but the dialog's setStyleSheet (which
        # fires AFTER on theme_changed via this method's subscribe)
        # undoes that work — checkboxes stick at the old accent.
        #
        # Skip invisible widgets — on a theme-mode change the deferred
        # _rebuild_pages_for_theme is about to destroy every checkbox
        # in lazy-built non-current pages, so polishing them now is
        # work on dead widgets. On an accent change those pages aren't
        # rebuilt, but their checkboxes will repolish via Qt's
        # showEvent chain the next time the user visits the page.
        for cb in self.findChildren(QCheckBox):
            if not cb.isVisible():
                continue
            cb.style().unpolish(cb)
            cb.style().polish(cb)
            cb.update()
        for rb in self.findChildren(QRadioButton):
            if not rb.isVisible():
                continue
            rb.style().unpolish(rb)
            rb.style().polish(rb)
            rb.update()
        # Repaint the dialog's own body — paintEvent fills
        # DIALOG_BODY_COLOR, which differs across theme modes (frosted
        # vs solid vs transparent). A QSS re-stamp alone won't trigger
        # the custom paintEvent. Skip on the accent-only fast path —
        # accent doesn't change DIALOG_BODY_COLOR, so the existing
        # paint is still correct, and skipping saves a full repaint.
        if not accent_only:
            self.update()
            # Frosted blurs behind the dialog; Transparent / Solid
            # don't. Defer so the paintEvent triggered by self.update()
            # above has already refreshed the body fill, and toggle
            # off→on so KWin re-installs the blur even when the previous
            # theme also had blur (re-issuing the same enableBlurBehind
            # call is otherwise deduped and the live-switch lands
            # without visible blur). Accent picks don't touch blur, so
            # the toggle is a wasted compositor round-trip there.
            QTimer.singleShot(0, lambda: self._apply_blur(force_refresh=True))

    def _apply_blur(self, force_refresh: bool = False):
        """Blur behind the dialog when the active theme is frosted.
        Region shaped to the BODY_RADIUS rounded rect so the blur
        doesn't bleed past the dialog's transparent corners. Silent
        no-op where the compositor has no blur support.

        ``force_refresh=True`` toggles blur off then on — used on live
        theme switches where the compositor would otherwise dedupe the
        re-issued enable call and leave the dialog unblurred."""
        from modules import blur
        from modules.theme import get_active_theme

        want_blur = get_active_theme().blur
        if force_refresh and want_blur:
            blur.apply(self, False, corner_radius=self.BODY_RADIUS)
        blur.apply(self, want_blur, corner_radius=self.BODY_RADIUS)

    def showEvent(self, e):
        super().showEvent(e)
        # Apply compositor blur once the dialog has a mapped surface.
        QTimer.singleShot(0, self._apply_blur)
        QTimer.singleShot(0, self._polish_current_page)
        QTimer.singleShot(0, self.activateWindow)
        QTimer.singleShot(0, self.raise_)

    def _polish_current_page(self):
        current = self.stack.currentWidget()
        if current is None:
            return
        current.ensurePolished()
        for child in current.findChildren(QComboBox):
            child.ensurePolished()

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
            # Read live from ui_helpers — the `from … import` binding is
            # frozen at import, but refresh_theme() rebinds the module
            # attribute on a theme-mode switch (body opacity differs
            # across modes).
            from modules import ui_helpers as _uih

            p.setBrush(QColor(*_uih.DIALOG_BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
