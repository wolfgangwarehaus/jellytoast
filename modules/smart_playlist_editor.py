"""Smart-playlist editor dialog.

A focused QDialog that lets the user assemble a rule set from the
schema in :mod:`modules.providers.smart_rule_schema`. Each rule is a
chip with field/op/value pickers; matches a single-flat-list-of-rules
shape with a top-level ``match: all|any``. A live-preview pane on the
right runs ``get_provider().query_items(...)`` in a background worker
after each change and shows the first slice of matches so users can
verify rules before saving.

Presets (defined in :mod:`modules.smart_playlists.presets`) load as a
starter rule set the user can then tweak. Saving normalises the dict
shape and pushes onto ``settings.smart_playlists`` via the
:class:`SmartPlaylistsStore` helper.

Wired into the library "Smart playlists" section by
:func:`open_smart_playlist_editor` — the public entrypoint that
returns the new/edited entry (or ``None`` on cancel).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from modules import async_io
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_CAPTION,
    TYPE_TINY,
    type_qss,
)
from modules.providers.smart_rule_schema import FIELDS, VALID_MATCH, validate_rules
from modules.smart_playlists.presets import PRESETS, YEAR_PRESET_NAME, make_year_preset
from modules.ui_helpers import TEXT, TEXT_DIM


# Public field labels (mirror schema ordering so the catalogue stays
# the single source of truth — adding a field to ``FIELDS`` is enough
# to surface it here too).
_FIELD_LABELS: Dict[str, str] = {
    "genre": "Genre",
    "artist": "Artist",
    "album": "Album",
    "year": "Year",
    "play_count": "Play count",
    "rating": "Rating",
}

# Friendly operator labels — kept identical to the schema op keys for
# data shape, but rendered nicer in the combo.
_OP_LABELS: Dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "contains": "contains",
    "greater_than": "greater than",
    "less_than": "less than",
    "between": "between",
}

# Sort options. Mostly mirrors schema fields; a few human-friendly
# extras are deliberately omitted so the editor stays a thin shell
# over the schema.
_SORT_OPTIONS: List[str] = ["", "artist", "album", "year", "play_count", "rating"]

# Preview hard cap — keeps the round-trip fast and the dialog
# responsive even on large libraries.
_PREVIEW_LIMIT = 25
# Debounce window before firing a preview refresh — every chip change
# bounces the timer so rapid typing doesn't spam the server.
_PREVIEW_DEBOUNCE_MS = 350


def _label(text: str, *, dim: bool = True) -> QLabel:
    lab = QLabel(text)
    color = TEXT_DIM if dim else TEXT
    lab.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {color};")
    return lab


def _between_payload(value: Any) -> Optional[List[int]]:
    """Normalise a ``between`` value into a 2-int list. Accepts list-
    of-two-ints or ``"a-b"``-style strings. Returns None if it can't
    parse."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [int(value[0]), int(value[1])]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        parts = value.replace("..", "-").split("-", 1)
        if len(parts) == 2:
            try:
                return [int(parts[0]), int(parts[1])]
            except (TypeError, ValueError):
                return None
    return None


class _RuleChip(QFrame):
    """One rule row: field / op / value(s) / remove. Emits ``changed``
    on any internal change so the dialog can refresh the preview."""

    changed = Signal()
    removed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, rule: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setObjectName("ruleChip")
        self.setStyleSheet(
            """
            QFrame#ruleChip {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 6px;
            }
            """
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 4, SPACE_SM, 4)
        row.setSpacing(SPACE_SM)

        self._field = QComboBox()
        for k in FIELDS:
            self._field.addItem(_FIELD_LABELS.get(k, k), k)
        row.addWidget(self._field)

        self._op = QComboBox()
        row.addWidget(self._op)

        # Two value widgets, only one is visible at a time depending on
        # operator (single value vs between's "low – high" pair).
        self._value = QLineEdit()
        self._value.setPlaceholderText("value")
        row.addWidget(self._value, 1)

        self._value_low = QSpinBox()
        self._value_low.setRange(0, 9999)
        self._value_low.hide()
        row.addWidget(self._value_low)

        self._between_dash = QLabel("–")
        self._between_dash.setStyleSheet(f"color: {TEXT_DIM};")
        self._between_dash.hide()
        row.addWidget(self._between_dash)

        self._value_high = QSpinBox()
        self._value_high.setRange(0, 9999)
        self._value_high.hide()
        row.addWidget(self._value_high)

        remove = QPushButton("×")
        remove.setFixedSize(22, 22)
        remove.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent; border: none; color: {TEXT_DIM};
                {type_qss(TYPE_TINY)} font-weight: 700;
            }}
            QPushButton:hover {{ color: {TEXT}; }}
            """
        )
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.clicked.connect(self.removed.emit)
        row.addWidget(remove)

        # Wire changes — field change cascades to op list, op change
        # may swap the value widget shape, value changes emit changed.
        self._field.currentIndexChanged.connect(self._on_field_changed)
        self._op.currentIndexChanged.connect(self._on_op_changed)
        # Source signals carry the new value (str / int); strip it so
        # the no-arg ``changed`` signal can fan out cleanly.
        self._value.textChanged.connect(lambda _t: self.changed.emit())
        self._value_low.valueChanged.connect(lambda _v: self.changed.emit())
        self._value_high.valueChanged.connect(lambda _v: self.changed.emit())

        if rule:
            self.set_value(rule)
        else:
            self._refresh_ops()

    def _on_field_changed(self) -> None:
        self._refresh_ops()
        self.changed.emit()

    def _on_op_changed(self) -> None:
        self._refresh_value_widgets()
        self.changed.emit()

    def _refresh_ops(self) -> None:
        field = self._field.currentData()
        ops = FIELDS.get(field, {}).get("ops", [])
        current = self._op.currentData()
        self._op.blockSignals(True)
        self._op.clear()
        for op in ops:
            self._op.addItem(_OP_LABELS.get(op, op), op)
        if current in ops:
            self._op.setCurrentIndex(ops.index(current))
        self._op.blockSignals(False)
        self._refresh_value_widgets()

    def _refresh_value_widgets(self) -> None:
        op = self._op.currentData()
        field = self._field.currentData()
        ftype = FIELDS.get(field, {}).get("type", str)
        is_between = op == "between"
        self._value.setVisible(not is_between)
        self._value_low.setVisible(is_between)
        self._between_dash.setVisible(is_between)
        self._value_high.setVisible(is_between)
        if not is_between:
            self._value.setPlaceholderText("number" if ftype is int else "text")

    def value(self) -> Dict[str, Any]:
        field = self._field.currentData()
        op = self._op.currentData()
        ftype = FIELDS.get(field, {}).get("type", str)
        if op == "between":
            value: Any = [self._value_low.value(), self._value_high.value()]
        else:
            raw = self._value.text().strip()
            if ftype is int:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    value = 0
            else:
                value = raw
        return {"field": field, "op": op, "value": value}

    def set_value(self, rule: Dict[str, Any]) -> None:
        field = rule.get("field")
        op = rule.get("op")
        if field in FIELDS:
            idx = list(FIELDS).index(field)
            self._field.setCurrentIndex(idx)
        self._refresh_ops()
        if op:
            ops = FIELDS.get(field, {}).get("ops", [])
            if op in ops:
                self._op.setCurrentIndex(ops.index(op))
        self._refresh_value_widgets()
        v = rule.get("value")
        if op == "between":
            pair = _between_payload(v) or [0, 0]
            self._value_low.setValue(pair[0])
            self._value_high.setValue(pair[1])
        else:
            self._value.setText("" if v is None else str(v))


class SmartPlaylistEditorDialog(QDialog):
    """Modal editor for a single smart playlist."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        entry: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(parent)
        self._original = entry
        self.setWindowTitle("Edit smart playlist" if entry else "New smart playlist")
        self.setModal(True)
        self.setMinimumSize(720, 480)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_LG)

        # ── Left column: form ────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(SPACE_SM)
        outer.addLayout(left, 3)

        left.addWidget(_label("Name", dim=False))
        self._name = QLineEdit(str((entry or {}).get("name") or ""))
        self._name.setPlaceholderText("e.g. Recent favorites")
        left.addWidget(self._name)

        # Preset picker — loads a starter rule set. Saved as "Custom"
        # once the user touches anything.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(SPACE_SM)
        preset_row.addWidget(_label("Preset"))
        self._preset = QComboBox()
        self._preset.addItem("Custom", "")
        for name, _description, _friendly, _rules in PRESETS:
            self._preset.addItem(name, name)
        self._preset.currentIndexChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self._preset, 1)
        left.addLayout(preset_row)

        # Match-mode + sort row
        match_row = QHBoxLayout()
        match_row.setSpacing(SPACE_SM)
        match_row.addWidget(_label("Match"))
        self._match = QComboBox()
        self._match.addItem("all rules (AND)", "all")
        self._match.addItem("any rule (OR)", "any")
        self._match.currentIndexChanged.connect(lambda _i: self._queue_preview())
        match_row.addWidget(self._match)
        match_row.addSpacing(SPACE_MD)
        match_row.addWidget(_label("Sort"))
        self._sort = QComboBox()
        for s in _SORT_OPTIONS:
            self._sort.addItem(s or "default", s)
        self._sort.currentIndexChanged.connect(lambda _i: self._queue_preview())
        match_row.addWidget(self._sort)
        self._sort_desc = QCheckBox("descending")
        self._sort_desc.stateChanged.connect(lambda _s: self._queue_preview())
        match_row.addWidget(self._sort_desc)
        match_row.addStretch(1)
        left.addLayout(match_row)

        # Limit
        limit_row = QHBoxLayout()
        limit_row.setSpacing(SPACE_SM)
        limit_row.addWidget(_label("Limit"))
        self._limit = QSpinBox()
        self._limit.setRange(0, 5000)
        self._limit.setSpecialValueText("no limit")
        self._limit.setValue(0)
        self._limit.valueChanged.connect(lambda _v: self._queue_preview())
        limit_row.addWidget(self._limit)
        limit_row.addStretch(1)
        left.addLayout(limit_row)

        # Rules
        left.addWidget(_label("Rules", dim=False))
        self._rules_container = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(SPACE_SM)
        self._rules_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._rules_container)
        left.addWidget(scroll, 1)

        self._chips: List[_RuleChip] = []
        add_btn = QPushButton("+ Add rule")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(lambda: self._add_chip(None))
        left.addWidget(add_btn)

        # ── Right column: live preview ───────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(SPACE_SM)
        outer.addLayout(right, 2)

        right.addWidget(_label("Preview", dim=False))
        self._preview_status = _label("")
        right.addWidget(self._preview_status)
        self._preview_list = QListWidget()
        self._preview_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        right.addWidget(self._preview_list, 1)

        # Footer: Save / Cancel
        footer = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        footer.accepted.connect(self._on_accept)
        footer.rejected.connect(self.reject)
        outer.addWidget(footer, 0, Qt.AlignmentFlag.AlignBottom)

        # Preview debounce
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._refresh_preview)

        # Seed from existing entry, else leave empty.
        if entry and isinstance(entry.get("rules"), dict):
            self._apply_rules(entry["rules"])
        # Fire an initial preview after the dialog lays out.
        QTimer.singleShot(0, self._refresh_preview)

    # ── Preset / rules state ────────────────────────────────────────

    def _on_preset_changed(self) -> None:
        name = self._preset.currentData()
        if not name:
            return
        if name == YEAR_PRESET_NAME:
            rules = make_year_preset(datetime.now().year)
        else:
            from modules.smart_playlists.presets import get_preset

            rules = get_preset(name) or {}
        self._apply_rules(rules)
        # Don't lock the user into the preset — they can tweak from here.

    def _apply_rules(self, rules: Dict[str, Any]) -> None:
        # Reset chips
        for chip in self._chips:
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []
        for r in rules.get("rules") or []:
            self._add_chip(r)
        match = rules.get("match", "all")
        if match in VALID_MATCH:
            self._match.setCurrentIndex(0 if match == "all" else 1)
        sort = rules.get("sort") or ""
        if sort in _SORT_OPTIONS:
            self._sort.setCurrentIndex(_SORT_OPTIONS.index(sort))
        self._sort_desc.setChecked(bool(rules.get("sort_desc")))
        limit = rules.get("limit")
        self._limit.setValue(int(limit) if isinstance(limit, int) and limit > 0 else 0)
        self._queue_preview()

    def _add_chip(self, rule: Optional[Dict[str, Any]]) -> None:
        chip = _RuleChip(self._rules_container, rule)
        chip.changed.connect(self._queue_preview)
        chip.removed.connect(lambda c=chip: self._remove_chip(c))
        # Insert before the trailing stretch so chips stack top-down.
        self._rules_layout.insertWidget(self._rules_layout.count() - 1, chip)
        self._chips.append(chip)
        self._queue_preview()

    def _remove_chip(self, chip: _RuleChip) -> None:
        if chip in self._chips:
            self._chips.remove(chip)
        chip.setParent(None)
        chip.deleteLater()
        self._queue_preview()

    # ── Preview ─────────────────────────────────────────────────────

    def _queue_preview(self) -> None:
        self._preview_timer.start()

    @Slot()
    def _refresh_preview(self) -> None:
        rules = self.rules_dict()
        # Clamp preview-side limit so the dialog never paints thousands.
        preview_rules = dict(rules)
        cap = rules.get("limit")
        preview_rules["limit"] = min(_PREVIEW_LIMIT, cap) if isinstance(cap, int) and cap > 0 else _PREVIEW_LIMIT

        errors = validate_rules(preview_rules)
        if errors:
            self._preview_status.setText(f"⚠ {errors[0]}")
            self._preview_list.clear()
            return
        self._preview_status.setText("…matching")
        self._preview_list.clear()

        def _go() -> List[Dict[str, Any]]:
            from modules.providers import get_provider

            try:
                return list(get_provider().query_items(preview_rules))
            except Exception:
                return []

        async_io.run_async(
            _go,
            on_result=self._on_preview_result,
            on_error=lambda _e: self._preview_status.setText("preview unavailable"),
        )

    def _on_preview_result(self, items: List[Dict[str, Any]]) -> None:
        self._preview_list.clear()
        if not items:
            self._preview_status.setText("0 matches")
            return
        for it in items:
            title = it.get("Name") or it.get("Title") or "(untitled)"
            artist = (
                it.get("Artist")
                or (it.get("Artists") or [None])[0]
                or it.get("ArtistName")
                or ""
            )
            text = f"{title}  ·  {artist}" if artist else str(title)
            self._preview_list.addItem(QListWidgetItem(text))
        cap = self.rules_dict().get("limit")
        tail = f" (capped at {_PREVIEW_LIMIT})" if len(items) >= _PREVIEW_LIMIT else ""
        cap_note = f", saved limit {cap}" if isinstance(cap, int) and cap > 0 else ""
        self._preview_status.setText(f"{len(items)} match{'es' if len(items) != 1 else ''}{tail}{cap_note}")

    # ── Save ────────────────────────────────────────────────────────

    def rules_dict(self) -> Dict[str, Any]:
        rules = [c.value() for c in self._chips]
        out: Dict[str, Any] = {
            "match": self._match.currentData() or "all",
            "rules": rules,
            "sort": self._sort.currentData() or None,
            "sort_desc": bool(self._sort_desc.isChecked()),
        }
        limit = int(self._limit.value())
        out["limit"] = limit if limit > 0 else None
        return out

    def _on_accept(self) -> None:
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Give the playlist a name.")
            self._name.setFocus()
            return
        rules = self.rules_dict()
        errors = validate_rules(rules)
        if errors:
            QMessageBox.warning(self, "Invalid rules", "\n".join(errors[:5]))
            return
        self.accept()

    def values(self) -> Dict[str, Any]:
        """Final entry dict — caller persists via ``settings.smart_playlists``."""
        return {
            "name": self._name.text().strip(),
            "rules": self.rules_dict(),
            "created_at": (self._original or {}).get(
                "created_at"
            ) or datetime.now().isoformat(timespec="seconds"),
        }


def open_smart_playlist_editor(
    parent: Optional[QWidget] = None,
    entry: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Show the editor dialog; return the saved entry, or None on cancel."""
    dlg = SmartPlaylistEditorDialog(parent, entry)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.values()
