"""Smart playlists library surface.

A standalone page (mirrors ``RadioView`` and ``DownloadsLibraryView``)
that lists every smart playlist the user has saved via
``settings.smart_playlists``. Each row offers Play, Edit, and Delete.
Clicking a row plays the playlist as a regular ``PLAYLIST`` queue —
the rules resolve through ``get_provider().query_items`` and the
result lands in the queue manager exactly like a server-side playlist
would.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modules import async_io
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_CAPTION,
    TYPE_TITLE,
    type_qss,
)
from modules.player_state import PlayerBus, QueueContext, QueueKind
from modules.settings import get_settings
from modules.smart_playlist_editor import open_smart_playlist_editor
from modules.ui_helpers import TEXT, TEXT_DIM, ink_alpha


def _rule_summary(rules: Dict[str, Any]) -> str:
    """One-line human summary of a rule set for the row subtitle."""
    out: List[str] = []
    match = rules.get("match", "all")
    n = len(rules.get("rules") or [])
    out.append(f"{n} rule{'s' if n != 1 else ''} ({'AND' if match == 'all' else 'OR'})")
    limit = rules.get("limit")
    if isinstance(limit, int) and limit > 0:
        out.append(f"limit {limit}")
    sort = rules.get("sort")
    if sort:
        out.append(f"sort {sort}" + (" desc" if rules.get("sort_desc") else ""))
    return "  ·  ".join(out)


class _SmartPlaylistRow(QFrame):
    """One row — name + summary + Play / Edit / Delete buttons."""

    def __init__(
        self,
        entry: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("smartPlaylistRow")
        self.setStyleSheet(
            f"""
            QFrame#smartPlaylistRow {{
                background: {ink_alpha(0.03)};
                border: 1px solid {ink_alpha(0.06)};
                border-radius: 8px;
            }}
            QFrame#smartPlaylistRow:hover {{
                background: {ink_alpha(0.06)};
            }}
            """
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_SM)

        # Title + summary
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name = QLabel(str(entry.get("name") or "Untitled"))
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        text_col.addWidget(name)
        summary = QLabel(_rule_summary(entry.get("rules") or {}))
        summary.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        text_col.addWidget(summary)
        row.addLayout(text_col, 1)

        # Actions
        self.play_btn = QPushButton("Play")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setObjectName("accent")
        row.addWidget(self.play_btn)

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.delete_btn)


class SmartPlaylistsView(QWidget):
    """List of saved smart playlists, with create/edit/delete + a Play
    action that resolves rules → tracks → PLAYLIST queue."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.bus = PlayerBus.get()

        # Blend into the translucent / blurred window body. Without
        # this the view, its scroll area and every QLabel inherit
        # GLOBAL_STYLE's opaque `QWidget { background: BG }` rule and
        # paint a solid panel over the body.
        self.setObjectName("smartPlaylistsView")
        self.setStyleSheet(
            "QWidget#smartPlaylistsView, "
            "QWidget#smartPlaylistsView QLabel { background: transparent; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        # Header
        head = QHBoxLayout()
        head.setSpacing(SPACE_MD)
        title = QLabel("Smart playlists")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)} font-weight: 700;")
        head.addWidget(title)
        head.addStretch(1)
        self._new_btn = QPushButton("+ New smart playlist")
        self._new_btn.setObjectName("accent")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._on_new)
        head.addWidget(self._new_btn)
        outer.addLayout(head)

        # Empty-state caption (toggled in reload)
        self._empty_caption = QLabel(
            "No smart playlists yet. Click \"+ New smart playlist\" to define one."
        )
        self._empty_caption.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._empty_caption.setWordWrap(True)
        outer.addWidget(self._empty_caption)
        self._empty_caption.hide()

        # Scrollable rows
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(SPACE_SM)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_container)
        outer.addWidget(self._scroll, 1)

        self.reload()

    # ── State ───────────────────────────────────────────────────────

    def reload(self) -> None:
        # Clear existing rows (except the trailing stretch).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        entries = list(get_settings().smart_playlists)
        if not entries:
            self._empty_caption.show()
            self._scroll.hide()
            return
        self._empty_caption.hide()
        self._scroll.show()
        for entry in entries:
            row = _SmartPlaylistRow(entry, self._rows_container)
            row.play_btn.clicked.connect(lambda _=False, e=entry: self._play(e))
            row.edit_btn.clicked.connect(lambda _=False, e=entry: self._edit(e))
            row.delete_btn.clicked.connect(lambda _=False, e=entry: self._delete(e))
            # Insert before the trailing stretch.
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    # ── Actions ─────────────────────────────────────────────────────

    @Slot()
    def _on_new(self) -> None:
        entry = open_smart_playlist_editor(self)
        if entry is None:
            return
        self._upsert(entry)

    def _edit(self, entry: Dict[str, Any]) -> None:
        edited = open_smart_playlist_editor(self, entry)
        if edited is None:
            return
        # If the name changed, the new entry replaces the old by *position*
        # (name uniqueness isn't a hard contract — duplicate names are fine
        # so long as the user wants them).
        self._upsert(edited, replace_entry=entry)

    def _delete(self, entry: Dict[str, Any]) -> None:
        confirm = QMessageBox.question(
            self,
            "Delete smart playlist?",
            f"Delete \"{entry.get('name')}\"? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        entries = list(get_settings().smart_playlists)
        entries = [
            e
            for e in entries
            if not (
                e.get("name") == entry.get("name")
                and e.get("created_at") == entry.get("created_at")
            )
        ]
        get_settings().smart_playlists = entries
        self.reload()

    def _upsert(
        self,
        entry: Dict[str, Any],
        replace_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        entries = list(get_settings().smart_playlists)
        if replace_entry is not None:
            entries = [
                e
                for e in entries
                if not (
                    e.get("name") == replace_entry.get("name")
                    and e.get("created_at") == replace_entry.get("created_at")
                )
            ]
        entries.append(entry)
        get_settings().smart_playlists = entries
        self.reload()

    def _play(self, entry: Dict[str, Any]) -> None:
        rules = entry.get("rules") or {}
        name = entry.get("name") or "Smart playlist"

        def _go() -> List[Dict[str, Any]]:
            from modules.providers import get_provider

            try:
                return list(get_provider().query_items(rules))
            except Exception:
                return []

        async_io.run_async(
            _go,
            on_result=lambda items: self._on_resolved(items, name),
            on_error=lambda _e: QMessageBox.warning(
                self, "Couldn't load playlist", "The provider call failed."
            ),
        )

    def _on_resolved(self, items: List[Dict[str, Any]], name: str) -> None:
        if not items:
            QMessageBox.information(
                self, "Empty playlist", f"\"{name}\" matched no tracks right now."
            )
            return
        ctx = QueueContext(
            kind=QueueKind.PLAYLIST,
            source_id="",  # client-side: no server playlist ID
            source_label=name,
        )
        self.bus.queue_play_now.emit(items, 0, ctx)
