"""
Native top navigation bar — back / forward / home / search / drawer +
section title. Drives the host's native nav signals; everything is
PySide6 widgets sharing the host window's translucent body color so
the header zone doesn't fight us on transparency.
"""

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QWidget

from modules.design_tokens import TYPE_SUBHEAD, type_qss
from modules.icon_button import IconButton
from modules.icons import icon
from modules.kde_titlebar import handle_titlebar_double_click
from modules.platform_compat import IS_WINDOWS
from modules.player_state import PlayerBus
from modules.ui_helpers import ink_alpha, opaque_menu

# Library tab label sets — keyed by collection type. The labels are
# kept compatible with Jellyfin's collection taxonomy so they map 1:1
# to the matching native surface in _on_tab_requested. Music is the
# only collection jellytoast actively renders today; the other entries
# stay here as a forward-compatible reference for future expansion.
_LIBRARY_TABS = {
    "music": [
        "Albums",
        "Suggestions",
        "Artists",
        "Playlists",
        "Smart playlists",
        "Songs",
        "Genres",
        "Radio",
        "Downloads",
    ],
    "movies": ["Movies", "Suggestions", "Trailers", "Favorites", "Collections", "Genres"],
    "tvshows": ["Shows", "Suggestions", "Latest", "Upcoming", "Genres", "Networks", "Episodes"],
    "books": ["Books", "Suggestions", "Genres"],
    "homevideos": ["Videos", "Photos", "Albums"],
    "music_videos": ["Music videos"],
}


# (label, Jellyfin SortBy parameter — comma chains add a deterministic
# tiebreaker so albums with the same primary value stay in a stable
# alphabetical order).
LIBRARY_SORT_OPTIONS = [
    ("Name", "SortName"),
    ("Album artist", "AlbumArtist,SortName"),
    ("Release date", "PremiereDate,SortName"),
    ("Date added", "DateCreated,SortName"),
    ("Recently played", "DatePlayed,SortName"),
]


class _StayOpenMenu(QMenu):
    """A ``QMenu`` that does NOT dismiss when a *checkable* action is
    activated by a mouse click, so the user can flip several rows in one
    visit (the multi-library picker). A click on a non-checkable action or
    on empty space / outside the menu closes it as usual.

    A plain ``QMenu`` hides itself in ``mouseReleaseEvent`` on ANY
    mouse-activated action, checkable or not — so without this the library
    picker slammed shut on the first toggle and every change cost a reopen.
    """

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt override name)
        act = self.activeAction()
        if act is not None and act.isEnabled() and act.isCheckable():
            # Toggle the check + fire ``triggered``, but keep the menu open
            # (skip the base impl, which would hide()).
            act.trigger()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt override name)
        # Keyboard analogue of ``mouseReleaseEvent``: Return/Enter/Space on a
        # checkable active row toggles it WITHOUT closing the menu, so a user
        # navigating by keyboard (the menu holds the keyboard grab) can flip
        # several libraries in one visit. The base QMenu would trigger AND
        # hide(). Arrow-key navigation, Esc, etc. fall through to the base.
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            act = self.activeAction()
            if act is not None and act.isEnabled() and act.isCheckable():
                act.trigger()
                event.accept()
                return
        super().keyPressEvent(event)


class JtTopBar(QWidget):
    nav_requested = Signal(str)  # "back" | "forward" | "home" | "search" | "preferences"
    settings_requested = Signal()
    cast_requested = Signal()
    tab_requested = Signal(int, str)  # (tab index in collection list, label)
    # Library controls cluster — visible only when the host swaps in a
    # native library grid (set_library_controls_visible(True)).
    shuffle_all_requested = Signal()
    view_mode_changed = Signal(str)  # "grid" | "list"
    sort_changed = Signal(str, str)  # (Jellyfin SortBy key, "ascending" | "descending")
    # Multi-library selection — the user toggled which Navidrome/Jellyfin
    # music libraries are loaded via the "Music" title dropdown. Payload is
    # the full list of selected library-id strings (empty = all). The host
    # persists + normalizes it (the widget stays a dumb view).
    libraries_selected = Signal(list)

    def __init__(self, parent=None, titlebar_mode: bool = False):
        super().__init__(parent)
        # When True this bar doubles as the borderless main window's
        # titlebar: drag-to-move, double-click-maximize, and a
        # min / max / close cluster on the right.
        self._titlebar_mode = titlebar_mode
        self.setFixedHeight(48)
        self.setObjectName("jtTopBar")
        # Transparent so the host window's painted body shows through.
        self.setStyleSheet("""
            QWidget#jtTopBar { background: transparent; }
            QWidget#jtTopBar > QWidget { background: transparent; }
            QWidget#jtTopBar QLabel { background: transparent; }
        """)
        # Collected by _icon_btn() at construction so _apply_styling
        # can re-stamp them all on theme_changed.
        self._icon_buttons: list[QPushButton] = []

        # 3-column layout — left, center, right each carry stretch=1
        # so the center column lands at the bar's geometric center
        # regardless of how wide the side columns' content grows. This
        # is what keeps the View dropdown + library controls cluster
        # truly centered, instead of being offset by asymmetric side
        # content (long titles on the left, single search button on
        # the right).
        layout = QHBoxLayout(self)
        # Vertical margins kept tight (4px) so the 40x40 search button
        # fits inside the 48px bar without clipping its hover
        # background at the bottom edge (48 - 8 = 40 = button height).
        # Standard 36px icon buttons still get 6px breathing room
        # above + below from layout alignment.
        layout.setContentsMargins(14, 4, 14, 4)
        layout.setSpacing(0)

        # ── Left column ─────────────────────────────────────────────
        left_col = QWidget()
        left_col.setStyleSheet("background: transparent;")
        left_layout = QHBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        self.back_btn = self._icon_btn("back", "Back")
        self.fwd_btn = self._icon_btn("forward", "Forward")
        self.home_btn = self._icon_btn("home", "Home")
        self.settings_btn = self._icon_btn("settings", "Settings")
        self.back_btn.clicked.connect(lambda: self.nav_requested.emit("back"))
        self.fwd_btn.clicked.connect(lambda: self.nav_requested.emit("forward"))
        self.home_btn.clicked.connect(lambda: self.nav_requested.emit("home"))
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        for b in (self.back_btn, self.fwd_btn, self.home_btn, self.settings_btn):
            left_layout.addWidget(b)

        # Subtle divider between nav cluster and title. Tracked as
        # self._separator so _apply_styling can re-stamp it live.
        self._separator = QFrame()
        self._separator.setFixedSize(1, 18)
        self._separator.setStyleSheet(self._separator_qss())
        left_layout.addSpacing(10)
        left_layout.addWidget(self._separator)
        left_layout.addSpacing(14)

        # Section title. A plain QLabel until the active server exposes
        # 2+ music libraries — then ``set_available_libraries`` swaps it
        # for a clickable dropdown button (chevron + multi-select menu)
        # so the user can choose which libraries are loaded. Kept as a
        # QLabel in the single-library common case so there's no chevron /
        # hover affordance where it'd be a dead control.
        self.title_label = QLabel("")
        self.title_label.setStyleSheet(self._title_label_qss())
        left_layout.addWidget(self.title_label)
        # Dropdown variant — hidden until 2+ libraries are available. A
        # borderless text+chevron button mirroring the view_btn styling.
        self.library_btn = QPushButton("")
        self.library_btn.setIcon(icon("chevron_down"))
        self.library_btn.setIconSize(QSize(13, 13))
        self.library_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.library_btn.setToolTip("Choose which libraries to load")
        self.library_btn.setFixedHeight(34)
        self.library_btn.setStyleSheet(self._view_btn_qss())
        self.library_btn.clicked.connect(self._show_library_menu)
        self._install_enter_to_click(self.library_btn)
        self.library_btn.hide()
        left_layout.addWidget(self.library_btn)
        # Available music libraries [{Id, Name}] + the active selection,
        # pushed by the host. Empty selection == all libraries.
        self._available_libraries: list[dict] = []
        self._selected_library_ids: list[str] = []
        # Live refs to the open multi-library menu so set_selected_libraries
        # can re-sync its checkmarks after the host normalizes a selection
        # (e.g. collapsing 'every library' → 'all'). None when no menu open.
        self._lib_menu: QMenu | None = None
        self._lib_actions: dict[str, QAction] = {}
        self._all_action: QAction | None = None
        # Trailing stretch keeps the left column's content anchored to
        # its left edge while the column itself fills 1/3 of the bar.
        left_layout.addStretch(1)

        # ── Center column ──────────────────────────────────────────
        center_col = QWidget()
        center_col.setStyleSheet("background: transparent;")
        center_layout = QHBoxLayout(center_col)
        center_layout.setContentsMargins(0, 0, 0, 0)
        # Spacing 0 — the only visual gap (dropdown↔controls) is owned by the
        # controls' own left margin, so the balance spacer below can keep the
        # dropdown exactly centered without stray spacing throwing it off.
        center_layout.setSpacing(0)
        # PIN the view dropdown to the bar's geometric centre. The center
        # column is the bar's middle third (1/3..2/3), so a leading + trailing
        # stretch centre their content at the bar's 1/2 mark. The balance
        # spacer mirrors the library-controls width on the LEFT, so the
        # dropdown stays dead-centre whether or not the controls are showing,
        # and only breathes symmetrically (centre fixed) as the page-name width
        # changes — no left/right bounce. See set_library_controls_visible.
        center_layout.addStretch(1)
        self._center_balance = QWidget()
        self._center_balance.setFixedWidth(0)
        self._center_balance.setStyleSheet("background: transparent;")
        center_layout.addWidget(self._center_balance)

        # Library tab dropdown — borderless text + chevron. The label
        # tracks the currently active tab (e.g. "Albums"); clicking
        # opens a menu of all tabs for the current collection.
        # Visible only on library pages.
        self.view_btn = QPushButton("Albums")
        self.view_btn.setIcon(icon("chevron_down"))
        self.view_btn.setIconSize(QSize(14, 14))
        self.view_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.view_btn.setToolTip("Switch library view")
        # Match the 34px icon-button height so every hover pill in the
        # bar reads as the same rectangle — without this, the view_btn
        # sizes to its natural text-padding height (~28 px) and the
        # highlight pill comes out visibly shorter than the shuffle /
        # view-toggle / sort buttons next to it.
        self.view_btn.setFixedHeight(34)
        self.view_btn.setStyleSheet(self._view_btn_qss())
        self.view_btn.clicked.connect(self._show_view_menu)
        self._install_enter_to_click(self.view_btn)
        self.view_btn.hide()  # shown only when collection is set
        self._view_collection = ""
        # When True, view_btn always reads "Now Playing" and the host's
        # set_active_tab calls are ignored so the np-page label sticks.
        self._now_playing_mode = False
        center_layout.addWidget(self.view_btn)

        # Library controls cluster — Shuffle all + View toggle (grid/
        # list) + Sort dropdown + Sort-order toggle. Hidden by default;
        # the host shows it via set_library_controls_visible(True) when
        # a native library grid is the active content surface. Sits
        # immediately to the right of the View dropdown so the two
        # read as one centered cluster.
        self._library_ctrls = QWidget()
        self._library_ctrls.setStyleSheet("background: transparent;")
        lc = QHBoxLayout(self._library_ctrls)
        # 8px left margin = the visual gap between the dropdown and the
        # controls (center_layout spacing is 0). The balance spacer mirrors
        # this whole width, gap included, to keep the dropdown centered.
        lc.setContentsMargins(8, 0, 0, 0)
        lc.setSpacing(2)

        self.shuffle_all_btn = self._icon_btn("shuffle", "Shuffle all")
        self.shuffle_all_btn.clicked.connect(self.shuffle_all_requested.emit)
        lc.addWidget(self.shuffle_all_btn)

        # View toggle — uses the grid/list icons from the registry. The
        # visible glyph reflects the *current* mode (Apple Music
        # convention) rather than the next one, so the user can read
        # "I'm in grid view" at a glance. Restored from Settings so
        # the user's last choice survives across launches; LibraryGrid
        # reads the same setting at construction so the initial paint
        # already matches.
        from modules.settings import get_settings as _gs_view

        self._view_mode = _gs_view().library_view_mode
        self.view_mode_btn = self._icon_btn(
            self._view_mode,
            "Toggle grid / list",
        )
        self.view_mode_btn.clicked.connect(self._on_view_toggle)
        lc.addWidget(self.view_mode_btn)

        # Sort — single icon button. Click opens a menu with both the
        # sort criterion (Name / Album artist / …) AND the order
        # (Ascending / Descending) so the cluster stays compact.
        # Initial state restored from Settings so the user's preferred
        # sort sticks across launches.
        from modules.settings import get_settings

        s = get_settings()
        saved_key = s.library_sort_by
        self._current_sort = next(
            (opt for opt in LIBRARY_SORT_OPTIONS if opt[1] == saved_key),
            LIBRARY_SORT_OPTIONS[0],
        )
        self._sort_order = s.library_sort_order
        self.sort_btn = self._icon_btn("sort", "")
        self.sort_btn.clicked.connect(self._show_sort_menu)
        self._install_enter_to_click(self.sort_btn)
        lc.addWidget(self.sort_btn)
        self._refresh_sort_btn_tooltip()

        self._library_ctrls.hide()
        center_layout.addWidget(self._library_ctrls)
        center_layout.addStretch(1)

        # ── Right column ───────────────────────────────────────────
        right_col = QWidget()
        right_col.setStyleSheet("background: transparent;")
        right_layout = QHBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        # Leading stretch anchors the search button to the right edge
        # of the column.
        right_layout.addStretch(1)

        # Offline-mode chip — small status pill nested in the right
        # column to the left of the search button. Hidden unless the
        # user is in offline mode or the server is unreachable. The
        # chip subscribes to PlayerBus directly so the top bar stays
        # the owner of its own status surface.
        from modules.offline_banner import OfflineChip

        self.offline_chip = OfflineChip(self)
        right_layout.addWidget(self.offline_chip)
        right_layout.addSpacing(6)

        # Search uses the same 34×34 footprint as the other icon
        # buttons so every hover pill across the bar reads as the same
        # rectangle. The glyph stays at 20 px (slightly larger than the
        # 18 px standard icon-button glyph) so the search button still
        # carries a touch more visual weight without making its
        # highlight pill stand taller than its neighbours.
        self.search_btn = IconButton()
        self.search_btn.setIcon(icon("search"))
        self.search_btn.setIconSize(QSize(20, 20))
        self.search_btn.setFixedSize(34, 34)
        self.search_btn.setToolTip("Search")
        self.search_btn.setStyleSheet(self._search_btn_qss())
        self.search_btn.clicked.connect(lambda: self.nav_requested.emit("search"))
        right_layout.addWidget(self.search_btn)

        # Window controls — present only when the bar is the borderless
        # window's titlebar (KWin draws none of its own then). min / max
        # / close all reuse the standard icon button — one neutral hover
        # wash, no destructive-red special case on close.
        if titlebar_mode:
            right_layout.addSpacing(8)
            self.min_btn = self._icon_btn("win_minimize", "Minimize")
            self.min_btn.clicked.connect(lambda: self.window().showMinimized())
            self.max_btn = self._icon_btn("win_maximize", "Maximize")
            self.max_btn.clicked.connect(self._toggle_max)
            self.close_btn = self._icon_btn("win_close", "Close")
            self.close_btn.clicked.connect(lambda: self.window().close())
            for b in (self.min_btn, self.max_btn, self.close_btn):
                right_layout.addWidget(b)

        # Equal stretch on each column = the center column is at the
        # bar's geometric center regardless of side content.
        layout.addWidget(left_col, 1)
        layout.addWidget(center_col, 1)
        layout.addWidget(right_col, 1)

        # Live-apply: re-stamp every theme-dependent stylesheet
        # whenever the color editor (or accent picker) fires
        # theme_changed.
        try:
            PlayerBus.get().theme_changed.connect(self._apply_styling)
        except Exception:
            pass

    def set_library_controls_visible(self, visible: bool):
        """Show/hide the Shuffle + View toggle + Sort cluster. The host
        flips this to True when a native library grid is the active
        content surface and False on curated surfaces (Suggestions,
        Search, NowPlayingPage) where sort/view-toggle don't apply."""
        self._library_ctrls.setVisible(visible)
        # Mirror the controls' width on the left so the view dropdown stays
        # centered in the bar regardless of whether they're shown.
        self._center_balance.setFixedWidth(
            self._library_ctrls.sizeHint().width() if visible else 0
        )

    def set_back_enabled(self, enabled: bool):
        """Toggle the back arrow's enabled state — host calls this
        whenever the navigation history's position changes so the
        button visually reflects whether there's anywhere to go back
        to. Disabled buttons render with reduced opacity via Qt's
        default style so the user doesn't waste clicks at the stack
        edge."""
        self.back_btn.setEnabled(enabled)

    def set_forward_enabled(self, enabled: bool):
        self.fwd_btn.setEnabled(enabled)

    # ── Titlebar mode (borderless window) ───────────────────────────

    def _toggle_max(self):
        w = self.window()
        if w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()

    def mousePressEvent(self, e):
        # In titlebar mode a press on the bar's own area starts a
        # compositor-driven window move. Interactive children (buttons,
        # the view dropdown) consume their own presses, so anything
        # that reaches here is empty bar / label / column space.
        if self._titlebar_mode and e.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        # Mirror KDE's TitlebarDoubleClickCommand setting (kwinrc):
        # because our window is borderless KWin never sees the click, so
        # we read the setting and invoke the matching KWin action via
        # D-Bus. On Windows the frameless window has no compositor command,
        # so double-click does the standard full-maximize toggle. Falls
        # back to a Qt vertical-max toggle off KDE.
        if self._titlebar_mode and e.button() == Qt.MouseButton.LeftButton:
            if IS_WINDOWS:
                self._toggle_max()
            else:
                handle_titlebar_double_click(self.window())
            return
        super().mouseDoubleClickEvent(e)

    def _on_view_toggle(self):
        self._view_mode = "list" if self._view_mode == "grid" else "grid"
        # The visible icon reflects the *current* mode (Apple Music
        # convention), not what clicking will switch to.
        self.view_mode_btn.setIcon(icon(self._view_mode))
        self.view_mode_changed.emit(self._view_mode)

    @staticmethod
    def _dropdown_menu_qss(*, checkable: bool = False) -> str:
        """ONE QSS source for all three top-bar dropdowns (view / sort /
        library) so they can't visually drift (the library picker had).
        Accent-tinted hover/selection matches ``ui_helpers.GLOBAL_STYLE``.

        ``checkable`` menus (sort / library) reserve a native check column on
        the left, so they get EXTRA left padding to push the ✓ off the rounded
        corner it was hugging; plain text menus (view) keep the tighter indent
        they already read well at. The slightly larger menu padding gives the
        first/last rows clearance from the rounded top/bottom corners too.

        Background uses ``popup_body_fill()`` (NOT the raw `POPUP_OPAQUE_FILL`)
        so these dropdowns FROST over verified compositor blur like every other
        `opaque_menu` popup: when blur is active the fill drops to a capped
        frosted alpha and the blur lifts through; otherwise it stays the opaque
        token. The raw token made the light family read stark white — dark's
        token is already translucent (0.65) so it "accidentally" frosted, while
        light's (0.80, near-white) painted opaque. Read live off the module
        (NOT an import-time binding) so dark↔light + accent + blur-status changes
        apply on the next open without rebuilding the bar."""
        from modules import ui_helpers as _u
        from modules.theme import _hex_to_rgb, get_active_theme

        _ar, _ag, _ab = _hex_to_rgb(get_active_theme().accent)
        left = 22 if checkable else 14
        return f"""
            QMenu {{
                background: {_u.popup_body_fill()};
                color: {_u.TEXT};
                border: none;
                border-radius: 8px;
                padding: 6px;
            }}
            QMenu::item {{
                padding: 7px 26px 7px {left}px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{ background: rgba({_ar},{_ag},{_ab},0.2); }}
            QMenu::separator {{
                height: 1px;
                background: {ink_alpha(0.08)};
                margin: 4px 8px;
            }}
        """

    def _popup_menu_centered(self, menu: QMenu, button: QWidget) -> QPoint:
        """Top-left point that drops ``menu`` directly below ``button`` and
        horizontally CENTRED on it — the balanced look the view dropdown uses
        (the button's chevron sits over the middle of the menu). Widens the
        menu to at least the button so a short menu isn't narrower than the
        control that opened it."""
        menu.ensurePolished()
        menu_w = max(menu.sizeHint().width(), button.width())
        menu.setMinimumWidth(menu_w)
        btn_top_left = button.mapToGlobal(QPoint(0, 0))
        return QPoint(
            btn_top_left.x() + button.width() // 2 - menu_w // 2,
            btn_top_left.y() + button.height(),
        )

    def _show_sort_menu(self):
        menu = opaque_menu(self, blur_corner_radius=8)
        # Built fresh per-show so live-accent / dark↔light changes apply
        # without rebuilding the top bar (the shared QSS reads them live).
        # Checkable rows → extra left room for the native check column.
        menu.setStyleSheet(self._dropdown_menu_qss(checkable=True))
        # Section 1: sort criterion. Checkable so Qt renders a native
        # check beside the active option.
        active_action = None
        for label, key in LIBRARY_SORT_OPTIONS:
            act = QAction(label, menu)
            act.setCheckable(True)
            is_current = self._current_sort == (label, key)
            act.setChecked(is_current)
            if is_current:
                active_action = act
            act.triggered.connect(
                lambda _checked=False, lbl=label, k=key: self._on_sort_picked(lbl, k)
            )
            menu.addAction(act)
        menu.addSeparator()
        # Section 2: sort order. Same menu, two more checkable items.
        for order_label, order_key in (("Ascending", "ascending"), ("Descending", "descending")):
            act = QAction(order_label, menu)
            act.setCheckable(True)
            act.setChecked(self._sort_order == order_key)
            act.triggered.connect(lambda _checked=False, o=order_key: self._on_sort_order_picked(o))
            menu.addAction(act)
        # Pre-highlight the active sort criterion so keyboard arrow
        # navigation starts from the current selection rather than
        # from no active row.
        if active_action is not None:
            menu.setActiveAction(active_action)
        pt = self.sort_btn.mapToGlobal(self.sort_btn.rect().bottomLeft())
        # Park focus on the button so the library grid behind us loses
        # focus (and its _keyboard_mode resets) — otherwise on KDE
        # Wayland arrow keys leak through to the grid even with the
        # menu visible.
        self.sort_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        self._exec_menu_with_kbd_grab(menu, pt)

    def _on_sort_picked(self, label: str, key: str):
        self._current_sort = (label, key)
        from modules.settings import get_settings

        get_settings().library_sort_by = key
        self._refresh_sort_btn_tooltip()
        self.sort_changed.emit(key, self._sort_order)

    def _on_sort_order_picked(self, order: str):
        self._sort_order = order
        from modules.settings import get_settings

        get_settings().library_sort_order = order
        self._refresh_sort_btn_tooltip()
        self.sort_changed.emit(self._current_sort[1], self._sort_order)

    def _refresh_sort_btn_tooltip(self):
        # Reflects current state in the tooltip so hovering the icon
        # button surfaces what's selected (the menu is the canonical
        # place to see + change it, but a tooltip is faster to glance).
        order_label = self._sort_order.capitalize()
        self.sort_btn.setToolTip(f"Sort: {self._current_sort[0]} ({order_label})")

    def _exec_menu_with_kbd_grab(self, menu: QMenu, pos) -> None:
        """Show ``menu`` at ``pos`` with a hard keyboard grab so arrow
        keys can't leak to widgets behind it. On KDE Wayland, QMenu's
        popup focus is unreliable: Down arrow alternates between the
        menu and whatever QAbstractItemView lives underneath. An
        explicit grabKeyboard (the same trick QComboBox uses for its
        dropdown) makes the menu the exclusive recipient of key events
        for as long as it's visible."""
        from PySide6.QtCore import QTimer

        # grabKeyboard requires the widget to be visible. exec() shows
        # the menu before it pumps events, so a 0-delay singleShot fires
        # on the very next tick — after show, before the user can press
        # a key.
        QTimer.singleShot(0, menu.grabKeyboard)
        try:
            menu.exec(pos)
        finally:
            menu.releaseKeyboard()

    def _install_enter_to_click(self, btn: QPushButton) -> None:
        """Make Return/Enter on a focused button trigger click() the same
        as Space. Qt's QAbstractButton.keyPressEvent only binds Space;
        Return is reserved for the dialog default-button mechanism, so
        toolbar-style buttons outside a QDialog need an explicit binding
        or keyboard nav can't drop their menu."""
        orig = btn.keyPressEvent

        def _kpe(e):
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                btn.click()
                e.accept()
                return
            orig(e)

        btn.keyPressEvent = _kpe

    def _icon_btn(self, name: str, tooltip: str) -> IconButton:
        b = IconButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(34, 34)
        b.setToolTip(tooltip)
        b.setStyleSheet(self._icon_btn_qss())
        # Stash the glyph name + track the button so _apply_styling
        # can re-issue the icon in the new tint on theme_changed.
        b.setProperty("_jt_icon", name)
        self._icon_buttons.append(b)
        return b

    @staticmethod
    def _icon_btn_qss() -> str:
        """Built per-call so a theme_changed re-stamp reads the
        current WASH_HOVER / WASH_PRESSED values."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: {_u.WASH_HOVER};
            }}
            QPushButton:pressed {{
                background: {_u.WASH_PRESSED};
            }}
        """

    @staticmethod
    def _view_btn_qss() -> str:
        """View-dropdown button QSS. Hover / pressed use the shared
        WASH tokens so the highlight matches the icon buttons — a
        lighter pill, not the darker SELECTED_ROW wash."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                color: {_u.TEXT};
                border: none;
                border-radius: 8px;
                padding: 4px 8px;
                {type_qss(TYPE_SUBHEAD)}
                text-align: left;
            }}
            QPushButton:hover {{ background: {_u.WASH_HOVER}; }}
            QPushButton:pressed {{ background: {_u.WASH_PRESSED}; }}
        """

    @staticmethod
    def _search_btn_qss() -> str:
        """Search-button QSS — identical to `_icon_btn_qss` so the
        hover pill matches every other button across the bar. Kept as
        its own method (rather than re-using `_icon_btn_qss`) so any
        future search-specific divergence has a clear home."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{ background: {_u.WASH_HOVER}; }}
            QPushButton:pressed {{ background: {_u.WASH_PRESSED}; }}
        """

    @staticmethod
    def _title_label_qss() -> str:
        from modules import ui_helpers as _u

        return f"color: {_u.TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.2px;"

    @staticmethod
    def _separator_qss() -> str:
        """Vertical divider hairline. Reads BORDER live."""
        from modules import ui_helpers as _u

        return f"background: {_u.BORDER};"

    def _apply_styling(self):
        """Re-stamp every theme-dependent stylesheet in the bar. Called
        once at init AND on PlayerBus.theme_changed so the color editor
        flows through to top-bar surfaces live without a restart."""
        # Icon buttons — back / fwd / home / settings / shuffle / view
        # mode / sort. Re-stamp the pill QSS and re-issue the glyph in
        # the new tint (the QIcon is baked at construction).
        for b in self._icon_buttons:
            b.setStyleSheet(self._icon_btn_qss())
            name = b.property("_jt_icon")
            if name:
                b.setIcon(icon(name))
        # view_mode_btn's glyph is state-driven (list / grid) — re-issue
        # from the current mode rather than the construction-time tag.
        if hasattr(self, "view_mode_btn"):
            self.view_mode_btn.setIcon(icon(self._view_mode))
        # View dropdown + search button — bespoke QSS + their own icons.
        if hasattr(self, "view_btn"):
            self.view_btn.setStyleSheet(self._view_btn_qss())
            self.view_btn.setIcon(icon("chevron_down"))
        if hasattr(self, "library_btn"):
            self.library_btn.setStyleSheet(self._view_btn_qss())
            self.library_btn.setIcon(icon("chevron_down"))
        if hasattr(self, "search_btn"):
            self.search_btn.setStyleSheet(self._search_btn_qss())
            self.search_btn.setIcon(icon("search"))
        # (min / max / close are _icon_btn buttons — handled by the
        # _icon_buttons loop above.)
        # Title label color.
        if hasattr(self, "title_label"):
            self.title_label.setStyleSheet(self._title_label_qss())
        # Separator hairline.
        if hasattr(self, "_separator"):
            self._separator.setStyleSheet(self._separator_qss())

    def set_title(self, text: str):
        # Drive whichever title widget is active: the dropdown button when
        # the server has multiple libraries, the plain label otherwise.
        text = text or ""
        self.title_label.setText(text)
        self.library_btn.setText(text)

    def set_available_libraries(self, libs: list):
        """Host pushes the active server's music libraries ([{Id, Name}])
        after sign-in. With 2+, the title becomes a multi-select dropdown;
        with 0–1 it stays a plain label (no dead chevron). Idempotent —
        safe to call on every server change."""
        self._available_libraries = list(libs or [])
        multi = len(self._available_libraries) >= 2
        # Swap which title widget is visible; preserve the current text.
        current = self.title_label.text() or self.library_btn.text()
        self.title_label.setVisible(not multi)
        self.library_btn.setVisible(multi)
        if current:
            self.set_title(current)

    def set_selected_libraries(self, ids: list):
        """Host pushes the current (normalized) selection (list of library
        ids; empty = all) so the menu's checkmarks reflect persisted state.
        If the menu is open, re-sync its live checkmarks too — the host
        collapses 'every library selected' to '' (all), so without this the
        open menu's marks would drift from the title + persisted state after
        the collapse boundary is crossed."""
        self._selected_library_ids = [str(i) for i in (ids or [])]
        self._refresh_menu_checks()

    def _refresh_menu_checks(self):
        """Re-stamp the open library menu's checkmarks from the current
        selection. No-op when no menu is open."""
        menu = self._lib_menu
        if menu is None:
            return
        try:
            if not menu.isVisible():
                return
        except RuntimeError:  # underlying C++ menu already gone
            self._lib_menu = None
            return
        selected = set(self._selected_library_ids)
        if self._all_action is not None:
            self._all_action.setChecked(not selected)
        for lid, act in self._lib_actions.items():
            # All-mode (empty) → every library is effectively ON.
            act.setChecked((not selected) or lid in selected)

    def _show_library_menu(self):
        """Multi-select menu of the server's music libraries. Checkable
        rows in a ``_StayOpenMenu`` so the menu does NOT close on each
        toggle (the user can flip several in one visit), plus an 'All
        libraries' reset row. Emits ``libraries_selected`` with the full id
        list on each toggle; the host owns persistence + the reload, then
        pushes the normalized selection back via ``set_selected_libraries``
        which re-syncs these checkmarks live."""
        if len(self._available_libraries) < 2:
            return
        menu = opaque_menu(self, menu_cls=_StayOpenMenu, blur_corner_radius=8)
        menu.setStyleSheet(self._dropdown_menu_qss(checkable=True))
        selected = set(self._selected_library_ids)

        # "All libraries" reset row — checked when nothing specific is
        # selected. Picking it clears the selection (→ all).
        all_act = QAction("All libraries", menu)
        all_act.setCheckable(True)
        all_act.setChecked(not selected)
        all_act.triggered.connect(lambda _checked=False: self._on_library_toggled(None))
        menu.addAction(all_act)
        menu.addSeparator()

        self._lib_actions = {}
        for lib in self._available_libraries:
            lid = str(lib.get("Id") or "")
            name = str(lib.get("Name") or lid)
            act = QAction(name, menu)
            act.setCheckable(True)
            # Empty selection means 'all', so every library is effectively
            # ON — show it checked. Rendering them unchecked in the 'all'
            # state made unchecking one (to drop it) paradoxically re-add
            # it; see _on_library_toggled.
            act.setChecked((not selected) or lid in selected)
            # ``triggered`` fires after Qt flips the check state.
            act.triggered.connect(
                lambda _checked=False, i=lid: self._on_library_toggled(i)
            )
            menu.addAction(act)
            self._lib_actions[lid] = act
        self._all_action = all_act
        self._lib_menu = menu

        # Centre the menu under its button — same balanced placement as the
        # view dropdown (was bottom-left-anchored, which read off-centre next
        # to the centred view menu).
        pt = self._popup_menu_centered(menu, self.library_btn)
        self.library_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        # _StayOpenMenu keeps itself open across checkable toggles (a plain
        # QMenu would hide on the first click); it closes on click-away. Route
        # through the shared keyboard-grab helper like the sort / view menus so
        # arrow keys can't leak to the grid behind it on KDE Wayland.
        self._exec_menu_with_kbd_grab(menu, pt)

    def _on_library_toggled(self, lib_id):
        """A library row (or the 'All' row when ``lib_id is None``) was
        toggled. Recompute the selection list and emit it. The host
        normalizes (all-selected == none == 'all') and persists."""
        if lib_id is None:
            new_ids: list[str] = []  # 'All' → clear
        else:
            # Work from the EFFECTIVE selection: an empty list means 'all',
            # so expand it to every library before toggling. Without this,
            # unchecking a library while in the 'all' state (empty list)
            # would paradoxically ADD it (it's "not in" the empty list)
            # instead of leaving "all except that one" — the source of the
            # "had to toggle a few times to drop a library" confusion
            # (2026-06-02). The host re-collapses 'every library' → '' (all).
            sel = list(self._selected_library_ids)
            if not sel:
                sel = [str(lib.get("Id") or "") for lib in self._available_libraries]
            if lib_id in sel:
                sel.remove(lib_id)
            else:
                sel.append(lib_id)
            new_ids = sel
        self._selected_library_ids = new_ids
        self.libraries_selected.emit(list(new_ids))

    def set_collection(self, collection_type: str):
        """Show/hide the View dropdown based on what kind of library
        page we're on. `collection_type` matches Jellyfin's
        `collectionType` query param (music, movies, tvshows, …).
        Empty string hides the dropdown."""
        # Now-playing mode owns the dropdown label; don't let a
        # collection refresh stomp on it.
        if getattr(self, "_now_playing_mode", False):
            self._view_collection = (collection_type or "").lower()
            return
        self._view_collection = (collection_type or "").lower()
        tabs = _LIBRARY_TABS.get(self._view_collection, [])
        self.view_btn.setVisible(bool(tabs))
        # Default the label to the first tab whenever we land on a new
        # library — get refined later by set_active_tab once we've
        # polled the DOM for the actually-selected tab.
        if tabs and self.view_btn.text() not in tabs:
            self.view_btn.setText(tabs[0])

    def set_now_playing_mode(self, active: bool, label: str = "Now Playing"):
        """Repurpose the library-tab dropdown for the now-playing page.
        When active=True, the button reads ``label`` (typically
        "Now Playing" for live playback or "Browsing" for preview /
        browse mode) and clicking opens the same library-tab menu
        so the user can navigate away. When active=False, normal
        library behavior resumes via set_collection / set_active_tab.
        """
        self._now_playing_mode = active
        if active:
            self.view_btn.setText(label)
            self.view_btn.show()
        else:
            # Reapply the active library label if we're still on a
            # library collection; otherwise the host's next
            # set_collection / set_active_tab call will refresh it.
            tabs = _LIBRARY_TABS.get(self._view_collection, [])
            self.view_btn.setVisible(bool(tabs))
            # If the dropdown is still showing a now-playing-mode label
            # ("Now Playing" / "Browsing" / etc.), reset to a valid tab
            # so the user immediately sees which surface they're on.
            # The host's set_active_tab call (if any) will refine to
            # the actual active surface.
            if tabs and self.view_btn.text() not in tabs:
                self.view_btn.setText(tabs[0])

    def set_active_tab(self, label: str):
        """Update the dropdown label to reflect the currently-active
        library tab. Called by the host after surface swaps and by the
        dropdown itself after the user picks a tab."""
        if not label:
            return
        if self._now_playing_mode:
            return  # "Now Playing" label takes precedence
        tabs = _LIBRARY_TABS.get(self._view_collection, [])
        # Match case-insensitively against the canonical label so we
        # display our own casing rather than whatever the DOM returned.
        target = label.strip().lower()
        for canonical in tabs:
            if canonical.lower() == target:
                self.view_btn.setText(canonical)
                return
        # If the active tab isn't in our dict (collection we don't
        # know about yet), show what the DOM gave us verbatim.
        self.view_btn.setText(label.strip())

    def _show_view_menu(self):
        tabs = _LIBRARY_TABS.get(self._view_collection, [])
        if not tabs:
            return
        menu = opaque_menu(self, blur_corner_radius=8)
        menu.setStyleSheet(self._dropdown_menu_qss())
        current_label = self.view_btn.text().strip().lower()
        active_action = None
        for idx, label in enumerate(tabs):
            act = QAction(label, menu)
            if label.lower() == current_label:
                active_action = act
            act.triggered.connect(
                lambda _checked=False, i=idx, lbl=label: self.tab_requested.emit(i, lbl)
            )
            menu.addAction(act)
        # Pre-highlight the active tab so keyboard arrow navigation
        # starts from the current view rather than from no active row.
        if active_action is not None:
            menu.setActiveAction(active_action)
        # Pop below the button, horizontally centred on it — the chevron sits
        # over the middle of the dropdown, so the menu's centre under it reads
        # more balanced than a left-aligned drop. (Shared with the library
        # picker via _popup_menu_centered.)
        pt = self._popup_menu_centered(menu, self.view_btn)
        # Park focus on the button so the library grid behind us loses
        # focus (and its _keyboard_mode resets) — otherwise on KDE
        # Wayland arrow keys leak through to the grid even with the
        # menu visible.
        self.view_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        self._exec_menu_with_kbd_grab(menu, pt)
