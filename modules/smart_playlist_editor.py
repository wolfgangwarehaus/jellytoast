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

from PySide6.QtCore import QDate, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
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
    RADIUS_WINDOW,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_CAPTION,
    TYPE_SUBHEAD,
    TYPE_TINY,
    type_qss,
)
from modules.icons import icon
from modules.providers.smart_rule_schema import FIELDS, VALID_MATCH, validate_rules
from modules.selector import Selector, selector_qss
from modules.smart_playlists.presets import PRESETS, YEAR_PRESET_NAME, make_year_preset

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
    "is_favorite": "Favorite",
    "date_added": "Date added",
    "last_played": "Last played",
}

# Friendly operator labels — kept identical to the schema op keys for
# data shape, but rendered nicer in the combo.
_OP_LABELS: Dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "contains": "contains",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "greater_than": "greater than",
    "less_than": "less than",
    "between": "between",
    "in_the_last": "in the last",
    "before": "before",
    "after": "after",
}

# Sort options. Mostly mirrors schema fields; ``random`` is the one
# special-token sort (see ``smart_rule_schema.SPECIAL_SORTS``).
_SORT_OPTIONS: List[str] = ["", "artist", "album", "year", "play_count", "rating", "random"]

# Friendly combo labels for the sort tokens. The combo stores the raw
# token as item data, so load/dump round-tripping is unaffected — this
# only changes what the user reads. ``random`` reads as "Shuffle"
# because that's what users go looking for.
_SORT_LABELS: Dict[str, str] = {
    "": "Default",
    "artist": "Artist",
    "album": "Album",
    "year": "Year",
    "play_count": "Play count",
    "rating": "Rating",
    "random": "Shuffle",
}

# Preview hard cap — keeps the round-trip fast and the dialog
# responsive even on large libraries.
_PREVIEW_LIMIT = 25
# Debounce window before firing a preview refresh — every chip change
# bounces the timer so rapid typing doesn't spam the server.
_PREVIEW_DEBOUNCE_MS = 350


def _label(text: str, *, dim: bool = True) -> QLabel:
    # Late-import so a settings-driven theme/accent swap that landed
    # since module import takes effect on the next dialog opening —
    # `from modules.ui_helpers import TEXT_DIM` at module top freezes
    # the binding to the load-time value. Per architecture_live_accent.
    from modules.ui_helpers import TEXT as _TEXT
    from modules.ui_helpers import TEXT_DIM as _TEXT_DIM

    lab = QLabel(text)
    color = _TEXT_DIM if dim else _TEXT
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
        from modules.ui_helpers import ink_alpha as _ink_alpha

        self.setStyleSheet(
            f"""
            QFrame#ruleChip {{
                background: {_ink_alpha(0.04)};
                border: 1px solid {_ink_alpha(0.06)};
                border-radius: 6px;
            }}
            """
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 4, SPACE_SM, 4)
        row.setSpacing(SPACE_SM)

        # Minimum widths so the friendly labels ("Date added",
        # "greater than", …) never render clipped when the dialog is
        # squeezed narrow. Selector grows naturally with longer
        # labels so AdjustToContents isn't needed.
        self._field = Selector()
        self._field.setMinimumWidth(120)
        for k in FIELDS:
            self._field.addItem(_FIELD_LABELS.get(k, k), k)
        row.addWidget(self._field)

        self._op = Selector()
        self._op.setMinimumWidth(110)
        row.addWidget(self._op)

        # Value widgets — only one is visible at a time, picked by the
        # field type + operator: free text, between's "low – high" pair,
        # a bool checkbox, a day-count (in_the_last), or a date picker
        # (before / after).
        self._value = QLineEdit()
        self._value.setPlaceholderText("value")
        row.addWidget(self._value, 1)

        self._value_low = QSpinBox()
        self._value_low.setRange(0, 9999)
        self._value_low.hide()
        row.addWidget(self._value_low)

        self._between_dash = QLabel("–")
        from modules.ui_helpers import TEXT_DIM as _TEXT_DIM_RC

        self._between_dash.setStyleSheet(f"color: {_TEXT_DIM_RC};")
        self._between_dash.hide()
        row.addWidget(self._between_dash)

        self._value_high = QSpinBox()
        self._value_high.setRange(0, 9999)
        self._value_high.hide()
        row.addWidget(self._value_high)

        # `in_the_last` day count — the " days" suffix makes the unit
        # unambiguous (a bare "30" read as anything).
        self._value_days = QSpinBox()
        self._value_days.setRange(1, 3650)
        self._value_days.setValue(30)
        self._value_days.setSuffix(" days")
        self._value_days.hide()
        row.addWidget(self._value_days)

        # `before` / `after` calendar date.
        self._value_date = QDateEdit()
        self._value_date.setCalendarPopup(True)
        self._value_date.setDisplayFormat("yyyy-MM-dd")
        self._value_date.setDate(QDate.currentDate())
        self._value_date.hide()
        row.addWidget(self._value_date)

        # Bool-field value widget (used by ``is_favorite``). Only one of
        # _value / spin pair / _value_bool is visible at a time.
        self._value_bool = QCheckBox("yes")
        self._value_bool.hide()
        row.addWidget(self._value_bool)

        remove = QPushButton("×")
        remove.setFixedSize(22, 22)
        from modules.ui_helpers import TEXT as _TEXT_RM
        from modules.ui_helpers import TEXT_DIM as _TEXT_DIM_RM

        remove.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent; border: none; color: {_TEXT_DIM_RM};
                {type_qss(TYPE_TINY)} font-weight: 700;
            }}
            QPushButton:hover {{ color: {_TEXT_RM}; }}
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
        self._value_bool.stateChanged.connect(lambda _s: self.changed.emit())
        self._value_days.valueChanged.connect(lambda _v: self.changed.emit())
        self._value_date.dateChanged.connect(lambda _d: self.changed.emit())

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
        is_bool = ftype is bool
        is_days = op == "in_the_last"
        is_date = op in ("before", "after")
        is_text = not (is_between or is_bool or is_days or is_date)
        self._value.setVisible(is_text)
        self._value_low.setVisible(is_between)
        self._between_dash.setVisible(is_between)
        self._value_high.setVisible(is_between)
        self._value_bool.setVisible(is_bool)
        self._value_days.setVisible(is_days)
        self._value_date.setVisible(is_date)
        if is_text:
            self._value.setPlaceholderText("number" if ftype is int else "text")

    def value(self) -> Dict[str, Any]:
        field = self._field.currentData()
        op = self._op.currentData()
        ftype = FIELDS.get(field, {}).get("type", str)
        if op == "between":
            value: Any = [self._value_low.value(), self._value_high.value()]
        elif op == "in_the_last":
            value = self._value_days.value()
        elif op in ("before", "after"):
            value = self._value_date.date().toString("yyyy-MM-dd")
        elif ftype is bool:
            value = bool(self._value_bool.isChecked())
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
        ftype = FIELDS.get(field, {}).get("type", str)
        if op == "between":
            pair = _between_payload(v) or [0, 0]
            self._value_low.setValue(pair[0])
            self._value_high.setValue(pair[1])
        elif op == "in_the_last":
            try:
                self._value_days.setValue(int(v))
            except (TypeError, ValueError):
                self._value_days.setValue(30)
        elif op in ("before", "after"):
            qd = QDate.fromString(str(v)[:10], "yyyy-MM-dd")
            self._value_date.setDate(qd if qd.isValid() else QDate.currentDate())
        elif ftype is bool:
            self._value_bool.setChecked(bool(v))
        else:
            self._value.setText("" if v is None else str(v))


class SmartPlaylistEditorDialog(QDialog):
    """Modal editor for a single smart playlist.

    Frameless frosted dialog matching the Cast + Settings dialogs —
    custom titlebar, rounded translucent body, blur applied via the
    keep_above KWin noborder rule on KDE Wayland."""

    BODY_RADIUS = RADIUS_WINDOW

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        entry: Optional[Dict[str, Any]] = None,
        preset_rules: Optional[Dict[str, Any]] = None,
        suggested_name: Optional[str] = None,
        hint: Optional[str] = None,
    ):
        super().__init__(parent)
        self._hint_text = hint
        self._original = entry
        self._is_edit = bool(entry)
        # Save & Play deferral state — see ``_on_save_and_play``. The
        # editor keeps itself open during the async resolve+play work
        # so the user gets visible loading feedback INSIDE the editor
        # rather than landing on whatever surface they came from and
        # waiting blankly. The handler (set by
        # ``open_smart_playlist_editor``) does the work + calls
        # ``self.accept`` when done.
        self._save_and_play_handler: Optional[Any] = None
        # Flag flipped by `_on_save_and_play` when the handler is
        # invoked. The parent helper's `finished` handler reads it to
        # decide whether to also fire on_save (plain Save) or skip
        # (Save & Play already persisted via the handler).
        self._used_save_and_play: bool = False
        # Distinct title so the KWin `noborder` rule scopes to this
        # dialog without catching the main window. Both new + edit
        # share the same window title (matches the noborder rule);
        # the visible heading in the custom titlebar disambiguates.
        from modules.keep_above import SMART_PLAYLIST_EDITOR_WINDOW_TITLE

        self.setWindowTitle(SMART_PLAYLIST_EDITOR_WINDOW_TITLE)
        # Wide enough that the left form's match / sort / rule combos
        # show their labels in full — 720 squeezed them to clipping.
        self.setMinimumSize(860, 520)

        # Mirror the Cast + Settings dialogs: independent top-level
        # window (not Qt.Dialog so the main window doesn't fade as a
        # transient parent), frameless everywhere EXCEPT KDE Wayland
        # where the noborder rule strips chrome from the decorated
        # window (decorated + noborder keeps KWin blur alive during
        # a drag; pure frameless loses blur).
        from modules.platform_compat import is_kde_wayland

        _flags = Qt.WindowType.Window
        if not is_kde_wayland():
            _flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtSmartPlaylistEditor")
        # Non-modal so the main window stays draggable / interactive
        # while the editor is open — matches Settings + Cast dialogs.
        # Save / Cancel still close it normally; nothing else cares
        # about modality here since the editor doesn't gate any
        # follow-up modal flow.
        self.setWindowModality(Qt.WindowModality.NonModal)
        from modules.ui_helpers import DIALOG_BODY_COLOR as _DIALOG_BODY_COLOR

        self._dialog_body_color = _DIALOG_BODY_COLOR
        self._reapply_styles()
        # Live-accent: rebuild the stylesheet (which contains the
        # accent-tinted Selector rules) whenever the theme changes,
        # so a user changing accent / theme while the editor sits
        # open sees the new colors without restart.
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._reapply_styles)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        outer = QHBoxLayout(body)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_LG)
        root.addWidget(body, 1)

        # ── Left column: form ────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(SPACE_SM)
        outer.addLayout(left, 3)

        left.addWidget(_label("Name", dim=False))
        _initial_name = str((entry or {}).get("name") or "") or str(suggested_name or "")
        self._name = QLineEdit(_initial_name)
        self._name.setPlaceholderText("e.g. Recent favorites")
        left.addWidget(self._name)

        # Preset picker — loads a starter rule set. Saved as "Custom"
        # once the user touches anything.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(SPACE_SM)
        preset_row.addWidget(_label("Preset"))
        self._preset = Selector()
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
        self._match = Selector()
        self._match.addItem("all rules (AND)", "all")
        self._match.addItem("any rule (OR)", "any")
        self._match.currentIndexChanged.connect(lambda _i: self._queue_preview())
        match_row.addWidget(self._match)
        match_row.addSpacing(SPACE_MD)
        match_row.addWidget(_label("Sort"))
        self._sort = Selector()
        for s in _SORT_OPTIONS:
            self._sort.addItem(_SORT_LABELS.get(s, s or "default"), s)
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
        # Optional pre-seed hint — used when a right-click "More like
        # X" recipe couldn't fill in every rule it wanted (e.g. the
        # source album has no genres tagged on the server). Tells the
        # user WHY their recipe is leaner than expected so they can
        # add the missing rule themselves or fix their tagging.
        if self._hint_text:
            from modules.ui_helpers import TEXT_DIM as _TEXT_DIM_HINT

            hint_label = QLabel(self._hint_text)
            hint_label.setWordWrap(True)
            hint_label.setStyleSheet(
                f"color: {_TEXT_DIM_HINT}; {type_qss(TYPE_CAPTION)}"
            )
            left.addWidget(hint_label)
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

        # ── Right column: live preview + footer ──────────────────────
        right = QVBoxLayout()
        right.setSpacing(SPACE_SM)
        outer.addLayout(right, 2)

        right.addWidget(_label("Preview", dim=False))
        self._preview_status = _label("")
        right.addWidget(self._preview_status)
        self._preview_list = QListWidget()
        self._preview_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        # Track titles can be long ("Symphony No. 9 in D minor, Op. 125
        # — IV. Presto · Ode to Joy"); horizontal scrolling looks bad and
        # forces the user to scrub a tiny bar. Elide instead so each row
        # stays one line and the column expands to its natural width.
        self._preview_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._preview_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._preview_list.setUniformItemSizes(True)
        right.addWidget(self._preview_list, 1)

        # Footer: Save / Cancel — beneath the preview column so the list
        # can expand the full width to the dialog's right edge instead of
        # being trimmed by buttons floated to the right of it.
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(SPACE_SM)
        footer_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        footer_row.addWidget(self._cancel_btn)
        self._save_btn = QPushButton("Save")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_accept)
        footer_row.addWidget(self._save_btn)
        # Save & Play takes the accent treatment — it's the primary
        # action right after building / editing a smart playlist (the
        # user almost always wants to hear the result immediately).
        # `&&` escapes Qt's keyboard-accelerator-marker meaning of a
        # single `&`; without the escape the button renders as
        # "Save  Play" with the P underlined for Alt-P access.
        self._save_play_btn = QPushButton("Save && Play")
        self._save_play_btn.setObjectName("accent")
        self._save_play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_play_btn.setDefault(True)
        self._save_play_btn.clicked.connect(self._on_save_and_play)
        footer_row.addWidget(self._save_play_btn)
        right.addLayout(footer_row)

        # Preview debounce
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._refresh_preview)
        # Generation token: each preview run captures the current value;
        # a result whose generation is stale (a newer edit fired another
        # query that hasn't returned) is dropped. Without it the
        # slower-finishing query wins regardless of which started last,
        # so the preview could show results for an older rule set.
        self._preview_gen = 0

        # Seed the rule chips. Priority: an existing entry being edited
        # wins; otherwise a caller-supplied ``preset_rules`` dict (the
        # right-click "Create from this X" flow); otherwise empty.
        if entry and isinstance(entry.get("rules"), dict):
            self._apply_rules(entry["rules"])
        elif isinstance(preset_rules, dict):
            self._apply_rules(preset_rules)
        # Fire an initial preview after the dialog lays out.
        QTimer.singleShot(0, self._refresh_preview)

    # ── Theme ───────────────────────────────────────────────────────

    def _reapply_styles(self) -> None:
        """Rebuild + apply the dialog stylesheet. Merges GLOBAL_STYLE
        (which carries the accent-tinted base rules) with
        ``selector_qss()`` so every Selector inside the dialog gets
        the same accent-aware border / hover / focus treatment as
        the Settings dialog. Called at init AND on
        ``PlayerBus.theme_changed`` so live-accent flips through."""
        from modules.ui_helpers import GLOBAL_STYLE as _GS

        self.setStyleSheet(_GS + selector_qss())

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
        self._preview_gen += 1
        gen = self._preview_gen

        def _go() -> List[Dict[str, Any]]:
            from modules.providers import get_provider

            try:
                return list(get_provider().query_items(preview_rules))
            except Exception:
                return []

        async_io.run_async(
            _go,
            on_result=lambda items, g=gen: self._on_preview_result(items, g),
            on_error=lambda _e, g=gen: (
                self._preview_status.setText("preview unavailable")
                if g == self._preview_gen
                else None
            ),
        )

    def _on_preview_result(self, items: List[Dict[str, Any]], gen: int = 0) -> None:
        # Drop a stale result — a newer edit superseded this query.
        if gen != self._preview_gen:
            return
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

    def _on_save_and_play(self) -> None:
        """Save & Play — validate, enter a Loading state on the
        Save & Play button, then hand the entry to the registered
        handler (set via ``open_smart_playlist_editor``). Handler
        does the persist + async play work and calls ``self.accept``
        when playback has actually started; the editor stays open
        with visible loading feedback in between.

        Falls through to plain ``_on_accept`` (synchronous close) if
        no handler is registered — used by tests that construct the
        dialog directly and don't expect deferred-close semantics."""
        if self._save_and_play_handler is None:
            self._on_accept()
            return
        # Validate first; bail without entering loading if invalid.
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
        # Loading state: disable Save + Save & Play, swap S&P label.
        # Cancel stays active — user can abort if the resolve hangs.
        self._save_play_btn.setEnabled(False)
        self._save_play_btn.setText("Loading…")
        self._save_btn.setEnabled(False)
        # Mark which button was used so the parent helper's `finished`
        # handler knows not to also fire on_save (handler already
        # persisted).
        self._used_save_and_play = True
        # Hand off; handler is responsible for calling self.accept
        # when its async work completes (success OR failure).
        self._save_and_play_handler(self.values(), self.accept)

    def set_save_and_play_handler(self, handler: Optional[Any]) -> None:
        """Register the deferred-close handler for Save & Play.
        Signature: ``handler(entry: dict, dismiss: Callable[[], None])``.
        Called only when the user clicks Save & Play; the handler
        must call ``dismiss()`` exactly once when its async work
        finishes (success, empty match, or error) so the editor
        closes."""
        self._save_and_play_handler = handler

    def used_save_and_play(self) -> bool:
        """Whether the user clicked Save & Play (vs plain Save).
        Read by the parent helper after ``finished(Accepted)`` to
        decide whether on_save should ALSO fire (no — handler
        already persisted) or not."""
        return self._used_save_and_play

    def values(self) -> Dict[str, Any]:
        """Final entry dict — caller persists via ``settings.smart_playlists``.

        ``schema_version`` is carried through from the edited entry so
        a re-save doesn't silently re-stamp it; ``settings`` defaults a
        missing version to the current one on write."""
        out: Dict[str, Any] = {
            "name": self._name.text().strip(),
            "rules": self.rules_dict(),
            "created_at": (self._original or {}).get(
                "created_at"
            ) or datetime.now().isoformat(timespec="seconds"),
        }
        prior_version = (self._original or {}).get("schema_version")
        if prior_version is not None:
            out["schema_version"] = prior_version
        return out

    # ── Dialog chrome (matches Cast + Settings dialogs) ─────────────

    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtSmartPlaylistTitle")
        tb.setStyleSheet("""
            QWidget#jtSmartPlaylistTitle { background: transparent; }
            QWidget#jtSmartPlaylistTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        glyph = QLabel()
        glyph.setPixmap(icon("list").pixmap(QSize(18, 18)))
        h.addWidget(glyph)

        from modules.ui_helpers import (
            TEXT as _TEXT_TB,
        )
        from modules.ui_helpers import (
            TEXT_DIM as _TEXT_DIM_TB,
        )
        from modules.ui_helpers import (
            WASH_HOVER as _WASH_HOVER_TB,
        )

        title = QLabel("Edit smart playlist" if self._is_edit else "New smart playlist")
        title.setStyleSheet(f"color: {_TEXT_TB}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        h.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {_TEXT_DIM_TB};
                border: none; border-radius: 6px; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: {_WASH_HOVER_TB}; color: {_TEXT_TB}; }}
        """)
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def _apply_blur(self):
        """Blur behind the dialog when the active theme is frosted.
        No-op on compositors with no blur support."""
        from modules import blur
        from modules.theme import get_active_theme

        blur.apply(self, get_active_theme().blur, corner_radius=self.BODY_RADIUS)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._apply_blur)

    def keyPressEvent(self, e):
        # Esc closes the dialog. Frameless + WA_TranslucentBackground on
        # KDE Wayland doesn't always route to QDialog's default Esc
        # handler, so bind it explicitly. An open combo popup still eats
        # Esc first, which is the desired nested behaviour.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            # Read live from ui_helpers — refresh_theme() rebinds the
            # module attr on a theme-mode switch, so re-resolving keeps
            # the body fill correct across light/dark.
            from modules import ui_helpers as _uih

            p.setBrush(QColor(*_uih.DIALOG_BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()


def open_smart_playlist_editor(
    parent: Optional[QWidget] = None,
    entry: Optional[Dict[str, Any]] = None,
    preset_rules: Optional[Dict[str, Any]] = None,
    suggested_name: Optional[str] = None,
    hint: Optional[str] = None,
    on_save: Optional[Any] = None,
    on_save_and_play: Optional[Any] = None,
) -> "SmartPlaylistEditorDialog":
    """Show the editor dialog non-blocking. Fires one of
    ``on_save(entry)`` or ``on_save_and_play(entry)`` depending on
    which button the user clicked. Does nothing on cancel.

    Non-blocking (``show()`` + ``finished`` signal, not ``exec()``) so
    the main window stays interactive while the editor is open —
    ``QDialog.exec()`` spins a nested event loop that effectively
    freezes the parent window regardless of ``windowModality``.

    Args:
        parent: dialog parent widget (also owns the dialog's lifetime
            via Qt parent-child).
        entry: an existing saved playlist to edit. When set, its
            ``name`` / ``rules`` seed the form and ``preset_rules`` /
            ``suggested_name`` are ignored — editing wins.
        preset_rules: a raw, schema-valid rules dict to pre-populate
            the rule chips for a *new* playlist. This is the path the
            right-click "Create smart playlist from this artist /
            album / genre" flow uses — pass the dict returned by one of
            the ``modules.smart_playlists.presets`` ``from_*`` factories.
        suggested_name: a pre-filled name for a new playlist (e.g.
            ``"More by Bjork"``). The user can still edit it before
            saving.
        on_save: callable invoked with the saved entry dict
            (``name`` / ``rules`` / ``created_at``) when the user
            clicks plain Save. Not invoked on cancel.
        on_save_and_play: callable invoked with the saved entry dict
            when the user clicks Save & Play. Typically wraps the
            ``on_save`` work + a call to ``smart_playlists.play_entry``
            so playback starts immediately after persistence. Falls
            back to ``on_save`` when omitted (so a caller that only
            wires plain Save still sees the click as a save).

    Returns the dialog instance — callers can hold the reference if
    they need it, but usually don't (the dialog stays alive via its
    Qt parent and self-cleans on close).
    """
    dlg = SmartPlaylistEditorDialog(
        parent,
        entry,
        preset_rules=preset_rules,
        suggested_name=suggested_name,
        hint=hint,
    )

    # Save & Play handler runs synchronously when the button fires;
    # the dialog stays open in a Loading state until the handler
    # calls ``dismiss`` (== ``dlg.accept``). Without a handler,
    # Save & Play degrades to plain-Save semantics so the button
    # still does something useful.
    if on_save_and_play is not None:
        dlg.set_save_and_play_handler(on_save_and_play)
    elif on_save is not None:
        def _fallback(entry_out, dismiss):
            on_save(entry_out)
            dismiss()
        dlg.set_save_and_play_handler(_fallback)

    def _on_finished(code: int) -> None:
        if code == QDialog.DialogCode.Accepted:
            # Plain Save path: handler was never invoked, on_save
            # fires here. Save & Play path: handler already
            # persisted via on_save_and_play, skip.
            if on_save is not None and not dlg.used_save_and_play():
                on_save(dlg.values())
        dlg.deleteLater()

    dlg.finished.connect(_on_finished)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
