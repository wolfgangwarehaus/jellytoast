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


def _fmt_speed(bps: float) -> str:
    """Bytes-per-second -> a compact ``X.X MB/s`` style string. Sub-KB
    rates collapse to ``"…"`` so the slot doesn't flicker with noise
    while a chunk loop is mid-ramp."""
    if bps < 1024:
        return "…"
    units = ("KB/s", "MB/s", "GB/s")
    val = bps / 1024
    i = 0
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.1f} {units[i]}"


def _fmt_eta(seconds: float) -> str:
    """Seconds -> a human ETA. ``-1`` (the backend's "unknown" marker)
    renders as ``"calculating…"``. ``0`` means idle — never displayed."""
    if seconds < 0:
        return "calculating…"
    if seconds < 60:
        return f"{int(seconds)} s left"
    if seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        if mins < 10 and secs:
            return f"{mins} min {secs} s left"
        return f"{mins} min left"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    return f"{hours} h {mins} min left"


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


class _QueueAggregateBlock(QWidget):
    """The "downloading right now" summary that sits at the top of the
    Downloads page. Subscribes to ``PlayerBus.download_queue_stats``
    (emitted at 1 Hz by ``modules.offline.manager``) and renders one
    counts line, one speed/ETA line, and a 4 px progress bar. Hides
    itself entirely when the queue is idle so the page reads "settings"
    once nothing's downloading."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._is_paused = offline.is_paused()
        self._last_fraction = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_SM)

        # Counts on top, speed/ETA underneath. Stacking vertically
        # keeps the block compact and gives both lines plenty of room
        # to render their full text on narrower dialog widths — a
        # horizontal row clipped the tail on tighter layouts.
        self._counts = QLabel()
        self._counts.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {TEXT};")
        self._counts.setWordWrap(False)
        self._tail = QLabel()
        self._tail.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")
        self._tail.setWordWrap(False)
        outer.addWidget(self._counts)
        outer.addWidget(self._tail)

        self._bar_track = QFrame()
        self._bar_track.setFixedHeight(4)
        self._bar_track.setStyleSheet(
            f"background: {BG_CARD}; border: none; border-radius: 2px;"
        )
        self._bar_fill = QFrame(self._bar_track)
        self._bar_fill.setStyleSheet(
            f"background: {ACCENT}; border: none; border-radius: 2px;"
        )
        self._bar_fill.setGeometry(0, 0, 0, 4)
        outer.addWidget(self._bar_track)

        bus = PlayerBus.get()
        bus.download_queue_stats.connect(self._on_stats)
        bus.download_queue_paused.connect(self._on_paused_changed)
        bus.download_queue_resumed.connect(self._on_paused_changed)

        # Prime from a one-shot read so users opening the dialog mid-
        # download see the aggregate immediately, not after a 1-second
        # tick.
        try:
            active, total_session, speed_bps, eta_seconds = offline.get_queue_stats()
        except Exception:
            active, total_session, speed_bps, eta_seconds = 0, 0, 0.0, 0.0
        self._on_stats(active, total_session, speed_bps, eta_seconds)

    def _on_stats(
        self,
        active: int,
        total_session: int,
        speed_bps: float,
        eta_seconds: float,
    ) -> None:
        # Drain edge / never-started: hide the whole block.
        if active == 0 and total_session == 0:
            self.setVisible(False)
            self._last_fraction = 0.0
            return

        self.setVisible(True)

        # The signal carries job counts, not byte progress, so the bar
        # tracks "jobs completed this session" — fraction of the
        # dispatched set that's no longer active. Coarse but truthful;
        # bytes-fraction is a follow-up that needs a new helper on the
        # manager.
        if total_session > 0:
            fraction = max(0.0, min(1.0, (total_session - active) / total_session))
        else:
            fraction = 0.0
        # Don't shrink the bar mid-flight when a new dispatch lifts
        # total_session and the active count hasn't caught up — only
        # ever advance.
        if fraction < self._last_fraction:
            fraction = self._last_fraction
        self._last_fraction = fraction

        pct = int(round(fraction * 100))
        # Until enough jobs have settled to give a real fraction, the
        # number jitters — show a placeholder so the user knows we're
        # still calculating instead of "0%" looking stuck.
        pct_text = f"{pct}%" if pct > 0 else "calculating…"

        if self._is_paused:
            self._counts.setText(
                f"Paused · {active} of {total_session} waiting · {pct_text}"
            )
            self._counts.setStyleSheet(
                f"{type_qss(TYPE_BODY)} color: {TEXT_DIM};"
            )
            self._tail.setText("")
            self._apply_bar(fraction, dim=True)
            return

        # Active variant.
        self._counts.setText(
            f"Downloading {active} of {total_session} · {pct_text}"
        )
        self._counts.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {TEXT};")

        speed_text = _fmt_speed(speed_bps) if speed_bps > 0 else ""
        eta_text = _fmt_eta(eta_seconds) if eta_seconds != 0 else ""
        tail_parts = [p for p in (speed_text, eta_text) if p]
        self._tail.setText(" · ".join(tail_parts))
        self._apply_bar(fraction, dim=False)

    def _apply_bar(self, fraction: float, *, dim: bool) -> None:
        from modules import ui_helpers as _ui

        color = _ui.TEXT_DIM if dim else _ui.ACCENT
        self._bar_fill.setStyleSheet(
            f"background: {color}; border: none; border-radius: 2px;"
        )
        self._bar_track.setStyleSheet(
            f"background: {_ui.BG_CARD}; border: none; border-radius: 2px;"
        )
        width = int(self._bar_track.width() * fraction)
        self._bar_fill.setGeometry(0, 0, width, 4)

    def _on_paused_changed(self, *args) -> None:
        self._is_paused = offline.is_paused()
        # Re-render with the current numbers; cheapest is a fresh tick
        # snapshot, since paused doesn't change them.
        try:
            stats = offline.get_queue_stats()
        except Exception:
            return
        self._on_stats(*stats)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = int(self._bar_track.width() * self._last_fraction)
        self._bar_fill.setGeometry(0, 0, width, 4)

    def _reapply_accent(self) -> None:
        """Live-apply contract — re-stamp QSS using the freshly mutated
        ``ui_helpers`` colour tokens. Per ``[[architecture-live-accent]]``
        the connect lives in ``DownloadsView.__init__``; nothing in this
        widget connects to ``theme_changed`` on its own."""
        from modules import ui_helpers as _ui

        self._counts.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {_ui.TEXT};")
        self._tail.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {_ui.TEXT_DIM};"
        )
        self._apply_bar(self._last_fraction, dim=self._is_paused)


class DownloadsView(QWidget):
    """The "Downloads" settings page — storage read-out + the managed
    list of downloads."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        # Recover the "we're in a library walk" flag from persisted
        # state — a paused bulk download survives a restart and the
        # pause button should still read "Resume library download".
        # Cleared on the drain-edge stats emit.
        self._in_library_download = bool(get_settings().library_download_in_progress)

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

        # Aggregate "downloading right now" block — counts + speed +
        # ETA + a 4 px progress bar. Hidden when the queue is idle so
        # the page reads "settings" once nothing's in flight.
        self._aggregate = _QueueAggregateBlock()
        outer.addWidget(self._aggregate)

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

        # Bulk-download action — paginate the active provider's album
        # list and enqueue everything that isn't already downloaded.
        # Idempotent (already-complete items skip on the manager side).
        self._download_all_btn = QPushButton("Download entire library")
        self._download_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_all_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_BODY)} color: {TEXT}; "
            f"background: transparent; border: 1px solid {TEXT_DIM}; "
            f"border-radius: {RADIUS_LG}px; padding: 6px 16px; }} "
            f"QPushButton:hover {{ border-color: {TEXT}; }}"
        )
        self._download_all_btn.clicked.connect(self._on_download_all_clicked)
        pause_row.addWidget(self._download_all_btn)

        # Destructive sweep — wipes every downloaded item. Always
        # visible; click is gated by a confirmation dialog.
        self._clear_all_btn = QPushButton("Clear all downloads")
        self._clear_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_all_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_BODY)} color: {TEXT}; "
            f"background: transparent; border: 1px solid {TEXT_DIM}; "
            f"border-radius: {RADIUS_LG}px; padding: 6px 16px; }} "
            f"QPushButton:hover {{ border-color: {TEXT}; }}"
        )
        self._clear_all_btn.clicked.connect(self._on_clear_all_clicked)
        self._refresh_clear_all_visibility()
        pause_row.addWidget(self._clear_all_btn)
        pause_row.addStretch(1)
        outer.addLayout(pause_row)
        self._refresh_pause_label()

        # Offline mode — explicit user toggle. Routed through the
        # offline package so the bus signal fires + persistence
        # happens in one place.
        self._offline_mode = QCheckBox("Offline mode")
        self._offline_mode.setChecked(offline.is_offline_mode())
        self._offline_mode.toggled.connect(self._on_offline_mode_toggled)
        outer.addLayout(
            self._make_check_row(self._offline_mode, "Show only downloaded music")
        )

        # Auto-offline — flip into offline mode when the server stops
        # responding. Default on; explicit user toggle still wins.
        self._auto_offline = QCheckBox("Automatic offline mode")
        self._auto_offline.setChecked(get_settings().auto_offline_mode)
        self._auto_offline.toggled.connect(
            lambda v: setattr(get_settings(), "auto_offline_mode", v)
        )
        outer.addLayout(
            self._make_check_row(
                self._auto_offline, "Switch when the server is unreachable"
            )
        )

        # Playback preference — when a track is downloaded, whether to
        # still stream it from the server while online.
        self._prefer_server = QCheckBox("Always stream from server")
        self._prefer_server.setChecked(get_settings().prefer_server_when_online)
        self._prefer_server.toggled.connect(
            lambda v: setattr(get_settings(), "prefer_server_when_online", v)
        )
        outer.addLayout(
            self._make_check_row(
                self._prefer_server, "Even when the track is downloaded"
            )
        )

        # Wi-Fi-only gate.
        self._wifi_only = QCheckBox("Only download on Wi-Fi")
        self._wifi_only.setChecked(offline.is_wifi_only())
        self._wifi_only.toggled.connect(self._on_wifi_only_toggled)
        outer.addLayout(
            self._make_check_row(
                self._wifi_only, "Pause on metered connections"
            )
        )

        # Notify-on-complete — slice C of the downloads-progress feature.
        self._notify_complete = QCheckBox("Notify when downloads finish")
        self._notify_complete.setChecked(get_settings().notify_on_download_complete)
        self._notify_complete.toggled.connect(
            lambda v: setattr(get_settings(), "notify_on_download_complete", v)
        )
        outer.addLayout(
            self._make_check_row(
                self._notify_complete, "Desktop notification on completion"
            )
        )

        # Library sync — paired with the "Download entire library"
        # action; 6-hour timer re-walks the provider for new albums.
        self._library_sync = QCheckBox("Keep library in sync")
        self._library_sync.setChecked(get_settings().library_sync_enabled)
        self._library_sync.toggled.connect(self._on_library_sync_toggled)
        outer.addLayout(
            self._make_check_row(
                self._library_sync, "Re-walk for new albums every 6 hours"
            )
        )

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
        dq_note = QLabel("Applies to new downloads only")
        dq_note.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT};")
        dq_row.addWidget(dq_note)
        outer.addLayout(dq_row)

        outer.addStretch(1)

        page_scroll.setWidget(body)
        page_layout.addWidget(page_scroll, 1)

        bus = PlayerBus.get()
        bus.download_progress.connect(self._on_progress)
        bus.offline_mode_changed.connect(self._on_offline_mode_changed)
        bus.downloads_wifi_only_changed.connect(self._on_wifi_only_changed)
        bus.download_queue_paused.connect(self._refresh_pause_label)
        bus.download_queue_resumed.connect(self._refresh_pause_label)
        bus.download_queue_stats.connect(self._on_queue_stats)
        bus.theme_changed.connect(self._reapply_accent)
        self._refresh_storage()

    def _reapply_accent(self) -> None:
        """Live-apply per ``[[architecture-live-accent]]``. Re-stamps
        the aggregate block; nothing else on the page bakes accent."""
        self._aggregate._reapply_accent()

    def _make_check_row(self, checkbox: QCheckBox, note_text: str) -> QHBoxLayout:
        """Compose ``[checkbox] ··· small caption`` on one line so a
        column of these stacks tightly. Used for every toggle on the
        page; was previously a multi-line wordwrapped note below each."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)
        row.addWidget(checkbox)
        row.addStretch(1)
        note = QLabel(note_text)
        note.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_FAINT};")
        row.addWidget(note)
        return row

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

    def _on_progress(self, _item_id: str, state: str, _fraction: float) -> None:
        # The per-item list lives on ``DownloadsLibraryView`` now — this
        # surface only needs the storage read-out + the "Clear all"
        # button to stay current as blobs land / disappear.
        if state in ("complete", "failed", "removed"):
            self._refresh_storage()
        if state in ("pending", "removed"):
            self._refresh_clear_all_visibility()

    def _refresh_clear_all_visibility(self) -> None:
        """Hide "Clear all downloads" when there's nothing to clear.
        Cheap (a single indexed SQLite query); safe to call on every
        download-state transition."""
        try:
            has_any = bool(offline.list_downloads())
        except Exception:
            has_any = False
        self._clear_all_btn.setVisible(has_any)

    # ── Pause / Resume ──────────────────────────────────────────────────────

    def _on_pause_clicked(self) -> None:
        if offline.is_paused():
            offline.resume()
        else:
            offline.pause()

    def _on_download_all_clicked(self) -> None:
        """Confirm + kick off a full-library bulk download. The walk
        is provider-paginated and idempotent — already-complete albums
        skip on the manager side, so this is also the right action to
        run after adding a new album to the server."""
        from modules.async_io import run_async

        # Re-entry guard: ``confirm.exec()`` is modal but rapid
        # double-clicks reaching the slot before the dialog mounts can
        # still queue a second invocation. The flag closes that window.
        if getattr(self, "_walking_library", False):
            return
        self._walking_library = True

        confirm = QMessageBox(self)
        confirm.setWindowTitle("Download entire library")
        confirm.setText(
            "Download every album in your library to local storage?\n\n"
            "This walks your server and enqueues any albums that aren't "
            "already downloaded. You can pause or cancel at any time "
            "from this page."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            self._walking_library = False
            return

        self._download_all_btn.setEnabled(False)
        self._download_all_btn.setText("Walking library…")
        # Flag the queue as "this is a full-library walk" so the
        # pause/resume button gets the explicit label. Cleared on
        # the drain-edge stats emit (active == 0 && total_session
        # == 0). Persisted so the flag survives an app restart in
        # the middle of a paused bulk download.
        self._in_library_download = True
        get_settings().library_download_in_progress = True
        # Auto-resume if the queue was paused before — otherwise the
        # walk enqueues but ``_dispatch`` refuses to pop, leaving the
        # user staring at a "Resume library download" button right
        # after they confirmed the download. The user explicitly
        # opting into a bulk walk is implicit consent to drain.
        if offline.is_paused():
            offline.resume()
        self._refresh_pause_label()

        def _done(result):
            total, enqueued = result if isinstance(result, tuple) else (0, 0)
            self._walking_library = False
            self._download_all_btn.setEnabled(True)
            self._download_all_btn.setText("Download entire library")
            self._refresh_download_all_visibility()
            # Only nag with a popup when the walk produced nothing —
            # otherwise the aggregate block + drain notification
            # already tell the story.
            if enqueued == 0:
                note = QMessageBox(self)
                note.setWindowTitle("Library walk complete")
                note.setText(
                    f"All {total} albums in your library are already "
                    "downloaded — nothing new to enqueue."
                )
                note.exec()

        def _err(_exc):
            self._walking_library = False
            self._download_all_btn.setEnabled(True)
            self._download_all_btn.setText("Download entire library")
            self._refresh_download_all_visibility()
            err = QMessageBox(self)
            err.setWindowTitle("Library walk failed")
            err.setText(
                "Couldn't walk the library — the server may be "
                "unreachable. Check the connection and try again."
            )
            err.exec()

        run_async(offline.sync_library, on_result=_done, on_error=_err)

    def _refresh_download_all_visibility(self) -> None:
        """Hide the bulk-download button while the queue is active —
        the aggregate block + pause/resume button already cover the
        same surface during a download. Re-show it once the queue
        idles so a follow-up walk is one click away."""
        try:
            active, _t, _s, _e = offline.get_queue_stats()
        except Exception:
            active = 0
        walking = getattr(self, "_walking_library", False)
        self._download_all_btn.setVisible(not walking and active == 0)

    def _on_clear_all_clicked(self) -> None:
        """Destructive sweep — wipe every user-requested download.
        Always gated by a confirmation dialog; the operation can't be
        undone short of re-downloading."""
        from modules.async_io import run_async

        confirm = QMessageBox(self)
        confirm.setWindowTitle("Clear all downloads")
        confirm.setText(
            "Remove every downloaded album, playlist, artist, and track "
            "from this device?\n\n"
            "This frees up the storage immediately. Your library on the "
            "server isn't affected — you can re-download anything later."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        self._clear_all_btn.setEnabled(False)
        self._clear_all_btn.setText("Clearing…")

        def _done(_result):
            self._clear_all_btn.setEnabled(True)
            self._clear_all_btn.setText("Clear all downloads")
            self._refresh_storage()
            self._refresh_clear_all_visibility()

        def _err(_exc):
            self._clear_all_btn.setEnabled(True)
            self._clear_all_btn.setText("Clear all downloads")
            self._refresh_clear_all_visibility()

        run_async(offline.clear_all, on_result=_done, on_error=_err)

    def _on_library_sync_toggled(self, value: bool) -> None:
        setattr(get_settings(), "library_sync_enabled", value)
        if value:
            offline.start_periodic_library_sync()
        else:
            offline.stop_periodic_library_sync()

    def _refresh_pause_label(self) -> None:
        paused = offline.is_paused()
        # When the user kicked off a full-library walk this session,
        # the buttons name the operation explicitly so it's clear what
        # they're acting on — "Pause / Resume library download" rather
        # than the generic "Pause / Resume downloads".
        in_library_walk = getattr(self, "_in_library_download", False)
        if in_library_walk:
            self._pause_btn.setText(
                "Resume library download" if paused else "Pause library download"
            )
        else:
            self._pause_btn.setText(
                "Resume downloads" if paused else "Pause downloads"
            )
        self._refresh_pause_visibility(paused=paused)

    def _refresh_pause_visibility(self, *, paused: bool) -> None:
        """The pause/resume button is only relevant when there's
        something to pause (active > 0) or already paused state to
        recover from. At full idle the button hides so the page reads
        as plain settings."""
        try:
            active, _total, _speed, _eta = offline.get_queue_stats()
        except Exception:
            active = 0
        self._pause_btn.setVisible(paused or active > 0)

    def _on_queue_stats(
        self,
        active: int,
        total_session: int,
        _speed_bps: float,
        _eta_seconds: float,
    ) -> None:
        # Stats tick / drain edge: keep the pause + bulk-download
        # buttons in lockstep with whether there's anything to pause.
        self._pause_btn.setVisible(offline.is_paused() or active > 0)
        walking = getattr(self, "_walking_library", False)
        self._download_all_btn.setVisible(not walking and active == 0)
        # Drain-edge clears the in-library-walk flag so the next
        # ad-hoc enqueue gets the plain "Pause downloads" label.
        # The persisted expected-total counter clears too so a fresh
        # walk recomputes from scratch.
        if active == 0 and total_session == 0:
            if getattr(self, "_in_library_download", False):
                self._in_library_download = False
                settings = get_settings()
                settings.library_download_in_progress = False
                settings.library_download_expected_total = 0
                self._refresh_pause_label()

