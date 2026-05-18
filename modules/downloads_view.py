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
    """One downloaded item — name, a kind/state sub-line, a Remove
    button. ``update_state`` is driven by ``download_progress``."""

    remove_requested = Signal(str)  # item_id

    def __init__(self, node: Dict, parent=None):
        super().__init__(parent)
        self._item_id = node.get("item_id", "")
        self._kind = node.get("kind", "")
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

        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {TEXT_DIM}; "
            f"background: transparent; border: 1px solid {TEXT_FAINT}; "
            f"border-radius: {RADIUS_LG}px; padding: 4px 12px; }} "
            f"QPushButton:hover {{ color: {TEXT}; border-color: {TEXT_DIM}; }}"
        )
        self._remove_btn.clicked.connect(lambda: self.remove_requested.emit(self._item_id))
        row.addWidget(self._remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.update_state(node.get("state", ""), 1.0)

    def update_state(self, state: str, fraction: float) -> None:
        """Refresh the sub-line for a lifecycle transition. ``complete``
        re-reads the on-disk size; ``downloading`` shows a percentage;
        the rest are short status strings."""
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
        else:  # stale, or anything unrecognised — show what we have
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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_MD)

        self._storage = QLabel()
        self._storage.setStyleSheet(f"{type_qss(TYPE_HEADING)} color: {TEXT};")
        outer.addWidget(self._storage)

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

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_autofade_scrollbars(self._scroll)

        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(SPACE_SM)
        self._list.addStretch(1)
        self._scroll.setWidget(self._list_host)
        outer.addWidget(self._scroll, 1)

        self._empty = QLabel(
            "No downloads yet.\nRight-click an album, playlist, artist, or track to download it."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"{type_qss(TYPE_BODY)} color: {TEXT_FAINT}; padding: {SPACE_XL}px;"
        )
        outer.addWidget(self._empty, 1)

        bus = PlayerBus.get()
        bus.download_progress.connect(self._on_progress)
        bus.offline_mode_changed.connect(self._on_offline_mode_changed)
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
            # Insert above the trailing stretch.
            self._list.insertWidget(self._list.count() - 1, row)
            self._rows[item_id] = row

        has_any = bool(self._rows)
        self._scroll.setVisible(has_any)
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

    # ── Live updates ────────────────────────────────────────────────────────

    def _on_progress(self, item_id: str, state: str, fraction: float) -> None:
        row = self._rows.get(item_id)
        if row is not None:
            if state == "removed":
                row.setParent(None)
                row.deleteLater()
                del self._rows[item_id]
                if not self._rows:
                    self._scroll.setVisible(False)
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
