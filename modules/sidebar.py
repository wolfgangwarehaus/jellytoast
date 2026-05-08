"""
Native sidebar drawer — Phase 5 of the native-UI pivot.

Replaces Jellyfin Web's drawer with a JellyToast-owned panel that
slides in from the left when the hamburger is clicked. v1 hosts the
buttons that used to live in the top bar's right cluster (Settings +
Account); future sessions will deepen this into a proper settings
overhaul (inline panels, server switcher, library shortcuts).

The widget is meant to be installed as an overlay layer inside the
central QStackedLayout (alongside the chrome and loading overlay), so
the panel sits above the content without disturbing layout. The
backdrop covering the rest of the window is click-dismissable and
slightly tinted to dim the content underneath.
"""

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
)

from modules.icons import icon
from modules.ui_helpers import BORDER, TEXT, TEXT_DIM
from modules.design_tokens import (
    TYPE_TITLE, TYPE_BODY, TYPE_MICRO, type_qss,
    SPACE_SM, SPACE_LG, SPACE_XL,
)


PANEL_WIDTH = 280


class _SidebarRow(QPushButton):
    """A row entry in the sidebar — icon + label, full panel width.
    Behaves like a tall flat button. Hover/press tints follow the
    same idiom as the top-bar icon buttons so the sidebar reads as
    part of the same control family."""

    ROW_HEIGHT = 44

    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(parent)
        self.setIcon(icon(icon_name))
        self.setIconSize(QSize(18, 18))
        self.setText(f"  {label}")
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT};
                border: none;
                border-radius: 8px;
                padding: 0 {SPACE_LG}px;
                {type_qss(TYPE_BODY)}
                text-align: left;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
            QPushButton:pressed {{ background: rgba(255,255,255,0.14); }}
        """)


class Sidebar(QWidget):
    """Slide-in drawer overlay. Lives inside the central QStackedLayout
    above the chrome layer; hidden by default. Toggle visibility with
    `set_open(True/False)` or `toggle()`. Clicking the backdrop or
    pressing Escape closes the panel."""

    settings_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("jtSidebar")
        # Hidden by default; the host shows the widget when the
        # hamburger is clicked.
        self.setVisible(False)
        # Catch keyboard focus so Escape works without the user clicking
        # somewhere first.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Panel (left, fixed width) ─────────────────────────────────
        self._panel = QFrame()
        self._panel.setObjectName("jtSidebarPanel")
        self._panel.setFixedWidth(PANEL_WIDTH)
        self._panel.setStyleSheet(f"""
            QFrame#jtSidebarPanel {{
                background: rgba(20, 22, 26, 0.94);
                border-right: 1px solid {BORDER};
            }}
        """)

        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(SPACE_LG, SPACE_XL, SPACE_LG, SPACE_LG)
        panel_layout.setSpacing(SPACE_SM)

        # Brand title at top — keeps the panel's identity even before
        # the deeper settings UI lands.
        title = QLabel("JellyToast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)} font-weight: 600;")
        panel_layout.addWidget(title)

        kicker = QLabel("MENU")
        kicker.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_MICRO)} "
            f"padding-top: {SPACE_LG}px;"
        )
        panel_layout.addWidget(kicker)

        # Rows. Settings opens the existing modal dialog, which now
        # also hosts the Account section (server URL, signed-in user,
        # sign-out). The separate sidebar Account row that used to
        # route to JF Web's preferences page is gone — that was the
        # last user-clicked entry into the JF Web embed.
        self._settings_row = _SidebarRow("settings", "Settings")
        self._settings_row.clicked.connect(self._on_settings_clicked)
        panel_layout.addWidget(self._settings_row)

        panel_layout.addStretch(1)

        outer.addWidget(self._panel)

        # ── Backdrop (right, fills remaining width) ───────────────────
        # Clickable transparent dimmer — pressing it closes the drawer.
        self._backdrop = _Backdrop()
        self._backdrop.clicked.connect(lambda: self.set_open(False))
        outer.addWidget(self._backdrop, 1)

    def is_open(self) -> bool:
        return self.isVisible()

    def set_open(self, open_: bool):
        if open_ and not self.isVisible():
            self.show()
            self.raise_()
            self.setFocus()
        elif not open_ and self.isVisible():
            self.hide()

    def toggle(self):
        self.set_open(not self.is_open())

    def _on_settings_clicked(self):
        self.set_open(False)
        self.settings_clicked.emit()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.set_open(False)
            return
        super().keyPressEvent(event)


class _Backdrop(QWidget):
    """The dimmed clickable area to the right of the sidebar panel.
    Emits `clicked` on left-press so the sidebar can close itself."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: rgba(0, 0, 0, 0.40);")
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)
