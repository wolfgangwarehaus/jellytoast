"""
Native top navigation bar — replaces Jellyfin Web's .skinHeader so the
header zone shares the host window's translucent body color and can't
fight us on transparency. Buttons drive QWebEngineView navigation; the
drawer button calls into a JS helper that clicks Jellyfin Web's own
drawer trigger.
"""

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QFrame, QMenu

from modules.icons import icon
from modules.ui_helpers import TEXT, TEXT_DIM, BORDER, BG_PANEL
from modules.design_tokens import TYPE_SUBHEAD, type_qss


# Library tab label sets — keyed by Jellyfin Web's collection type.
# Selecting an item programmatically clicks the matching tab button in
# the (still-rendered, just visually-suppressed) Jellyfin Web tab strip.
_LIBRARY_TABS = {
    "music": ["Albums", "Suggestions", "Artists",
              "Playlists", "Songs", "Genres"],
    "movies": ["Movies", "Suggestions", "Trailers", "Favorites",
               "Collections", "Genres"],
    "tvshows": ["Shows", "Suggestions", "Latest", "Upcoming",
                "Genres", "Networks", "Episodes"],
    "books": ["Books", "Suggestions", "Genres"],
    "homevideos": ["Videos", "Photos", "Albums"],
    "music_videos": ["Music videos"],
}


# (label, Jellyfin SortBy parameter — comma chains add a deterministic
# tiebreaker so albums with the same primary value stay in a stable
# alphabetical order).
LIBRARY_SORT_OPTIONS = [
    ("Name",            "SortName"),
    ("Album artist",    "AlbumArtist,SortName"),
    ("Release date",    "PremiereDate,SortName"),
    ("Date added",      "DateCreated,SortName"),
    ("Recently played", "DatePlayed,SortName"),
]


class JtTopBar(QWidget):
    nav_requested = Signal(str)        # "back" | "forward" | "home" | "search" | "preferences"
    drawer_toggle_requested = Signal()
    cast_requested = Signal()
    settings_requested = Signal()
    tab_requested = Signal(int, str)   # (tab index in collection list, label)
    # Library controls cluster — visible only when the host swaps in a
    # native library grid (set_library_controls_visible(True)).
    shuffle_all_requested = Signal()
    view_mode_changed = Signal(str)    # "grid" | "list"
    sort_changed = Signal(str, str)    # (Jellyfin SortBy key, "ascending" | "descending")

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

        # 3-column layout — left, center, right each carry stretch=1
        # so the center column lands at the bar's geometric center
        # regardless of how wide the side columns' content grows. This
        # is what keeps the View dropdown + library controls cluster
        # truly centered, instead of being offset by asymmetric side
        # content (long titles on the left, single search button on
        # the right).
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
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
        self.drawer_btn = self._icon_btn("menu", "Menu")
        self.back_btn.clicked.connect(lambda: self.nav_requested.emit("back"))
        self.fwd_btn.clicked.connect(lambda: self.nav_requested.emit("forward"))
        self.home_btn.clicked.connect(lambda: self.nav_requested.emit("home"))
        self.drawer_btn.clicked.connect(self.drawer_toggle_requested.emit)
        for b in (self.back_btn, self.fwd_btn, self.home_btn, self.drawer_btn):
            left_layout.addWidget(b)

        # Subtle divider between nav cluster and title
        sep = QFrame()
        sep.setFixedSize(1, 18)
        sep.setStyleSheet("background: rgba(255,255,255,0.08);")
        left_layout.addSpacing(10)
        left_layout.addWidget(sep)
        left_layout.addSpacing(14)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.2px;"
        )
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
        self.view_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT};
                border: none;
                border-radius: 6px;
                padding: 4px 8px;
                {type_qss(TYPE_SUBHEAD)}
                text-align: left;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
            QPushButton:pressed {{ background: rgba(255,255,255,0.12); }}
        """)
        self.view_btn.clicked.connect(self._show_view_menu)
        self.view_btn.hide()  # shown only when collection is set
        self._view_collection = ""
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
        # "I'm in grid view" at a glance.
        self._view_mode = "grid"
        self.view_mode_btn = self._icon_btn("grid", "Toggle grid / list")
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

        # Search lives here, sized slightly larger than the standard
        # icon button so it reads as a primary action and pairs
        # comfortably with the X close in the titlebar above.
        self.search_btn = QPushButton()
        self.search_btn.setIcon(icon("search"))
        self.search_btn.setIconSize(QSize(22, 22))
        self.search_btn.setFixedSize(40, 40)
        self.search_btn.setToolTip("Search")
        self.search_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.10); }
            QPushButton:pressed { background: rgba(255, 255, 255, 0.16); }
        """)
        self.search_btn.clicked.connect(lambda: self.nav_requested.emit("search"))
        right_layout.addWidget(self.search_btn)

        # Equal stretch on each column = the center column is at the
        # bar's geometric center regardless of side content.
        layout.addWidget(left_col, 1)
        layout.addWidget(center_col, 1)
        layout.addWidget(right_col, 1)

    def set_library_controls_visible(self, visible: bool):
        """Show/hide the Shuffle + View toggle + Sort cluster. The host
        flips this to True when a native library grid is the active
        content surface, False when JF Web's built-in controls take
        over (web view shows its own shuffle/sort/view controls)."""
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
        menu = QMenu(self)
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
            QMenu::item:selected {{ background: rgba(255,255,255,0.10); }}
            QMenu::separator {{
                height: 1px;
                background: rgba(255,255,255,0.08);
                margin: 4px 8px;
            }}
        """)
        # Section 1: sort criterion. Checkable so Qt renders a native
        # check beside the active option.
        for label, key in LIBRARY_SORT_OPTIONS:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(self._current_sort == (label, key))
            act.triggered.connect(
                lambda _checked=False, l=label, k=key: self._on_sort_picked(l, k)
            )
            menu.addAction(act)
        menu.addSeparator()
        # Section 2: sort order. Same menu, two more checkable items.
        for order_label, order_key in (("Ascending", "ascending"),
                                       ("Descending", "descending")):
            act = QAction(order_label, menu)
            act.setCheckable(True)
            act.setChecked(self._sort_order == order_key)
            act.triggered.connect(
                lambda _checked=False, o=order_key: self._on_sort_order_picked(o)
            )
            menu.addAction(act)
        pt = self.sort_btn.mapToGlobal(self.sort_btn.rect().bottomLeft())
        menu.popup(pt)

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
        self.sort_btn.setToolTip(
            f"Sort: {self._current_sort[0]} ({order_label})"
        )

    def _icon_btn(self, name: str, tooltip: str) -> QPushButton:
        b = QPushButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(34, 34)
        b.setToolTip(tooltip)
        b.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 8px;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.10);
            }
            QPushButton:pressed {
                background: rgba(255, 255, 255, 0.16);
            }
        """)
        return b

    def set_title(self, text: str):
        self.title_label.setText(text or "")

    def set_collection(self, collection_type: str):
        """Show/hide the View dropdown based on what kind of library
        page we're on. `collection_type` matches Jellyfin's
        `collectionType` query param (music, movies, tvshows, …).
        Empty string hides the dropdown."""
        self._view_collection = (collection_type or "").lower()
        tabs = _LIBRARY_TABS.get(self._view_collection, [])
        self.view_btn.setVisible(bool(tabs))
        # Default the label to the first tab whenever we land on a new
        # library — get refined later by set_active_tab once we've
        # polled the DOM for the actually-selected tab.
        if tabs and self.view_btn.text() not in tabs:
            self.view_btn.setText(tabs[0])

    def set_active_tab(self, label: str):
        """Update the dropdown label to reflect the currently-selected
        Jellyfin Web tab. Called after URL changes and after the user
        picks a tab from our dropdown menu."""
        if not label:
            return
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
        menu = QMenu(self)
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
            QMenu::item:selected {{ background: rgba(255,255,255,0.10); }}
        """)
        for idx, label in enumerate(tabs):
            act = QAction(label, menu)
            act.triggered.connect(
                lambda _checked=False, i=idx, lbl=label: self.tab_requested.emit(i, lbl)
            )
            menu.addAction(act)
        # Pop below the button, left-aligned.
        pt = self.view_btn.mapToGlobal(self.view_btn.rect().bottomLeft())
        menu.popup(pt)
