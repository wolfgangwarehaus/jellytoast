"""Main-content view that lists every explicitly-downloaded item.

Lives in the same content stack as the album grid / artist grid / now-
playing page; reached via the "Downloads" tab in the top bar's library
dropdown. Replaces the per-row list that used to sit at the bottom of
Settings → Downloads — that surface now holds only the queue controls
(storage, aggregate progress, pause/resume, library walk, toggles).

The row widget (``modules.downloads_view._DownloadRow``) is reused
verbatim so the per-row affordances (Re-sync, Remove, stale badge,
state sub-line) stay identical across both surfaces.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modules import offline
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    type_qss,
)
from modules.downloads_view import _CASCADE_KINDS, _DownloadRow
from modules.player_state import PlayerBus
from modules.ui_helpers import (
    TEXT_FAINT,
    install_autofade_scrollbars,
)


class DownloadsLibraryView(QWidget):
    """Standalone "Downloads" page reached from the top bar's library
    dropdown. One row per user-requested download node; live updates
    via ``PlayerBus.download_progress``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Top-level object name so tests / QSS can address the view itself
        # (the per-download rows already carry jtDownloadRow).
        self.setObjectName("downloadsLibraryView")
        self.setStyleSheet("background: transparent;")
        self._rows: Dict[str, _DownloadRow] = {}

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        page_layout.setSpacing(SPACE_MD)

        # No page title or storage read-out: the top-bar "Downloads" nav label
        # already names the surface, and the on-disk total lives on the
        # Settings → Downloads page — a second copy here just crowds the list.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_autofade_scrollbars(self._scroll)

        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(SPACE_SM)
        self._list.addStretch(1)
        self._scroll.setWidget(self._list_host)
        page_layout.addWidget(self._scroll, 1)

        self._empty = QLabel(
            "No downloads yet.\nRight-click an album, playlist, artist, or "
            "track to download it."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"{type_qss(TYPE_BODY)} color: {TEXT_FAINT}; padding: {SPACE_XL}px;"
        )
        page_layout.addWidget(self._empty, 1)

        bus = PlayerBus.get()
        bus.download_progress.connect(self._on_progress)
        self.reload()

    # ── Population ──────────────────────────────────────────────────────────

    def reload(self) -> None:
        for row in self._rows.values():
            self._list.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        for node in offline.list_downloads():
            item_id = node.get("item_id", "")
            if not item_id:
                continue
            row = _DownloadRow(node, parent=self._list_host)
            row.remove_requested.connect(self._on_remove_requested)
            row.resync_requested.connect(self._on_resync_requested)
            self._list.insertWidget(self._list.count() - 1, row)
            self._rows[item_id] = row

        self._refresh_visibility()

    def _refresh_visibility(self) -> None:
        """Show the scroll list when there are downloads, else the centered
        empty state. Hiding the whole scroll (not just its inner host) lets the
        empty label own the full content area and sit dead-centre."""
        has_any = bool(self._rows)
        self._scroll.setVisible(has_any)
        self._empty.setVisible(not has_any)

    # ── Live updates ────────────────────────────────────────────────────────

    def _on_progress(self, item_id: str, state: str, fraction: float) -> None:
        row = self._rows.get(item_id)
        if row is not None:
            if state == offline.DownloadState.REMOVED:
                self._list.removeWidget(row)
                row.hide()
                row.setParent(None)
                row.deleteLater()
                del self._rows[item_id]
                if not self._rows:
                    self._refresh_visibility()
                return
            row.update_state(state, fraction)
        elif state == offline.DownloadState.PENDING:
            self._add_row_for_id(item_id)
            if self._rows:
                self._refresh_visibility()

    def _add_row_for_id(self, item_id: str) -> None:
        for node in offline.list_downloads():
            if node.get("item_id") != item_id:
                continue
            row = _DownloadRow(node, parent=self._list_host)
            row.remove_requested.connect(self._on_remove_requested)
            row.resync_requested.connect(self._on_resync_requested)
            self._list.insertWidget(self._list.count() - 1, row)
            self._rows[item_id] = row
            return

    # ── Removal / Re-sync ───────────────────────────────────────────────────

    def _on_remove_requested(self, item_id: str) -> None:
        row = self._rows.get(item_id)
        kind = row._kind if row is not None else ""
        if kind in _CASCADE_KINDS:
            kind_label = {"album": "album", "artist": "artist", "playlist": "playlist"}[kind]
            confirm = QMessageBox(self)
            confirm.setWindowTitle("Remove download")
            confirm.setText(
                f"Remove this {kind_label} and every downloaded track inside it?"
            )
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
            )
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
        offline.remove(item_id)

    def _on_resync_requested(self, item_id: str) -> None:
        from modules.async_io import run_async

        row = self._rows.get(item_id)
        if row is None:
            return
        row.set_resyncing(True)

        from modules.offline import _index

        def _done(result):
            r = self._rows.get(item_id)
            if r is None:
                return
            if result and result.get("error"):
                r.set_resync_failed()
                return
            node = _index.get_node(item_id)
            state = (node or {}).get("state") or offline.DownloadState.COMPLETE
            r._resyncing = False
            r._resync_btn.setEnabled(True)
            r._remove_btn.setEnabled(True)
            r.update_state(state, 1.0)

        def _err(_exc):
            r = self._rows.get(item_id)
            if r is not None:
                r.set_resync_failed()

        run_async(offline.resync, item_id, on_result=_done, on_error=_err)
