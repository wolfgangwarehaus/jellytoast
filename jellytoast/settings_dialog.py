"""
jellytoast settings dialog. Frameless + frosted to match the main window
and mini player. Sidebar nav on the left, page content on the right.

Sections:
- General  — startup destination, window/tray behavior
- Account  — server URL, sign-out
- Appearance — theme mode (dark/light, live-applied) + accent color
- Display  — font + UI scaling
- About    — version + description

Settings that need the host window to react (sign-out, server change)
are emitted as signals; the host listens and acts.
"""

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_SUBHEAD,
    font,
    rad,
    type_qss,
)
from jellytoast.icon_button import IconButton
from jellytoast.icons import icon
from jellytoast.player_state import PlayerBus
from jellytoast.selector import (
    Selector as _Selector,  # noqa: E402  (deliberate post-class import for layout)
)
from jellytoast.settings import get_settings
from jellytoast.settings_page_casting import _CastingPageMixin
from jellytoast.settings_page_display import _DisplayPageMixin
from jellytoast.settings_page_general import _GeneralPageMixin
from jellytoast.settings_page_hotkeys import _HotkeysPageMixin
from jellytoast.settings_page_playback import _PlaybackPageMixin
from jellytoast.settings_page_scrobbling import _ScrobblingPageMixin
from jellytoast.settings_widgets import (  # noqa: F401 — re-exported
    _THEME_MODE_CHOICES,
    AUDIO_FAMILY_GREEN,
    AUDIO_FAMILY_PURPLE,
    AUDIO_QUALITIES,
    CAST_ROUTING_MODES,
    HOME_DESTINATIONS,
    LYRICS_FONT_SIZES,
    REPLAYGAIN_MODES,
    _AboutDialog,
    _AccentSwatch,
    _EyedropperSwatch,
    audio_family_dot_color,
    bit_perfect_path_hint,
)
from jellytoast.theme import _hex_to_rgb
from jellytoast.ui_helpers import (
    BORDER,
    DISABLED_FG,
    POPUP_OPAQUE_FILL,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WASH_HOVER,
    ink_alpha,
)


class SettingsDialog(
    _GeneralPageMixin,
    _PlaybackPageMixin,
    _CastingPageMixin,
    _DisplayPageMixin,
    _HotkeysPageMixin,
    _ScrobblingPageMixin,
    QDialog,
):
    # Matched to the host-OS window-corner radius (see RADIUS_WINDOW).
    BODY_RADIUS = RADIUS_WINDOW

    sign_out_requested = Signal()
    # Carries the new URL the user committed inline. Main window
    # reads it directly rather than re-prompting — the field above
    # the button IS the prompt.
    server_change_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from jellytoast.platform_compat import is_mobile_shell

        if is_mobile_shell() or self._compact:
            # The theme row (combo + Import) must fit a ~370 px phone page
            # (or Settings opened from a narrow desktop window).
            self._THEME_COMBO_W = 205
        # Delete the C++ object when the dialog finishes. It's rebuilt
        # fresh on every open (jellytoast.app._open_settings drops its ref on
        # finished) and several pages connect bound-method slots to the
        # PlayerBus singleton; without this the singleton keeps every
        # closed dialog alive forever (one leaked dialog per open) and its
        # stale slots keep firing. WA_DeleteOnClose covers a window-X
        # close; finished→deleteLater covers the accept()/reject() button
        # paths (done() hides without a close event), so both are needed.
        self.setAttribute(Qt.WA_DeleteOnClose)
        # A glass-opacity drag that settles within 120ms of the dialog closing
        # would otherwise lose its full re-stamp (the settle QTimer dies with
        # the dialog), leaving cached-body surfaces stale until the next theme
        # event. Flush any pending settle synchronously before teardown.
        self.finished.connect(self._flush_pending_glass_settle)
        self.finished.connect(self.deleteLater)
        self.s = get_settings()
        # Distinct title so the KWin `noborder` rule (jellytoast.keep_above)
        # can scope-match this dialog without catching the main window.
        from jellytoast.keep_above import SETTINGS_WINDOW_TITLE

        self.setWindowTitle(SETTINGS_WINDOW_TITLE)
        # 820→720: pulls a chunk of empty real-estate off the right
        # side of the dialog. Playback is the widest page (EQ band
        # grid + trailing Save/Delete buttons); the rest of the
        # pages adapt to the tighter content width page by page.
        # Height 540→620: lets the Playback page (Audio / Crossfade /
        # EQ / Shuffle) sit fully visible without invoking the page
        # scroller.
        #
        # 720×620 is the tuned baseline for the default font (Inter @ default
        # scale). It's scaled proportionally with the active font so a larger
        # font scale or a wide/monospace family gets a proportionally bigger
        # dialog (and a small scale a smaller one) instead of clipping — the
        # pages scroll for any residual overflow. See _adaptive_dialog_size.
        self._apply_dialog_size()

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
        # (jellytoast.keep_above.install_noborder_rules) strips the chrome.
        # KWin's blur effect drops blur for *undecorated* windows while
        # they move, so a frameless dialog flickers when dragged — a
        # decorated + noborder window keeps its blur.
        from jellytoast.platform_compat import is_kde_wayland

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
        self._body_layout = body_h
        body_h.setContentsMargins(10, 0, 14, 14)
        # 2→12 gap between nav column and the page stack: gives the
        # right pane visible room rather than touching the sidebar's
        # selected-row chip.
        body_h.setSpacing(12)

        self.nav = QListWidget()
        # Never show a horizontal scrollbar on the sidebar — if a wide/large
        # font needs more room we widen the column instead (see
        # _size_nav_to_content, re-run on live font changes), which reads far
        # better than a scrollbar under the nav labels.
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Initial width; _size_nav_to_content overrides it once the rows exist.
        self.nav.setFixedWidth(128)
        # Keyboard-navigable sidebar: Tab reaches it, Up/Down switch pages
        # (currentRowChanged rebuilds+shows). It was NoFocus, which made the
        # whole dialog unreachable by keyboard past the first control.
        self.nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
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
        # General is scrollable like the other content pages. It used to be
        # expand=True (unscrolled) to dodge a hair-trigger scrollbar that
        # "jiggled" at the old fixed 820×540 size — but with the adaptive size
        # + live fonts, a wide/large font makes it overflow and CLIP the bottom
        # (the Updates row + "Check now" got cut off). The scroll wrapper now
        # uses an autofade scrollbar (hidden when idle), so it scrolls cleanly
        # on overflow with no persistent-scrollbar jiggle.
        self._add_page("General", self._build_general)
        self._add_page("Playback", self._build_playback)
        self._add_page("Casting", self._build_casting)
        self._add_page("Downloads", self._build_downloads, expand=True)
        self._add_page("Display", self._build_display)
        self._add_page("Hotkeys", self._build_hotkeys)
        self._add_page("Scrobbling", self._build_scrobbling)
        # The per-token "Colors" editor (jellytoast.settings_colors_page) is
        # intentionally NOT registered as a page — it's a hidden power-user
        # subsystem with no UI entry point. Its persisted overrides still apply
        # at boot via color_tokens.load_persisted_overrides(); the accent
        # picker + screen eyedropper on the Display page remain the only colour
        # controls users see.

        # Width the sidebar to its longest label in the ACTIVE font now that
        # every page (and thus every nav row) has been registered — a fixed
        # 128px clipped "Scrobbling"/"Downloads" in a wide/monospace font.
        self._size_nav_to_content()

        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)  # fires _on_nav_changed → builds page 0
        if self._compact:
            self._install_compact_nav()
        # Seed keyboard focus on the sidebar so the dialog opens ready for
        # Up/Down page-switching.
        self.nav.setFocus(Qt.FocusReason.OtherFocusReason)

        # Live-apply: when the accent picker / eyedropper fires
        # theme_changed, re-stamp every accent-baked surface in the
        # dialog — combo borders, ghost-button outlines, restart
        # notice banner, EQ slider handles.
        try:
            PlayerBus.get().theme_changed.connect(self._reapply_dialog_accent_styling)
        except Exception:
            pass
        # An OS-driven "Auto (follow OS)" swap fires theme_changed but — unlike
        # the in-dialog combo path — doesn't rebuild this dialog's pages, so
        # baked text (nav labels, section headers) keeps its old colour and
        # reads wrong after a light↔dark flip. Track the resolved theme name
        # and rebuild the pages (deferred) whenever it changes from OUTSIDE the
        # dialog. _on_theme_changed updates _last_theme_name itself, so an
        # in-dialog pick won't double-rebuild.
        from jellytoast.theme import get_active_theme as _gat_init

        self._last_theme_name = _gat_init().name
        try:
            PlayerBus.get().theme_changed.connect(self._on_external_theme_changed)
        except Exception:
            pass

    @property
    def _compact(self) -> bool:
        """Phone width (the main window is compact — Plasma Mobile, or a
        narrow desktop window): the sidebar becomes a section dropdown and
        the dialog stops pinning a fixed desktop size."""
        from jellytoast import responsive

        return responsive.is_compact()

    def _apply_dialog_size(self) -> None:
        if not self._compact:
            self.setFixedSize(*self._adaptive_dialog_size())
            return
        # Resizable, filling the screen; a mobile shell maximizes it anyway
        # (it can't maximize a fixed-size window — it would sit 720 wide).
        from PySide6.QtWidgets import QApplication

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        scr = self.screen() or QApplication.primaryScreen()
        if scr is not None:
            self.resize(scr.availableGeometry().size())

    def _install_compact_nav(self) -> None:
        """Swap the sidebar for a section dropdown above the page: at phone
        width the 128 px column left the pages ~200 px."""
        self.nav.setVisible(False)
        picker = _Selector()
        for i in range(self.nav.count()):
            picker.addItem(self.nav.item(i).text(), i)
        picker.setCurrentIndex(self.nav.currentRow())
        picker.currentIndexChanged.connect(self.nav.setCurrentRow)
        # show_page() / keyboard moves drive the nav — keep the picker in step.
        self.nav.currentRowChanged.connect(
            lambda row: picker.currentIndex() != row and picker.setCurrentIndex(row)
        )
        self._body_layout.setDirection(QBoxLayout.Direction.TopToBottom)
        self._body_layout.insertWidget(0, picker)
        self._body_layout.setContentsMargins(12, 0, 12, 12)
        self._section_picker = picker

    def _adaptive_dialog_size(self) -> tuple[int, int]:
        """The dialog size for the active font. Keeps the tuned 720×620
        balance but scales it: by the font-size scale in BOTH dimensions
        (larger scale → bigger, small → smaller), and by a WIDTH-only factor
        for wide/monospace families so their extra character width doesn't
        overflow. Capped to the screen; residual overflow scrolls per-page."""
        from PySide6.QtGui import QFontInfo
        from PySide6.QtWidgets import QApplication

        from jellytoast.design_tokens import FONT_SCALE

        base_w, base_h = 720, 620
        # HEIGHT scales fully with the font-size scale — bigger text genuinely
        # needs proportionally more vertical room (and the pages scroll anyway).
        h = round(base_h * FONT_SCALE)
        # WIDTH scales only PARTIALLY. The widest text line grows with the font,
        # but the layout has horizontal slack (left-aligned content, right-
        # hugging buttons), so scaling width by the full factor sprawls the
        # dialog and leaves dead space on the right at larger sizes. Half the
        # scale delta keeps it comfortably wide without pushing everything apart.
        # Default scale (1.0) still lands exactly on the tuned 720 ("where we
        # started"). A monospace family packs wider glyphs → a small width-only
        # bump (nothing changes for proportional fonts).
        width_scale = 1.0 + (FONT_SCALE - 1.0) * 0.15
        width_family = 1.06 if QFontInfo(QApplication.font()).fixedPitch() else 1.0
        w = round(base_w * width_scale * width_family)
        scr = self.screen() or QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(w, avail.width() - 64)
            h = min(h, avail.height() - 64)
        return int(w), int(h)

    def _size_nav_to_content(self) -> None:
        """Width the sidebar to its longest label in the active font + the
        item/list padding, so a wide/monospace font never clips a nav row
        (it was a fixed 128px tuned for Inter)."""
        fm = self.nav.fontMetrics()
        text_w = max(
            (fm.horizontalAdvance(self.nav.item(i).text()) for i in range(self.nav.count())),
            default=96,
        )
        # item h-padding (14+14) + list padding (4+4) + generous breathing so a
        # font whose rendered advance slightly exceeds fontMetrics still fits
        # (the horizontal scrollbar is off, so the column must be wide enough).
        self.nav.setFixedWidth(int(text_w) + 28 + 8 + 16)

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
                border-radius: {rad(8)}px;
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
        # Narrow Settings (360 px): 12 px sides so the pages' widest rows fit.
        side = 12 if self._compact else 20
        v.setContentsMargins(side, 15, side, 14)
        v.setSpacing(0)
        if expand:
            self.stack.addWidget(wrap)
        else:
            scroller = QScrollArea()
            scroller.setWidget(wrap)
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QFrame.Shape.NoFrame)
            scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroller.setStyleSheet("background: transparent;")
            scroller.viewport().setStyleSheet("background: transparent;")
            # Auto-fading scrollbar — fades out when not actively scrolling,
            # same treatment as the radio / downloads / cast lists.
            from jellytoast.ui_helpers import install_autofade_scrollbars

            install_autofade_scrollbars(scroller)
            self.stack.addWidget(scroller)
        # (builder, expand, wrapper-layout) — wrapper-layout is where the
        # built content is added; the scroll wrapping is transparent to
        # _on_nav_changed / _rebuild_pages_for_theme.
        self._page_builders.append((builder, expand, v))

    def show_page(self, title: str) -> bool:
        """Select the nav page whose title matches ``title`` (lazy-builds
        it via the row-change handler). Returns False if no such page —
        used to deep-link into Settings (e.g. the cast button → Casting).
        """
        for i in range(self.nav.count()):
            if self.nav.item(i).text() == title:
                self.nav.setCurrentRow(i)
                return True
        return False

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
        import shiboken6

        if not shiboken6.isValid(self):
            # The deferral window means a theme change landing as the dialog
            # closes (OS auto-mode flip, accent watcher, keep/revert timeout)
            # can fire this on an already-deleted dialog — a RuntimeError
            # traceback on stderr. Nothing to rebuild; bail.
            return
        global TEXT, TEXT_DIM, TEXT_FAINT, ERROR_FG, BORDER, POPUP_OPAQUE_FILL
        global WASH_HOVER
        from jellytoast import ui_helpers as _u

        TEXT, TEXT_DIM, TEXT_FAINT = _u.TEXT, _u.TEXT_DIM, _u.TEXT_FAINT
        ERROR_FG, BORDER = _u.ERROR_FG, _u.BORDER
        POPUP_OPAQUE_FILL = _u.POPUP_OPAQUE_FILL
        WASH_HOVER = _u.WASH_HOVER

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
        # The sidebar nav isn't a page — re-stamp its QSS directly, then
        # re-widen it to the labels in the (possibly rescaled) active font.
        self.nav.setStyleSheet(self._nav_qss())
        self._size_nav_to_content()
        # The titlebar is built once and never rebuilt — re-stamp the
        # "Settings" heading and re-issue the cog glyph in the new tint.
        self._title_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        self._title_cog.setPixmap(icon("settings").pixmap(QSize(18, 18)))
        # The info (ⓘ) + close (✕) buttons bake colour-tokens / themed
        # icons too — re-issue the info glyph and re-stamp both QSS blocks
        # so they recolour live instead of only after a dialog reopen.
        self._about_btn.setIcon(icon("info"))
        self._about_btn.setStyleSheet(self._title_about_qss())
        self._title_close_btn.setStyleSheet(self._title_close_qss())
        # Rebuild the page in view now; the rest rebuild lazily on
        # their next visit, which keeps the switch cheap.
        self._on_nav_changed(current)

    def _build_downloads(self) -> QWidget:
        """The Downloads page — the offline-downloads management screen.
        Lazy import keeps the offline package (and its SQLite handle)
        off the settings dialog's construction path until this page is
        actually built."""
        from jellytoast.downloads_view import DownloadsView

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

        title = QLabel(self.tr("Settings"))
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        self._title_label = title
        h.addStretch(1)

        # About button (info circle) — opens a small overlay with the
        # name + version + blurb. Replaces the prior About settings page.
        about_btn = IconButton()
        from jellytoast.platform_compat import is_mobile_shell

        # Keyboard-reachable — but not on a phone, where nothing tabs and the
        # dialog's first focusable widget opened with a focus ring on it.
        about_btn.setFocusPolicy(
            Qt.FocusPolicy.NoFocus if is_mobile_shell() else Qt.FocusPolicy.TabFocus
        )
        about_btn.setIcon(icon("info"))
        about_btn.setIconSize(QSize(18, 18))
        about_btn.setFixedSize(32, 28)
        about_btn.setToolTip(self.tr("About jellytoast"))
        about_btn.setStyleSheet(self._title_about_qss())
        about_btn.clicked.connect(self._show_about)
        h.addWidget(about_btn)
        self._about_btn = about_btn

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(self._title_close_qss())
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)
        self._title_close_btn = close_btn

        # Make titlebar draggable via KWin (matches the main window).
        tb.mousePressEvent = self._titlebar_press
        return tb

    @staticmethod
    def _title_about_qss() -> str:
        """QSS for the titlebar info (ⓘ) button. ink_alpha() reads the
        live theme, so re-calling this after a theme switch yields the
        correct hover wash."""
        from jellytoast.ui_helpers import ACCENT

        return f"""
            QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: {rad(6)}px; }}
            QPushButton:hover {{ background: {ink_alpha(0.08)}; border-radius: {rad(6)}px; }}
            QPushButton:focus {{ background: {ink_alpha(0.08)}; border: 1px solid {ACCENT}; border-radius: {rad(6)}px; outline: none; }}
        """

    @staticmethod
    def _title_close_qss() -> str:
        """QSS for the titlebar close (✕) button. Bakes TEXT_DIM / TEXT /
        WASH_HOVER, so it must be re-stamped on a light↔dark switch."""
        return f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; border-radius: {rad(6)}px; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; color: {TEXT}; }}
        """

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

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
        "disconnected": (
            "rgba(220, 38, 38, 0.85)",
            "Disconnected — server unreachable. Check URL or network.",
        ),
        "signed_out": ("rgba(102, 102, 102, 0.55)", "Not signed in."),
    }

    # ── Crossfade helpers ───────────────────────────────────────────────────

    # ── Slider helpers ──────────────────────────────────────────────────────

    # Glass-opacity slider range — spans the airy jellytoast light default (140)
    # up to near-opaque. Wider than the preset-only band so every family's
    # default is reachable/displayable.
    _GLASS_MIN = 120
    _GLASS_MAX = 245
    # Fixed width for the family dropdown (stable right edge). The Mode combo and
    # the glass slider match it so their right edges line up, and Import + the
    # glass "Default" share one button width so those right-align too.
    _THEME_COMBO_W = 250

    # ── Hotkey helpers ──────────────────────────────────────────────────────

    # ── Scrobbling helpers ─────────────────────────────────────────────

    # ── About overlay (replaces the old About page) ────────────────────
    def _show_about(self):
        """Modal info dialog launched from the titlebar info button.
        Lighter than a full settings page since the content is just
        version + blurb — most surveyed music players (Strawberry,
        Supersonic, Feishin, Plexamp) put About in a small dialog
        rather than a settings tab.

        Renders as a borderless frosted pill matching the tooltip /
        volume-popup lift tone (see ``popup_paint_qcolor``). On KDE
        Wayland a noborder KWin rule (jellytoast.keep_above) strips the
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

    def _info_button(self, title: str, text: str, tooltip: str | None = None) -> QToolButton:
        """A small ⓘ button that holds a setting's descriptive text instead of
        an inline caption — keeps dense sections compact. Hover shows the text
        (tooltip); click opens it in the app-styled frosted dialog (so it works
        even when hover-tooltips are off). ``tooltip`` overrides the hover text
        when ``text`` is too long to read as a tooltip (e.g. multi-line help)."""
        from jellytoast.icons import icon

        btn = IconButton()
        # Keyboard-reachable (IconButton is NoFocus by default) so the help
        # text isn't mouse-only; Space/Enter opens it.
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        btn.setIcon(icon("info", size=15))
        btn.setIconSize(QSize(15, 15))
        btn.setFixedSize(20, 20)
        btn.setToolTip(text if tooltip is None else tooltip)
        btn.setAccessibleName(self.tr("About {0}").format(title))
        from jellytoast.ui_helpers import ACCENT

        btn.setStyleSheet(
            "QPushButton { border: 1px solid transparent; background: transparent; padding: 0; border-radius: {rad(5)}px; }"
            f"QPushButton:focus {{ border: 1px solid {ACCENT}; outline: none; }}"
        )

        def _show():
            from jellytoast.frosted_dialog import frosted_info

            frosted_info(self, title, text)

        btn.clicked.connect(_show)
        return btn

    def _build_and_apply_dialog_stylesheet(self):
        """Compose the dialog-level QSS that styles every QComboBox
        and QPushButton#ghost inside the dialog. Pulled into its own
        method so we can rebuild it from `_on_accent_picked` to
        live-apply a new accent — re-reads `ACCENT` from
        `jellytoast.ui_helpers` at call time, so the refresh_theme()
        path that ran just before this sees its new value."""
        from jellytoast.icons import icon_svg_path
        from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW
        from jellytoast.ui_helpers import check_url_for_accent

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
                border-radius: {rad(6)}px;
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
            /* Base text colour for the dialog's plain QPushButtons — e.g.
               "Customize" and the PipeWire conf installer. The dialog sets
               its own stylesheet, and Qt does NOT reliably merge the
               app-global base button-colour rule into a widget that already
               has one, so these kept stale (near-white) text after switching
               to a light theme. Rebuilt on every theme_changed, so it flips
               with the theme; the object-name rules below (#ghost,
               #jtSelector) and per-button stylesheets are more specific and
               still win. */
            QPushButton {{
                color: {TEXT};
                padding: 6px 14px;
                {type_qss(TYPE_BODY)}
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
                border-radius: {rad(6)}px;
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
                border-radius: {rad(8)}px;
                padding: 4px 0px;
                outline: 0;
                selection-background-color: rgba({_ar},{_ag},{_ab},0.85);
                selection-color: white;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 7px 14px;
                min-height: 22px;
                border-radius: {rad(4)}px;
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
                border-radius: {rad(6)}px;
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
                border-radius: {rad(6)}px;
                padding: 6px 12px;
                min-height: 22px;
            }}
            QLineEdit:focus, QKeySequenceEdit:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QCheckBox {{
                color: {TEXT}; spacing: 8px; background: transparent;
                {type_qss(TYPE_BODY)}
            }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {BORDER}; border-radius: {rad(3)}px;
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
                {type_qss(TYPE_BODY)}
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
                border-radius: {rad(4)}px; margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({_ar},{_ag},{_ab},0.4);
                border-radius: {rad(4)}px; min-height: 24px;
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
        # Keep the accent preset ring + eyedropper swatch in sync with the live
        # accent, so a "Follow system accent" update (which lands from outside the
        # dialog) shows the current system colour in the eyedropper. No-op when
        # the accent row isn't built.
        self._refresh_accent_swatches(self.s.accent_color)
        # Restart notice fill / left bar use the active accent. Rebuild
        # its inline stylesheet so its colour matches the new accent.
        # hasattr-guarded — the Display page (which owns the notice) is
        # built lazily, so this slot can fire before it exists (e.g. a
        # Colors-page slider drag while Display was never opened).
        if hasattr(self, "_theme_restart_notice"):
            from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW

            _ar2, _ag2, _ab2 = _hex_to_rgb(_ACCENT_NOW)
            self._theme_restart_notice.setStyleSheet(
                f"color: {TEXT}; "
                f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
                f"border-radius: {rad(6)}px; "
                f"padding: 10px 14px; "
                f"{type_qss(TYPE_CAPTION)}"
            )
        # Same for the General page's diagnostics acknowledgement — it bakes
        # TEXT_FAINT, which the refresh above just re-stamped.
        if hasattr(self, "_diagnostics_note"):
            self._diagnostics_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        # Scrobbling page's "your server is scrobbling for you" banner is
        # accent-tinted in its confirmatory state — re-stamp so it follows
        # a live accent swap. Guarded inside the helper (no-op before the
        # Scrobbling page is built).
        self._restamp_scrobble_banner()
        # _Selector builds its menu fresh on every open via opaque_menu(),
        # which re-reads the current accent each call — so a theme
        # change is picked up automatically. No cache to reset.
        # EQ slider handles bake ACCENT into their per-slider QSS at
        # construction time — re-stamp so the dot changes colour
        # live with the accent. No-op if the EQ section hasn't been
        # built yet (e.g. the user hasn't visited the Playback page).
        if hasattr(self, "_eq_page"):
            self._eq_page.reapply_accent()
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
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

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
            from jellytoast import ui_helpers as _uih

            p.setBrush(QColor(*_uih.body_color_tuple("dialog")))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
