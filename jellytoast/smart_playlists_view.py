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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_CAPTION,
    TYPE_TITLE,
    type_qss,
)
from jellytoast.player_state import PlayerBus
from jellytoast.settings import get_settings
from jellytoast.smart_playlist_editor import open_smart_playlist_editor
from jellytoast.ui_helpers import TEXT_DIM


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
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_SM)

        # Title + summary
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self._name_label = QLabel(str(entry.get("name") or "Untitled"))
        text_col.addWidget(self._name_label)
        self._summary_label = QLabel(_rule_summary(entry.get("rules") or {}))
        text_col.addWidget(self._summary_label)
        row.addLayout(text_col, 1)

        # Actions. Styled deliberately via button_qss tiers in
        # _apply_styling (not the global #accent / bare-QPushButton rules)
        # so each owns its label colour across every state — otherwise KDE
        # Breeze repaints bare-button labels from the palette in light
        # themes and they wash out to low-contrast grey.
        self.play_btn = QPushButton("Play")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.play_btn)

        self.edit_btn = QPushButton("Edit")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.delete_btn)

        self._apply_styling()

    def paintEvent(self, e):
        super().paintEvent(e)
        if getattr(self, "_kb_active", False):
            from jellytoast.keyboard_focus import paint_kb_row_ring

            paint_kb_row_ring(self)

    def _apply_styling(self) -> None:
        """Re-stamp baked QSS from the current theme tokens. Called at
        build and on theme_changed (via the view's _reapply_accent) — the
        per-surface re-stamp contract; see architecture_live_accent.md."""
        from jellytoast.design_tokens import (
            BTN_DESTRUCTIVE,
            BTN_PRIMARY,
            BTN_SECONDARY,
            button_qss,
        )
        from jellytoast.ui_helpers import TEXT, TEXT_DIM, ink_alpha

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
        self._name_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        self._summary_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        # Deliberate tiers — Play = primary, Edit = secondary, Delete =
        # destructive (matching the delete-confirm dialog's danger framing).
        # Each tier owns its label colour in every state, immune to the
        # Breeze light-theme palette hijack.
        self.play_btn.setStyleSheet(button_qss(BTN_PRIMARY))
        self.edit_btn.setStyleSheet(button_qss(BTN_SECONDARY))
        self.delete_btn.setStyleSheet(button_qss(BTN_DESTRUCTIVE))


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

        # Header — just the create action, right-aligned. No page title: the
        # top-bar "Smart playlists" nav label already names the surface, so a
        # second heading here is redundant.
        head = QHBoxLayout()
        head.setSpacing(SPACE_MD)
        head.addStretch(1)
        self._new_btn = QPushButton("+ New smart playlist")
        self._new_btn.setObjectName("accent")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._on_new)
        head.addWidget(self._new_btn)
        outer.addLayout(head)

        # Empty-state caption (toggled in reload). Expanding + AlignCenter so
        # it fills the area the rows would occupy and sits dead-centre instead
        # of stranded top-left.
        self._empty_caption = QLabel(
            "No smart playlists yet. Click \"+ New smart playlist\" to define one."
        )
        self._empty_caption.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._empty_caption.setWordWrap(True)
        self._empty_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_caption.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        outer.addWidget(self._empty_caption, 1)
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

        # Keyboard nav: Left/Right walks a row's Play/Edit/Delete, Up/Down
        # walks between rows; the focused row shows the accent ring.
        from jellytoast.keyboard_focus import install_row_grid_nav

        self._row_nav = install_row_grid_nav(
            self._ordered_rows,
            lambda r: [r.play_btn, r.edit_btn, r.delete_btn],
        )
        # Tab into this section lands on "+ New"; Down dives into the rows.
        self._new_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFocusProxy(self._new_btn)

        self.reload()

        # Live-accent re-stamp: rows + empty caption bake theme tokens into
        # per-widget QSS, and this view is nav-cached (rebuilds only on
        # reload()), so a dark<->light swap while it's open/cached must
        # re-stamp them. See architecture_live_accent.md.
        self.bus.theme_changed.connect(self._reapply_accent)

    def _ordered_rows(self):
        """Visible rows in layout order — for the keyboard row walker."""
        return [
            self._rows_layout.itemAt(i).widget()
            for i in range(self._rows_layout.count())
            if isinstance(self._rows_layout.itemAt(i).widget(), _SmartPlaylistRow)
        ]

    def focus_first_item(self):
        """Keyboard dive target — first row's Play, or '+ New' when empty."""
        rows = self._ordered_rows()
        if rows:
            rows[0].play_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._new_btn.setFocus(Qt.FocusReason.OtherFocusReason)

    # ── State ───────────────────────────────────────────────────────

    def _reapply_accent(self) -> None:
        from jellytoast.ui_helpers import TEXT_DIM

        self._empty_caption.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        for row in self._rows_container.findChildren(_SmartPlaylistRow):
            row._apply_styling()

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
            row.play_btn.clicked.connect(
                lambda _=False, e=entry, btn=row.play_btn: self._play(e, btn)
            )
            row.edit_btn.clicked.connect(lambda _=False, e=entry: self._edit(e))
            row.delete_btn.clicked.connect(lambda _=False, e=entry: self._delete(e))
            self._row_nav.wire(row)
            # Insert before the trailing stretch.
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    # ── Actions ─────────────────────────────────────────────────────

    @Slot()
    def _on_new(self) -> None:
        open_smart_playlist_editor(
            self,
            on_save=self._upsert,
            on_save_and_play=lambda entry, dismiss: self._upsert_and_play(
                entry, dismiss
            ),
        )

    def _edit(self, entry: Dict[str, Any]) -> None:
        # If the name changed, the new entry replaces the old by *position*
        # (name uniqueness isn't a hard contract — duplicate names are fine
        # so long as the user wants them).
        open_smart_playlist_editor(
            self,
            entry,
            on_save=lambda edited: self._upsert(edited, replace_entry=entry),
            on_save_and_play=lambda edited, dismiss: self._upsert_and_play(
                edited, dismiss, replace_entry=entry
            ),
        )

    def _upsert_and_play(
        self,
        entry: Dict[str, Any],
        dismiss,
        replace_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Save & Play handler — persist first (so the row appears in
        the list), then resolve the rules and start playback. The
        editor stays open in a Loading state until ``dismiss`` fires;
        pass it as ``play_entry``'s on_complete so the editor closes
        the moment playback starts."""
        from jellytoast.smart_playlists.play import play_entry

        self._upsert(entry, replace_entry=replace_entry)
        play_entry(entry, self, on_complete=dismiss)

    def _delete(self, entry: Dict[str, Any]) -> None:
        from jellytoast.frosted_dialog import frosted_confirm

        if not frosted_confirm(
            self,
            "Delete smart playlist?",
            f"Delete \"{entry.get('name')}\"? This can't be undone.",
            confirm_text="Delete",
            destructive=True,
        ):
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

    def _play(self, entry: Dict[str, Any], play_btn: QPushButton) -> None:
        """Resolve a smart playlist's rules and start playback.

        date-based rules (date_added / last_played) can't use a
        server-side filter — the evaluator pulls a recursive item set
        and filters client-side, which on a multi-thousand-track
        Jellyfin can take several seconds. Without a visible loading
        affordance on the row the user clicked, the wait reads as a
        hang. The button is disabled + relabelled to "Loading…" for
        the duration of the resolve and restored when the shared
        ``play_entry`` flow completes (success, empty match, OR
        provider error — it fires ``on_complete`` on every terminal
        path). Other rows stay clickable so the user can fire
        multiple loads; the last to resolve wins (acceptable race
        for now).

        The resolve→queue→navigate path itself lives in
        ``smart_playlists.play_entry`` so this row Play button and the
        editor's Save & Play can't drift; only the per-row loading
        affordance stays here."""
        from jellytoast.smart_playlists.play import play_entry

        play_btn.setEnabled(False)
        play_btn.setText("Loading…")

        def _restore() -> None:
            play_btn.setEnabled(True)
            play_btn.setText("Play")

        play_entry(entry, self, on_complete=_restore)
