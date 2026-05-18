"""Downloads screen — manage explicitly-downloaded music.

Lists every user-requested download (``offline.list_downloads()``) with
live progress, on-disk size, and a per-row remove. Embedded as the
"Downloads" page of the settings dialog (design doc §7).

The list is user-curated and small — dozens of items, not the thousands
a library grid holds — so it's plain per-row ``QFrame``s in a scroll
area rather than the ``QAbstractListModel`` / delegate / ``QListView``
scaffolding the big browse surfaces need (see the model/view note in
project memory: per-row widgets are only the perf wall for *big* lists).

Live updates ride ``PlayerBus.download_progress``. The signal fires for
both leaf tracks and the user-requested roots; only the roots have rows
here, so a row updates in place when its own id comes through, a brand-
new download (id not shown, ``state == "pending"`` — emitted only for a
user-requested root) triggers a reload, and leaf-track noise is ignored.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modules import offline
from modules.player_state import PlayerBus
from modules.settings import get_settings
from modules.ui_helpers import (
    ACCENT,
    BG_CARD,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WARN_FG,
    install_autofade_scrollbars,
)
from modules.design_tokens import (
    RADIUS_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_HEADING,
    type_qss,
)


# Human-readable node kinds for the row sub-line.
_KIND_LABEL = {
    "track": "Track",
    "album": "Album",
    "artist": "Artist",
    "playlist": "Playlist",
}
# Kinds whose removal cascades to child tracks — confirmed before remove
# (design doc §5.7). A lone track is low-stakes and skips the dialog.
_CASCADE_KINDS = {"album", "artist", "playlist"}


def _fmt_size(n: int) -> str:
    """Bytes -> a compact human string. 0 renders as a dash so an
    in-progress / failed row doesn't claim it occupies 0 bytes."""
    if n <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.0f} {units[i]}" if i == 0 else f"{size:.1f} {units[i]}"


class _DownloadRow(QFrame):
    """One downloaded item — name, a kind/state sub-line, a Re-sync and
    a Remove button. ``update_state`` is driven by
    ``download_progress``."""

    remove_requested = Signal(str)  # item_id
    resync_requested = Signal(str)  # item_id

    def __init__(self, node: Dict, parent=None):
        super().__init__(parent)
        self._item_id = node.get("item_id", "")
        self._kind = node.get("kind", "")
        self._resyncing = False
        self.setObjectName("jtDownloadRow")
        self.setStyleSheet(
            f"#jtDownloadRow {{ background: {BG_CARD}; border-radius: {RADIUS_LG}px; }}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self._name = QLabel(node.get("name") or self._item_id)
        self._name.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {TEXT};")
        self._sub = QLabel()
        self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")
        text_col.addWidget(self._name)
        text_col.addWidget(self._sub)
        row.addLayout(text_col, 1)

        _ghost_btn_qss = (
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {TEXT_DIM}; "
            f"background: transparent; border: 1px solid {TEXT_FAINT}; "
            f"border-radius: {RADIUS_LG}px; padding: 4px 12px; }} "
            f"QPushButton:hover {{ color: {TEXT}; border-color: {TEXT_DIM}; }} "
            f"QPushButton:disabled {{ color: {TEXT_FAINT}; border-color: {TEXT_FAINT}; }}"
        )

        self._resync_btn = QPushButton("Re-sync")
        self._resync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._resync_btn.setStyleSheet(_ghost_btn_qss)
        self._resync_btn.clicked.connect(lambda: self.resync_requested.emit(self._item_id))
        row.addWidget(self._resync_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_btn.setStyleSheet(_ghost_btn_qss)
        self._remove_btn.clicked.connect(lambda: self.remove_requested.emit(self._item_id))
        row.addWidget(self._remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.update_state(node.get("state", ""), 1.0)

    def set_resyncing(self, on: bool) -> None:
        """Toggle the in-flight resync UI: lock the action buttons and
        flip the sub-line to an ACCENT-coloured progress note. Cleared
        on the next ``update_state`` from the resync result."""
        self._resyncing = on
        self._resync_btn.setEnabled(not on)
        self._remove_btn.setEnabled(not on)
        if on:
            kind_label = _KIND_LABEL.get(self._kind, self._kind.title())
            self._sub.setText(f"{kind_label} · Re-syncing…")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {ACCENT};")

    def set_resync_failed(self) -> None:
        """Show a resync-failed sub-line. Same WARN_FG treatment as a
        failed download — the user needs to know the snapshot didn't
        refresh."""
        self._resyncing = False
        self._resync_btn.setEnabled(True)
        self._remove_btn.setEnabled(True)
        kind_label = _KIND_LABEL.get(self._kind, self._kind.title())
        self._sub.setText(f"{kind_label} · Re-sync failed")
        self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {WARN_FG};")

    def update_state(self, state: str, fraction: float) -> None:
        """Refresh the sub-line for a lifecycle transition. ``complete``
        re-reads the on-disk size; ``downloading`` shows a percentage;
        the rest are short status strings."""
        # An in-flight resync owns the sub-line until it completes; the
        # progress bus can still tick for other rows in the meantime.
        if self._resyncing:
            return
        kind_label = _KIND_LABEL.get(self._kind, self._kind.title())
        if state == "complete":
            size = _fmt_size(offline.item_size(self._item_id))
            self._sub.setText(f"{kind_label} · {size}")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")
        elif state == "downloading":
            pct = max(0, min(100, int(round(fraction * 100))))
            self._sub.setText(f"{kind_label} · Downloading… {pct}%")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {ACCENT};")
        elif state == "pending":
            self._sub.setText(f"{kind_label} · Queued…")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")
        elif state == "failed":
            self._sub.setText(f"{kind_label} · Download failed")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {WARN_FG};")
        elif state == "stale":
            size = _fmt_size(offline.item_size(self._item_id))
            self._sub.setText(f"{kind_label} · Stale · {size}")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {WARN_FG};")
        else:  # unrecognised — show what we have
            size = _fmt_size(offline.item_size(self._item_id))
            self._sub.setText(f"{kind_label} · {size}")
            self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")


class DownloadsView(QWidget):
    """The "Downloads" settings page — storage read-out + the managed
    list of downloads."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._rows: Dict[str, _DownloadRow] = {}

        # The page is taller than the settings dialog at typical
        # heights once Phase 6 added pause + wifi-only + per-row
        # re-sync. Wrap the whole page in one scroll area; the inline
        # downloads list flows inside it (no nested scroll regions).
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_autofade_scrollbars(page_scroll)

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_MD)

        self._storage = QLabel()
        self._storage.setStyleSheet(f"{type_qss(TYPE_HEADING)} color: {TEXT};")
        outer.addWidget(self._storage)

        # Queue-level pause/resume — primary action for the page. Sits
        # right under the storage read-out so it reads as "the queue is
        # the second thing about your downloads worth knowing".
        pause_row = QHBoxLayout()
        pause_row.setContentsMargins(0, 0, 0, 0)
        pause_row.setSpacing(SPACE_SM)
        self._pause_btn = QPushButton()
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_BODY)} color: {TEXT}; "
            f"background: transparent; border: 1px solid {TEXT_DIM}; "
            f"border-radius: {RADIUS_LG}px; padding: 6px 16px; }} "
            f"QPushButton:hover {{ border-color: {TEXT}; }}"
        )
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        pause_row.addWidget(self._pause_btn)
        pause_row.addStretch(1)
        outer.addLayout(pause_row)
        self._refresh_pause_label()

        # Offline mode — explicit user toggle. Routed through the
        # offline package so the bus signal fires + persistence
        # happens in one place. Bus subscription keeps the checkbox
        # in sync when auto-offline flips state from a network drop.
        self._offline_mode = QCheckBox("Offline mode")
        self._offline_mode.setChecked(offline.is_offline_mode())
        self._offline_mode.toggled.connect(self._on_offline_mode_toggled)
        outer.addWidget(self._offline_mode)

        offline_note = QLabel("Show only downloaded music and play from local storage.")
        offline_note.setWordWrap(True)
        offline_note.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 22px;"
        )
        outer.addWidget(offline_note)

        # Auto-offline — flip into offline mode when the server stops
        # responding. Default on; the user toggle still wins (an
        # explicit choice survives reconnect).
        self._auto_offline = QCheckBox("Automatic offline mode")
        self._auto_offline.setChecked(get_settings().auto_offline_mode)
        self._auto_offline.toggled.connect(
            lambda v: setattr(get_settings(), "auto_offline_mode", v)
        )
        outer.addWidget(self._auto_offline)

        auto_note = QLabel(
            "Switch to offline automatically when the server can't be "
            "reached, and back when it returns."
        )
        auto_note.setWordWrap(True)
        auto_note.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 22px;"
        )
        outer.addWidget(auto_note)

        # Playback preference — when a track is downloaded, whether to
        # still stream it from the server while online. Off by default
        # (the local copy is faster and free). Offline mode / an
        # unreachable server always use the local copy regardless.
        self._prefer_server = QCheckBox("Stream from server even when a track is downloaded")
        self._prefer_server.setChecked(get_settings().prefer_server_when_online)
        self._prefer_server.toggled.connect(
            lambda v: setattr(get_settings(), "prefer_server_when_online", v)
        )
        outer.addWidget(self._prefer_server)

        prefer_note = QLabel(
            "Off: downloaded tracks play from local storage — faster, no "
            "data. Offline mode and an unreachable server always play "
            "the local copy."
        )
        prefer_note.setWordWrap(True)
        prefer_note.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 22px;"
        )
        outer.addWidget(prefer_note)

        # Wi-Fi-only gate — routed through ``offline`` so persistence +
        # the bus signal fire in one place. v1 is the toggle + a stub
        # metered-flag the future auto-detect layer will flip; the
        # toggle alone is a no-op until that lands.
        self._wifi_only = QCheckBox("Only download on Wi-Fi")
        self._wifi_only.setChecked(offline.is_wifi_only())
        self._wifi_only.toggled.connect(self._on_wifi_only_toggled)
        outer.addWidget(self._wifi_only)

        wifi_note = QLabel(
            "Downloads pause when you're on a metered or cellular "
            "connection. (Auto-detection lands in a future update — "
            "flip on now and the toggle will start gating automatically.)"
        )
        wifi_note.setWordWrap(True)
        wifi_note.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 22px;"
        )
        outer.addWidget(wifi_note)

        # Notify-on-complete — slice C of the downloads-progress feature.
        # Backend gating lives in ``manager._emit_drain_complete``; this
        # is the user-facing toggle. Queue-behaviour group (next to
        # pause / wifi-only), not asset-quality group.
        self._notify_complete = QCheckBox("Notify me when downloads finish")
        self._notify_complete.setChecked(get_settings().notify_on_download_complete)
        self._notify_complete.toggled.connect(
            lambda v: setattr(get_settings(), "notify_on_download_complete", v)
        )
        outer.addWidget(self._notify_complete)

        notify_note = QLabel(
            "Desktop notification when the download queue drains. Uses "
            "your system's notification channel."
        )
        notify_note.setWordWrap(True)
        notify_note.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 22px;"
        )
        outer.addWidget(notify_note)

        # Lazy import: settings_dialog builds this page on demand, so the
        # module is fully loaded by now and there's no import cycle.
        from modules.settings_dialog import AUDIO_QUALITIES, _OpaqueComboBox

        dq_row = QHBoxLayout()
        dq_row.setContentsMargins(0, 0, 0, 0)
        dq_row.setSpacing(SPACE_SM)
        dq_label = QLabel("Download quality:")
        dq_label.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {TEXT};")
        dq_row.addWidget(dq_label)
        self._dq_combo = _OpaqueComboBox()
        for label, key in AUDIO_QUALITIES:
            self._dq_combo.addItem(label, key)
        idx = self._dq_combo.findData(get_settings().download_quality or "original")
        if idx >= 0:
            self._dq_combo.setCurrentIndex(idx)
        self._dq_combo.currentIndexChanged.connect(self._on_download_quality_changed)
        dq_row.addWidget(self._dq_combo)
        dq_row.addStretch(1)
        outer.addLayout(dq_row)

        dq_note = QLabel(
            "Applies to new downloads. Existing downloads keep the quality they were fetched at."
        )
        dq_note.setWordWrap(True)
        dq_note.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT}; padding: 0 0 0 2px;")
        outer.addWidget(dq_note)

        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(SPACE_SM)
        self._list.addStretch(1)
        outer.addWidget(self._list_host)

        self._empty = QLabel(
            "No downloads yet.\nRight-click an album, playlist, artist, or track to download it."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"{type_qss(TYPE_BODY)} color: {TEXT_FAINT}; padding: {SPACE_XL}px;"
        )
        outer.addWidget(self._empty)
        outer.addStretch(1)

        page_scroll.setWidget(body)
        page_layout.addWidget(page_scroll, 1)

        bus = PlayerBus.get()
        bus.download_progress.connect(self._on_progress)
        bus.offline_mode_changed.connect(self._on_offline_mode_changed)
        bus.downloads_wifi_only_changed.connect(self._on_wifi_only_changed)
        bus.download_queue_paused.connect(self._refresh_pause_label)
        bus.download_queue_resumed.connect(self._refresh_pause_label)
        self.reload()

    # ── Population ──────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Rebuild the list from scratch. Cheap (the list is small) and
        the safe answer whenever the set of rows — not just one row's
        state — may have changed."""
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        nodes = offline.list_downloads()
        for node in nodes:
            item_id = node.get("item_id", "")
            if not item_id:
                continue
            row = _DownloadRow(node)
            row.remove_requested.connect(self._on_remove_requested)
            row.resync_requested.connect(self._on_resync_requested)
            # Insert above the trailing stretch.
            self._list.insertWidget(self._list.count() - 1, row)
            self._rows[item_id] = row

        has_any = bool(self._rows)
        self._list_host.setVisible(has_any)
        self._empty.setVisible(not has_any)
        self._refresh_storage()

    def _refresh_storage(self) -> None:
        total = offline.storage_usage().get("total", 0)
        self._storage.setText(f"Storage used: {_fmt_size(total)}")

    def _on_download_quality_changed(self, _idx: int = 0) -> None:
        get_settings().download_quality = self._dq_combo.currentData() or "original"

    # ── Offline-mode toggle + bus sync ──────────────────────────────────────

    def _on_offline_mode_toggled(self, on: bool) -> None:
        # Skip the no-op echo from _on_offline_mode_changed setting
        # the checkbox via setChecked (which re-emits toggled).
        if on == offline.is_offline_mode():
            return
        offline.set_offline_mode(on)

    def _on_offline_mode_changed(self, on: bool) -> None:
        if self._offline_mode.isChecked() == on:
            return
        self._offline_mode.blockSignals(True)
        self._offline_mode.setChecked(on)
        self._offline_mode.blockSignals(False)

    # ── Wi-Fi-only toggle + bus sync ────────────────────────────────────────

    def _on_wifi_only_toggled(self, on: bool) -> None:
        if on == offline.is_wifi_only():
            return
        offline.set_wifi_only(on)

    def _on_wifi_only_changed(self, on: bool) -> None:
        if self._wifi_only.isChecked() == on:
            return
        self._wifi_only.blockSignals(True)
        self._wifi_only.setChecked(on)
        self._wifi_only.blockSignals(False)

    # ── Live updates ────────────────────────────────────────────────────────

    def _on_progress(self, item_id: str, state: str, fraction: float) -> None:
        row = self._rows.get(item_id)
        if row is not None:
            if state == "removed":
                row.setParent(None)
                row.deleteLater()
                del self._rows[item_id]
                if not self._rows:
                    self._list_host.setVisible(False)
                    self._empty.setVisible(True)
                self._refresh_storage()
                return
            row.update_state(state, fraction)
            if state in ("complete", "failed"):
                self._refresh_storage()
        elif state == "pending":
            # "pending" is emitted only for a user-requested root, so an
            # id we don't have a row for means a brand-new download.
            self.reload()

    # ── Removal ─────────────────────────────────────────────────────────────

    def _on_remove_requested(self, item_id: str) -> None:
        row = self._rows.get(item_id)
        kind = row._kind if row is not None else ""
        if kind in _CASCADE_KINDS:
            name = row._name.text() if row is not None else "this download"
            confirm = QMessageBox.question(
                self,
                "Remove download",
                f"Remove the downloaded files for “{name}”?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        # offline.remove emits download_progress(item_id, "removed", 0.0),
        # which _on_progress turns into the row teardown.
        offline.remove(item_id)

    # ── Pause / Resume ──────────────────────────────────────────────────────

    def _on_pause_clicked(self) -> None:
        if offline.is_paused():
            offline.resume()
        else:
            offline.pause()

    def _refresh_pause_label(self) -> None:
        self._pause_btn.setText(
            "Resume downloads" if offline.is_paused() else "Pause downloads"
        )

    # ── Re-sync ─────────────────────────────────────────────────────────────

    def _on_resync_requested(self, item_id: str) -> None:
        row = self._rows.get(item_id)
        if row is None:
            return
        row.set_resyncing(True)

        from modules.async_io import run_async
        from modules.offline import _index

        def _done(result: Dict) -> None:
            r = self._rows.get(item_id)
            if r is None:
                return
            if result and result.get("error"):
                r.set_resync_failed()
                return
            node = _index.get_node(item_id)
            state = (node or {}).get("state") or "complete"
            r._resyncing = False
            r._resync_btn.setEnabled(True)
            r._remove_btn.setEnabled(True)
            r.update_state(state, 1.0)
            self._refresh_storage()

        def _err(_exc: Exception) -> None:
            r = self._rows.get(item_id)
            if r is not None:
                r.set_resync_failed()

        run_async(offline.resync, item_id, on_result=_done, on_error=_err)
