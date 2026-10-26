"""Settings dialog — the Hotkeys page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    rad,
    type_qss,
)
from jellytoast.ui_helpers import (
    ERROR_FG,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)


class _HotkeysPageMixin:
    # ── Page: Hotkeys ──────────────────────────────────────────────────
    # Every rebindable keyboard shortcut, one live QKeySequenceEdit per
    # row with a per-row Reset and a conflict warning. The bindings
    # themselves are wired in JellytoastWindow.__init__ via
    # jellytoast.hotkeys — Ctrl+F, /, Ctrl+Shift+L, etc.
    def _build_hotkeys(self) -> QWidget:
        from jellytoast import hotkeys as _hotkeys

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # action_id -> (QKeySequenceEdit, warning QLabel, default_seq)
        self._hotkey_edits: dict = {}
        # action_id -> human label, for the conflict message.
        self._hotkey_labels = {e["action_id"]: e["label"] for e in _hotkeys.registry_actions()}

        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "In-app shortcuts"))
        )
        for entry in _hotkeys.registry_actions():
            v.addWidget(self._editable_hotkey_row(entry))

        reset_all = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Reset all to defaults")
        )
        reset_all.setObjectName("ghost")
        reset_all.clicked.connect(self._on_hotkeys_reset_all)
        v.addWidget(reset_all, 0, Qt.AlignmentFlag.AlignLeft)

        v.addSpacing(8)
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Media keys"))
        )
        v.addLayout(
            self._hotkey_row(
                QCoreApplication.translate("SettingsDialog", "Play / Pause"),
                QCoreApplication.translate("SettingsDialog", "Media Play"),
            )
        )
        v.addLayout(
            self._hotkey_row(
                QCoreApplication.translate("SettingsDialog", "Next track"),
                QCoreApplication.translate("SettingsDialog", "Media Next"),
            )
        )
        v.addLayout(
            self._hotkey_row(
                QCoreApplication.translate("SettingsDialog", "Previous track"),
                QCoreApplication.translate("SettingsDialog", "Media Previous"),
            )
        )
        note = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                "Media keys route through MPRIS and your desktop's own "
                "shortcut settings — they can't be rebound here.",
            )
        )
        note.setWordWrap(True)
        # Wrap to the page's content width (no maxWidth): capping the render
        # width while the layout sizes the label's HEIGHT for the wider column
        # under-allocated it, so the extra wrapped lines overflowed into the row
        # above. The narrower dialog keeps this from running awkwardly wide.
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addSpacing(6)
        v.addWidget(note)
        v.addStretch(1)
        return page

    def _editable_hotkey_row(self, entry: dict) -> QWidget:
        """One rebindable shortcut — label + a QKeySequenceEdit capture
        field + a per-row Reset, with a hidden inline conflict warning
        below the row."""
        from jellytoast import hotkeys as _hotkeys

        action_id = entry["action_id"]
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        row = QHBoxLayout()
        # Trailing right margin pulls the QKeySequenceEdit + Reset
        # button leftward so they don't sit hard against the page's
        # right edge — leaves the right column of the page reading
        # as a column rather than a strip of edge-aligned widgets.
        from jellytoast.platform_compat import is_mobile_shell

        # Narrow Settings (a phone, or opened from a narrow window) needs
        # the room, so no trailing indent there.
        narrow = getattr(self, "_compact", False) or is_mobile_shell()
        row.setContentsMargins(0, 0, 0 if narrow else 140, 0)
        row.setSpacing(8)
        name = QLabel(entry["label"])
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        # Wraps rather than forcing the row past a narrow page.
        name.setWordWrap(True)
        row.addWidget(name, 1)

        edit = QKeySequenceEdit()
        try:
            # One chord per action — no multi-step sequences.
            edit.setMaximumSequenceLength(1)
        except (AttributeError, TypeError):
            pass  # Qt < 6.5 — multi-chord accepted, harmless
        edit.setKeySequence(_hotkeys.get_sequence(action_id, entry["default_seq"]))
        edit.setMaximumWidth(150 if narrow else 170)
        edit.editingFinished.connect(lambda aid=action_id: self._on_hotkey_edited(aid))
        row.addWidget(edit)

        reset_btn = QPushButton(QCoreApplication.translate("SettingsDialog", "Reset"))
        reset_btn.setObjectName("ghost")
        reset_btn.clicked.connect(lambda _=False, aid=action_id: self._on_hotkey_reset(aid))
        row.addWidget(reset_btn)
        col.addLayout(row)

        warn = QLabel("")
        warn.setStyleSheet(f"color: {ERROR_FG}; {type_qss(TYPE_CAPTION)}")
        warn.setWordWrap(True)
        warn.setVisible(False)
        col.addWidget(warn)

        self._hotkey_edits[action_id] = (edit, warn, entry["default_seq"])
        return wrap

    def _hotkey_snapshot(self) -> dict:
        """Live page state — every row's *current* sequence, including
        edits not yet persisted — so the conflict check sees what the
        user is about to have, not just the saved set."""
        return {
            aid: edit.keySequence() for aid, (edit, _warn, _default) in self._hotkey_edits.items()
        }

    def _on_hotkey_edited(self, action_id: str):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        seq = edit.keySequence()
        if seq.isEmpty():
            # Cleared the field — treat as "reset to default".
            self._on_hotkey_reset(action_id)
            return
        conflict = _hotkeys.find_conflict(action_id, seq, bindings=self._hotkey_snapshot())
        if conflict:
            other = self._hotkey_labels.get(conflict, conflict)
            warn.setText(
                QCoreApplication.translate(
                    "SettingsDialog", "Already used by “{0}” — pick another."
                ).format(other)
            )
            warn.setVisible(True)
            # Snap the field back to the persisted binding.
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
            edit.blockSignals(False)
            return
        warn.setVisible(False)
        _hotkeys.set_sequence(action_id, seq)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkey_reset(self, action_id: str):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        _hotkeys.reset(action_id)
        edit.blockSignals(True)
        edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
        edit.blockSignals(False)
        warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkeys_reset_all(self):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        _hotkeys.reset_all()
        for aid, (edit, warn, default_seq) in self._hotkey_edits.items():
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(aid, default_seq))
            edit.blockSignals(False)
            warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _hotkey_row(self, label: str, keys: str) -> QHBoxLayout:
        row = QHBoxLayout()
        # Trailing right margin matches `_editable_hotkey_row` so the
        # binding chips line up under the Reset buttons above rather
        # than drifting all the way to the page's right edge.
        from jellytoast.platform_compat import is_mobile_shell

        # Narrow Settings (a phone, or opened from a narrow window) needs
        # the room, so no trailing indent there.
        narrow = getattr(self, "_compact", False) or is_mobile_shell()
        row.setContentsMargins(0, 0, 0 if narrow else 140, 0)
        name = QLabel(label)
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)
        binding = QLabel(keys)
        binding.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"background: {ink_alpha(0.06)}; padding: 3px 9px; "
            f"border-radius: {rad(5)}px;"
        )
        row.addWidget(binding)
        return row
