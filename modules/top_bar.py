"""
Native top navigation bar — replaces Jellyfin Web's .skinHeader so the
header zone shares the host window's translucent body color and can't
fight us on transparency. Buttons drive QWebEngineView navigation; the
drawer button calls into a JS helper that clicks Jellyfin Web's own
drawer trigger.
"""

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QAction, QCursor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QFrame, QMenu

from modules.icons import icon
from modules.ui_helpers import TEXT, TEXT_DIM, BORDER, BG_PANEL


# Library tab label sets — keyed by Jellyfin Web's collection type.
# Selecting an item programmatically clicks the matching tab button in
# the (still-rendered, just visually-suppressed) Jellyfin Web tab strip.
_LIBRARY_TABS = {
    "music": ["Albums", "Suggestions", "Album artists", "Artists",
              "Playlists", "Songs", "Genres"],
    "movies": ["Movies", "Suggestions", "Trailers", "Favorites",
               "Collections", "Genres"],
    "tvshows": ["Shows", "Suggestions", "Latest", "Upcoming",
                "Genres", "Networks", "Episodes"],
    "books": ["Books", "Suggestions", "Genres"],
    "homevideos": ["Videos", "Photos", "Albums"],
    "music_videos": ["Music videos"],
}


class JtTopBar(QWidget):
    nav_requested = pyqtSignal(str)        # "back" | "forward" | "home" | "search" | "preferences"
    drawer_toggle_requested = pyqtSignal()
    cast_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    tab_requested = pyqtSignal(int, str)   # (tab index in collection list, label)

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

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(2)

        # Left cluster: navigation
        self.back_btn = self._icon_btn("back", "Back")
        self.fwd_btn = self._icon_btn("forward", "Forward")
        self.home_btn = self._icon_btn("home", "Home")
        self.drawer_btn = self._icon_btn("menu", "Menu")
        self.back_btn.clicked.connect(lambda: self.nav_requested.emit("back"))
        self.fwd_btn.clicked.connect(lambda: self.nav_requested.emit("forward"))
        self.home_btn.clicked.connect(lambda: self.nav_requested.emit("home"))
        self.drawer_btn.clicked.connect(self.drawer_toggle_requested.emit)
        for b in (self.back_btn, self.fwd_btn, self.home_btn, self.drawer_btn):
            layout.addWidget(b)

        # Subtle divider between nav cluster and title
        sep = QFrame()
        sep.setFixedSize(1, 18)
        sep.setStyleSheet("background: rgba(255,255,255,0.08);")
        layout.addSpacing(10)
        layout.addWidget(sep)
        layout.addSpacing(14)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: 600;"
            "letter-spacing: 0.2px;"
        )
        layout.addWidget(self.title_label)
        # Breathing room between the section title and the View dropdown
        # so they don't read as one tightly-coupled cluster.
        layout.addSpacing(22)

        # Library tab dropdown — borderless text + chevron. The label
        # tracks the currently active tab (e.g. "Albums"); clicking
        # opens a menu of all tabs for the current collection.
        # Visible only on library pages.
        self.view_btn = QPushButton("Albums")
        self.view_btn.setIcon(icon("chevron_down"))
        self.view_btn.setIconSize(QSize(14, 14))
        self.view_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_btn.setToolTip("Switch library view")
        self.view_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT};
                border: none;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 14px; font-weight: 500;
                text-align: left;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
            QPushButton:pressed {{ background: rgba(255,255,255,0.12); }}
        """)
        self.view_btn.clicked.connect(self._show_view_menu)
        self.view_btn.hide()  # shown only when collection is set
        self._view_collection = ""
        layout.addWidget(self.view_btn)
        layout.addStretch(1)

        # Right cluster: actions
        self.search_btn = self._icon_btn("search", "Search")
        self.search_btn.clicked.connect(lambda: self.nav_requested.emit("search"))
        layout.addWidget(self.search_btn)

        self.cast_btn = self._icon_btn("cast", "Cast")
        self.cast_btn.clicked.connect(self.cast_requested.emit)
        layout.addWidget(self.cast_btn)

        self.settings_btn = self._icon_btn("settings", "JellyToast settings")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)

        self.user_btn = self._icon_btn("user", "Jellyfin account")
        self.user_btn.clicked.connect(lambda: self.nav_requested.emit("preferences"))
        layout.addWidget(self.user_btn)

    def _icon_btn(self, name: str, tooltip: str) -> QPushButton:
        b = QPushButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(34, 34)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
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
