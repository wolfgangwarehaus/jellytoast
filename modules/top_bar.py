"""
Native top navigation bar — back / forward / home / search / drawer +
section title. Drives the host's native nav signals; everything is
PySide6 widgets sharing the host window's translucent body color so
the header zone doesn't fight us on transparency.
"""

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QFrame, QMenu

from modules.icons import icon
from modules.ui_helpers import TEXT, BORDER, BG_PANEL, opaque_menu
from modules.design_tokens import TYPE_SUBHEAD, type_qss
from modules.player_state import PlayerBus


# Library tab label sets — keyed by collection type. The labels are
# kept compatible with Jellyfin's collection taxonomy so they map 1:1
# to the matching native surface in _on_tab_requested. Music is the
# only collection jellytoast actively renders today; the other entries
# stay here as a forward-compatible reference for future expansion.
_LIBRARY_TABS = {
    "music": ["Albums", "Suggestions", "Artists", "Playlists", "Songs", "Genres", "Downloads"],
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

    def __init__(self, parent=None):
        super().__init__(parent)
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

        self.title_label = QLabel("")
        self.title_label.setStyleSheet(self._title_label_qss())
        left_layout.addWidget(self.title_label)
        # Trailing stretch keeps the left column's content anchored to
        # its left edge while the column itself fills 1/3 of the bar.
        left_layout.addStretch(1)

        # ── Center column ──────────────────────────────────────────
        center_col = QWidget()
        center_col.setStyleSheet("background: transparent;")
        center_layout = QHBoxLayout(center_col)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        # Stretches on both sides center the cluster within the
        # column. Combined with the column itself being 1/3 of the
        # bar via the outer stretch=1, the cluster sits at the bar's
        # geometric center.
        center_layout.addStretch(1)

        # Library tab dropdown — borderless text + chevron. The label
        # tracks the currently active tab (e.g. "Albums"); clicking
        # opens a menu of all tabs for the current collection.
        # Visible only on library pages.
        self.view_btn = QPushButton("Albums")
        self.view_btn.setIcon(icon("chevron_down"))
        self.view_btn.setIconSize(QSize(14, 14))
        self.view_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.view_btn.setToolTip("Switch library view")
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
        lc.setContentsMargins(0, 0, 0, 0)
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

        # Search lives here, sized slightly larger than the standard
        # icon button so it reads as a primary action and pairs
        # comfortably with the X close in the titlebar above.
        self.search_btn = QPushButton()
        self.search_btn.setIcon(icon("search"))
        self.search_btn.setIconSize(QSize(22, 22))
        self.search_btn.setFixedSize(40, 40)
        self.search_btn.setToolTip("Search")
        self.search_btn.setStyleSheet(self._search_btn_qss())
        self.search_btn.clicked.connect(lambda: self.nav_requested.emit("search"))
        right_layout.addWidget(self.search_btn)

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

    def _on_view_toggle(self):
        self._view_mode = "list" if self._view_mode == "grid" else "grid"
        # The visible icon reflects the *current* mode (Apple Music
        # convention), not what clicking will switch to.
        self.view_mode_btn.setIcon(icon(self._view_mode))
        self.view_mode_changed.emit(self._view_mode)

    def _show_sort_menu(self):
        menu = opaque_menu(self)
        # Accent-tinted hover/selection — matches the global menu language
        # in ui_helpers.GLOBAL_STYLE rather than the previous flat-grey
        # override. Built fresh per-show so live-accent changes apply
        # without rebuilding the top bar.
        from modules.theme import get_active_theme, _hex_to_rgb

        _ar, _ag, _ab = _hex_to_rgb(get_active_theme().accent)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {BG_PANEL};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 7px 22px 7px 14px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{ background: rgba({_ar},{_ag},{_ab},0.2); }}
            QMenu::separator {{
                height: 1px;
                background: rgba(255,255,255,0.08);
                margin: 4px 8px;
            }}
        """)
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

    def _icon_btn(self, name: str, tooltip: str) -> QPushButton:
        b = QPushButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(34, 34)
        b.setToolTip(tooltip)
        b.setStyleSheet(self._icon_btn_qss())
        # Track for live-apply on theme_changed.
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
        """View-dropdown button QSS. Reads TEXT + SELECTED_ROW +
        PRESSED_WHITE live."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                color: {_u.TEXT};
                border: none;
                border-radius: 6px;
                padding: 4px 8px;
                {type_qss(TYPE_SUBHEAD)}
                text-align: left;
            }}
            QPushButton:hover {{ background: {_u.SELECTED_ROW}; }}
            QPushButton:pressed {{ background: {_u.PRESSED_WHITE}; }}
        """

    @staticmethod
    def _search_btn_qss() -> str:
        """Search-button QSS. Slightly larger radius than icon
        buttons, otherwise mirrors the WASH hover/pressed pair."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 10px;
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
        # mode / sort.
        for b in self._icon_buttons:
            b.setStyleSheet(self._icon_btn_qss())
        # View dropdown + search button — bespoke QSS each.
        if hasattr(self, "view_btn"):
            self.view_btn.setStyleSheet(self._view_btn_qss())
        if hasattr(self, "search_btn"):
            self.search_btn.setStyleSheet(self._search_btn_qss())
        # Title label color.
        if hasattr(self, "title_label"):
            self.title_label.setStyleSheet(self._title_label_qss())
        # Separator hairline.
        if hasattr(self, "_separator"):
            self._separator.setStyleSheet(self._separator_qss())

    def set_title(self, text: str):
        self.title_label.setText(text or "")

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
        menu = opaque_menu(self)
        from modules.theme import get_active_theme, _hex_to_rgb

        _ar, _ag, _ab = _hex_to_rgb(get_active_theme().accent)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {BG_PANEL};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 7px 22px 7px 14px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{ background: rgba({_ar},{_ag},{_ab},0.2); }}
        """)
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
        # Pop below the button, left-aligned.
        pt = self.view_btn.mapToGlobal(self.view_btn.rect().bottomLeft())
        # Park focus on the button so the library grid behind us loses
        # focus (and its _keyboard_mode resets) — otherwise on KDE
        # Wayland arrow keys leak through to the grid even with the
        # menu visible.
        self.view_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        self._exec_menu_with_kbd_grab(menu, pt)
