"""Update-available chip — a small rounded button in the top bar's right
column (next to the offline chip), shown only when ``jellytoast/updates.py``
finds a newer release on a manual install channel.

Hidden until ``PlayerBus.update_available`` fires; then it reads "Update 0.1.6"
and a click opens a small menu — **Download** (deep-linked to the right asset),
**What's new** (release notes), **Dismiss** (hide + remember the version so it
doesn't re-nag). Mirrors ``offline_banner.OfflineChip``'s subtle accent-pill
styling and live-accent refresh.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QPushButton

from jellytoast.design_tokens import TYPE_CAPTION, rad, type_qss
from jellytoast.player_state import PlayerBus
from jellytoast.ui_helpers import ACCENT, TEXT_DIM, opaque_menu


class UpdateChip(QPushButton):
    """Compact "a newer version is out" pill. Subtle by design — present but
    not shouting — matching the offline chip beside it."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setObjectName("jtUpdateChip")
        self.setFixedHeight(24)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._version = ""
        self._download_url = ""
        self._notes_url = ""
        self._menu = None  # kept alive across a non-blocking popup()
        self._apply_style()
        self.clicked.connect(self._open_menu)
        self.setVisible(False)

        bus = PlayerBus.get()
        bus.update_available.connect(self._on_update_available)
        # Live-accent: rebuild the stylesheet when the accent changes.
        bus.theme_changed.connect(self._apply_style)

    # ── Style ───────────────────────────────────────────────────────────
    def _apply_style(self, *_: object) -> None:
        r, g, b = self._accent_rgb()
        self.setStyleSheet(
            f"QPushButton#jtUpdateChip {{ "
            f"background: rgba({r},{g},{b},0.16); color: {TEXT_DIM}; "
            f"border: 1px solid rgba({r},{g},{b},0.40); "
            f"border-radius: {rad(6)}px; padding: 0 10px; "
            f"{type_qss(TYPE_CAPTION)} }}"
            f"QPushButton#jtUpdateChip:hover {{ "
            f"background: rgba({r},{g},{b},0.26); "
            f"border-color: rgba({r},{g},{b},0.60); }}"
        )

    @staticmethod
    def _accent_rgb() -> tuple[int, int, int]:
        try:
            s = ACCENT.lstrip("#")
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except Exception:
            return 138, 91, 228

    # ── Bus handler ─────────────────────────────────────────────────────
    def _on_update_available(self, version: str, download_url: str, notes_url: str) -> None:
        self._version = version
        self._download_url = download_url
        self._notes_url = notes_url
        self.setText(self.tr("Update {0}").format(version))
        self.setToolTip(self.tr("jellytoast {0} is available").format(version))
        self.setVisible(True)

    # ── Interaction ─────────────────────────────────────────────────────
    def _open_menu(self) -> None:
        if not self._version:
            return
        menu = opaque_menu(self)
        download = QAction(self.tr("Download"), menu)
        download.triggered.connect(lambda: self._open(self._download_url))
        menu.addAction(download)
        notes = QAction(self.tr("What's new"), menu)
        notes.triggered.connect(lambda: self._open(self._notes_url))
        menu.addAction(notes)
        menu.addSeparator()
        dismiss = QAction(self.tr("Dismiss"), menu)
        dismiss.triggered.connect(self._dismiss)
        menu.addAction(dismiss)
        self._menu = menu  # prevent GC during the non-blocking popup
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    @staticmethod
    def _open(url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _dismiss(self) -> None:
        try:
            from jellytoast.settings import get_settings

            get_settings().update_dismissed_version = self._version
        except Exception:
            pass
        self.setVisible(False)
