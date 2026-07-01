"""Settings → Colors page — debug/power-user UI for live-editing
every registered color token via HSV+opacity sliders.

Lives in its own module to keep settings_dialog.py from growing
unboundedly. The settings dialog calls ``build_colors_page(parent)``
to get a ready-to-add page widget.

UI shape:

    Section header (category label)
    ────────────────────────────────
    [swatch]  TOKEN_NAME                                       [Reset]
                description (one-line caption)
              H ━●━━━━━ 240   S ━●━━━━━  60   V ━●━━━━━  88   A ━●━━━━━ 100

One row per token, grouped by category. Slider drag fires after a
50ms settle (mirrors the EQ slider settle pattern) to avoid spamming
``apply_override`` 60×/sec on rapid drags — that would also re-fire
``theme_changed`` 60×/sec and trigger a re-stamp avalanche.

Tokens of kind ``hex`` hide the A slider (no alpha component).
Tokens of kind ``rgba`` or ``tuple_rgba`` show all four sliders.
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from jellytoast import color_tokens as ct
from jellytoast.color_picker import pick_screen_color
from jellytoast.design_tokens import (
    TYPE_CAPTION,
    TYPE_SUBHEAD,
    TYPE_TINY,
    rad,
    type_qss,
)
from jellytoast.icons import icon
from jellytoast.theme import ink_rgb
from jellytoast.ui_helpers import TEXT, ink_alpha

# ── Token value ↔ QColor conversion ────────────────────────────────────────

_RGBA_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)"
)


def token_to_qcolor(value: Any, kind: str) -> QColor:
    """Parse a token's stored value into a QColor.

    - ``hex``: ``"#rrggbb"`` → QColor with alpha=255.
    - ``rgba``: ``"rgba(r, g, b, a)"`` (or ``rgb(r,g,b)``) → QColor.
      Alpha is multiplied by 255 from the 0.0-1.0 css form.
    - ``tuple_rgba``: ``(r, g, b, a)`` int tuple → QColor.

    Falls back to opaque white on malformed input rather than raising;
    the editor stays usable even if a stored override is bad.
    """
    try:
        if kind == "hex":
            c = QColor(value)
            c.setAlpha(255)
            return c
        if kind == "tuple_rgba":
            r, g, b, a = value
            return QColor(int(r), int(g), int(b), int(a))
        # rgba string
        match = _RGBA_RE.search(value)
        if match:
            r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
            a_raw = match.group(4)
            alpha = int(round(float(a_raw) * 255)) if a_raw is not None else 255
            return QColor(r, g, b, alpha)
    except Exception:
        pass
    return QColor(255, 255, 255, 255)


def qcolor_to_token(color: QColor, kind: str) -> Any:
    """Serialize a QColor back into the token's storage format."""
    r, g, b, a = color.red(), color.green(), color.blue(), color.alpha()
    if kind == "hex":
        return f"#{r:02x}{g:02x}{b:02x}"
    if kind == "tuple_rgba":
        return (r, g, b, a)
    # rgba string — alpha as 0.0-1.0 with trim
    alpha_f = a / 255.0
    if abs(alpha_f - 1.0) < 1e-3:
        return f"rgba({r},{g},{b},1.0)"
    if abs(alpha_f) < 1e-3:
        return f"rgba({r},{g},{b},0)"
    return f"rgba({r},{g},{b},{alpha_f:.2f})"


# ── Swatch widget ───────────────────────────────────────────────────────────


class _Swatch(QWidget):
    """Fixed-size colored square showing the current token value.

    Renders with a checkerboard backdrop so the alpha channel is
    visually obvious for translucent tokens. 32x32 with 6px corner
    radius — sized to read clearly without dominating the row.
    """

    SIDE = 32

    def __init__(self, color: QColor, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(self.SIDE, self.SIDE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_color(self, color: QColor):
        self._color = color
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Rounded-square clip so both the checker AND the color fill
        # respect the corner radius.
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.SIDE, self.SIDE, rad(6), rad(6))
        p.setClipPath(path)
        # Checkerboard backdrop — 4px tiles in two greys so the
        # transparency of the fill on top is obvious.
        light = QColor(70, 70, 78)
        dark = QColor(40, 40, 46)
        tile = 8
        for ty in range(0, self.SIDE, tile):
            for tx in range(0, self.SIDE, tile):
                color = light if (tx // tile + ty // tile) % 2 else dark
                p.fillRect(tx, ty, tile, tile, color)
        # The token color on top — alpha shows the checker through.
        p.fillRect(0, 0, self.SIDE, self.SIDE, self._color)
        # Thin outline for definition — theme-aware ink (near-black on a
        # light theme, white on dark) so it stays visible against the body;
        # a hardcoded white outline vanished on light-fill tokens in light.
        p.setClipping(False)
        p.setPen(QColor(*ink_rgb(), 40))
        p.drawRoundedRect(0, 0, self.SIDE - 1, self.SIDE - 1, rad(6), rad(6))


# ── Per-token editor row ────────────────────────────────────────────────────


class _ColorTokenRow(QWidget):
    """One row of the colors page: swatch + name + sliders + reset
    button for a single token. Fires ``ct.apply_override(name, value)``
    on slider changes (with a 50ms settle to avoid spamming
    theme_changed during drag)."""

    SLIDER_PATCH_STYLE = f"""
        QSlider::groove:horizontal {{
            height: 3px;
            background: {ink_alpha(0.18)};
            border-radius: {rad(2)}px;
        }}
        QSlider::sub-page:horizontal {{
            background: {ink_alpha(0.45)};
            border-radius: {rad(2)}px;
        }}
        QSlider::add-page:horizontal {{
            background: {ink_alpha(0.18)};
            border-radius: {rad(2)}px;
        }}
        QSlider::handle:horizontal {{
            width: 10px; height: 10px; margin: -4px 0;
            background: #ffffff; border-radius: 5px;
        }}
    """

    def __init__(self, token: ct.ColorToken, parent: QWidget | None = None):
        super().__init__(parent)
        self._token = token
        self._show_alpha = token.kind in ("rgba", "tuple_rgba")
        self._suppress_signals = False
        # Last colour loaded into the sliders. Lets _load_from_current skip a
        # redundant repaint when an apply's theme_changed re-broadcast reaches
        # a row whose token didn't actually change (the on-release reload storm).
        self._last_loaded: QColor | None = None

        # Settle timer collapses rapid drag ticks into one apply.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(50)
        self._settle.timeout.connect(self._on_settled)

        # Listen for external token changes (accent picker, palette
        # load, ACCENT cascade-deriving ACCENT_DEEP/BORDER_ACCENT) so
        # this row's sliders + swatch reflect the current value.
        # _load_from_current uses _suppress_signals during setValue so
        # this won't re-trigger our own settle → apply_override loop.
        try:
            from jellytoast.player_state import PlayerBus

            PlayerBus.get().theme_changed.connect(self._on_external_change)
        except Exception:
            pass

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # ── Top row: swatch + name + reset button ──────────────────
        header = QHBoxLayout()
        header.setSpacing(10)
        header.setContentsMargins(0, 0, 0, 0)

        self._swatch = _Swatch(token_to_qcolor(ct.get_current(token.name), token.kind))
        header.addWidget(self._swatch)

        name_lbl = QLabel(token.name)
        name_lbl.setStyleSheet(f"color: {TEXT}; font-weight: 600;")
        header.addWidget(name_lbl)

        header.addStretch(1)

        # Numeric readout so the user can see exact slider values.
        self._readout = QLabel("")
        self._readout.setStyleSheet(
            f"color: {ink_alpha(0.55)}; font-family: monospace; "
            f"{type_qss(TYPE_TINY)}"
        )
        header.addWidget(self._readout)

        reset_btn = QPushButton("Reset")
        reset_btn.setFixedHeight(22)
        reset_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {ink_alpha(0.06)};
                color: {ink_alpha(0.85)};
                border: none;
                border-radius: {rad(4)}px;
                padding: 0 8px;
                {type_qss(TYPE_TINY)}
            }}
            QPushButton:hover {{ background: {ink_alpha(0.12)}; }}
            QPushButton:pressed {{ background: {ink_alpha(0.18)}; }}
        """
        )
        reset_btn.clicked.connect(self._on_reset)
        header.addWidget(reset_btn)

        outer.addLayout(header)

        # ── Description line ──────────────────────────────────────
        desc = QLabel(token.description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {ink_alpha(0.45)}; {type_qss(TYPE_TINY)} "
            "padding-left: 42px;"
        )
        outer.addWidget(desc)

        # ── Sliders row ────────────────────────────────────────────
        sliders = QHBoxLayout()
        sliders.setContentsMargins(42, 4, 0, 6)
        sliders.setSpacing(8)

        self._h_slider = self._make_slider(0, 360)
        self._s_slider = self._make_slider(0, 100)
        self._v_slider = self._make_slider(0, 100)
        self._a_slider = self._make_slider(0, 100) if self._show_alpha else None

        sliders.addWidget(self._labelled_slider("H", self._h_slider))
        sliders.addWidget(self._labelled_slider("S", self._s_slider))
        sliders.addWidget(self._labelled_slider("V", self._v_slider))
        if self._a_slider is not None:
            sliders.addWidget(self._labelled_slider("A", self._a_slider))
        # Trailing stretch soaks up leftover width so the capped sliders pack
        # left instead of spreading across the row.
        sliders.addStretch(1)

        outer.addLayout(sliders)

        # Initial slider positions from the current token value.
        self._load_from_current()

    # ── Helpers ────────────────────────────────────────────────────

    def _make_slider(self, lo: int, hi: int) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        # Compact + capped: a small minimum so they survive a narrow page, and
        # a maximum so H/S/V/(A) stay short and left-packed (via the trailing
        # stretch in the row) instead of each expanding to ~a third of the page
        # — that over-long stretch was the "sliders have room to reduce" gripe.
        s.setMinimumWidth(36)
        s.setMaximumWidth(110)
        s.setStyleSheet(self.SLIDER_PATCH_STYLE)
        # Tracking OFF is the key to a smooth drag: a mouse drag emits
        # ``sliderMoved`` continuously (→ a cheap, this-swatch-only preview)
        # but ``valueChanged`` only ONCE, on release. So the expensive
        # app-wide apply (ct.apply_override → theme_changed → an
        # app.setStyleSheet re-polish of every widget) can't fire mid-drag —
        # that re-polish was the GUI-thread lock the user felt as "chug".
        # A keyboard / wheel change isn't a drag, so it goes straight through
        # valueChanged and applies (debounced) as before.
        s.setTracking(False)
        s.sliderMoved.connect(self._on_slider_preview)
        s.valueChanged.connect(self._on_value_committed)
        return s

    def _on_slider_preview(self, _val: int):
        # Live during a drag: repaint THIS swatch + readout only, never apply.
        c = self._compose_color()
        self._swatch.set_color(c)
        self._update_readout(c)

    def _on_value_committed(self, _val: int):
        # Fires on release (tracking off) or on a keyboard / wheel step — never
        # mid-drag. Keep the swatch in sync (covers keyboard / wheel, where
        # sliderMoved never fired), then debounce the app-wide apply.
        if self._suppress_signals:
            return
        c = self._compose_color()
        self._swatch.set_color(c)
        self._update_readout(c)
        self._settle.start()

    def _labelled_slider(self, label: str, slider: QSlider) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QHBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {ink_alpha(0.55)}; {type_qss(TYPE_TINY)} "
            "font-family: monospace; min-width: 12px;"
        )
        v.addWidget(lbl)
        v.addWidget(slider, 1)
        return w

    def _load_from_current(self):
        """Initialise / refresh sliders from the token's current value."""
        c = token_to_qcolor(ct.get_current(self._token.name), self._token.kind)
        # An apply's theme_changed re-broadcast reaches EVERY row, but only the
        # edited token (and any accent-family followers) actually moved. Skip
        # the slider/swatch repaint for rows that didn't change — that mass
        # repaint was the on-release reload-storm hitch.
        if self._last_loaded is not None and c == self._last_loaded:
            return
        self._last_loaded = QColor(c)
        h = c.hsvHue()
        if h < 0:  # achromatic — Qt returns -1
            h = 0
        s = c.hsvSaturation()
        v = c.value()
        a = c.alpha()
        self._suppress_signals = True
        try:
            self._h_slider.setValue(h)
            self._s_slider.setValue(round(s * 100 / 255))
            self._v_slider.setValue(round(v * 100 / 255))
            if self._a_slider is not None:
                self._a_slider.setValue(round(a * 100 / 255))
        finally:
            self._suppress_signals = False
        self._swatch.set_color(c)
        self._update_readout(c)

    def _update_readout(self, c: QColor):
        if self._show_alpha:
            self._readout.setText(
                f"#{c.red():02x}{c.green():02x}{c.blue():02x}"
                f" · α{c.alpha() * 100 // 255}%"
            )
        else:
            self._readout.setText(c.name())

    def _compose_color(self) -> QColor:
        # sliderPosition(), NOT value(): with tracking off, value() only updates
        # on release, but sliderPosition() follows the handle live during a drag
        # — so the swatch previews the colour as you move (the whole point).
        h = self._h_slider.sliderPosition()
        s = round(self._s_slider.sliderPosition() * 255 / 100)
        v = round(self._v_slider.sliderPosition() * 255 / 100)
        a = (
            round(self._a_slider.sliderPosition() * 255 / 100)
            if self._a_slider
            else 255
        )
        c = QColor()
        c.setHsv(h, s, v, a)
        return c

    def _on_settled(self):
        c = self._compose_color()
        value = qcolor_to_token(c, self._token.kind)
        ct.apply_override(self._token.name, value)

    def _on_reset(self):
        ct.reset(self._token.name)
        self._load_from_current()

    def refresh(self):
        """Called by the page when an external change (e.g. accent
        picker, theme switch) might have shifted token values."""
        self._load_from_current()

    def _on_external_change(self):
        """theme_changed subscriber. Re-loads our slider values from
        the live token state so we reflect cascade-derived updates
        (e.g. ACCENT change derived a new ACCENT_DEEP) without the
        user touching this row."""
        self._load_from_current()


# ── Public entry point ──────────────────────────────────────────────────────


def build_colors_page() -> QWidget:
    """Construct the Settings → Colors page widget. Returns a scroll
    area wrapping a vertical stack of category sections + token rows."""
    rows: list[_ColorTokenRow] = []

    body = QWidget()
    body.setStyleSheet("background: transparent;")
    v = QVBoxLayout(body)
    v.setContentsMargins(0, 0, 14, 0)  # right pad for scrollbar
    v.setSpacing(14)

    # Top blurb explaining what this page is.
    intro = QLabel(
        "Adjust UI colors live. Changes persist across launches. "
        "Use Reset on a row to restore that token's default."
    )
    intro.setWordWrap(True)
    intro.setStyleSheet(
        f"color: {ink_alpha(0.65)}; {type_qss(TYPE_CAPTION)} padding-bottom: 4px;"
    )
    v.addWidget(intro)

    # Toolbar row: saved-palette combo + Save / Delete / Export /
    # Import / Reset-all. Combo lists every named palette in
    # debug/color_palettes/ — selecting one loads it (writes overrides).
    toolbar = QHBoxLayout()
    toolbar.setContentsMargins(0, 0, 0, 0)
    toolbar.setSpacing(6)

    btn_qss = f"""
        QPushButton {{
            background: {ink_alpha(0.06)};
            color: {ink_alpha(0.85)};
            border: 1px solid {ink_alpha(0.12)};
            border-radius: {rad(6)}px;
            padding: 6px 12px;
            {type_qss(TYPE_CAPTION)}
        }}
        QPushButton:hover {{ background: {ink_alpha(0.12)}; }}
        QPushButton:pressed {{ background: {ink_alpha(0.18)}; }}
        QPushButton:disabled {{ color: {ink_alpha(0.30)}; }}
    """

    # Eyedropper — sample any colour on screen and set it as the accent
    # (ct.apply_override cascades ACCENT_DEEP + BORDER_ACCENT). Sits first in
    # the toolbar so it reads as the primary "grab a colour" action.
    def _on_accent_sampled(hex_color: str):
        ct.apply_override("ACCENT", hex_color)
        for r in rows:
            r.refresh()

    dropper_btn = QPushButton()
    dropper_btn.setIcon(icon("eyedropper"))
    dropper_btn.setIconSize(QSize(16, 16))
    dropper_btn.setStyleSheet(btn_qss)
    dropper_btn.setToolTip("Pick an accent colour from the screen")
    dropper_btn.clicked.connect(lambda: pick_screen_color(_on_accent_sampled))
    toolbar.addWidget(dropper_btn)
    toolbar.addSpacing(8)

    # Use the dialog's _Selector (QPushButton + QMenu) so the
    # palettes dropdown picks up the same chrome as the rest of the
    # settings combos. Lazy import — settings_dialog is the entry
    # point that loads this module.
    from jellytoast.settings_dialog import _Selector

    palettes_combo = _Selector()
    palettes_combo.setMinimumWidth(160)
    palettes_combo.setToolTip("Apply a saved palette")
    _PALETTE_PLACEHOLDER = "— saved palettes —"

    def _refresh_palettes_combo():
        palettes_combo.blockSignals(True)
        try:
            palettes_combo.clear()
            palettes_combo.addItem(_PALETTE_PLACEHOLDER, None)
            for name in ct.list_palettes():
                palettes_combo.addItem(name, name)
        finally:
            palettes_combo.blockSignals(False)

    _refresh_palettes_combo()

    def _on_palette_picked(_idx: int):
        name = palettes_combo.currentData()
        if not name:
            return
        try:
            ct.load_palette(name)
        except Exception as exc:
            QMessageBox.warning(
                None, "Couldn't load palette", f"{name}: {exc}"
            )
            return
        for r in rows:
            r.refresh()
        # Snap combo back to placeholder so re-picking the same name
        # works (apply is one-shot, not a persistent selection).
        palettes_combo.blockSignals(True)
        try:
            palettes_combo.setCurrentIndex(0)
        finally:
            palettes_combo.blockSignals(False)

    palettes_combo.currentIndexChanged.connect(_on_palette_picked)
    toolbar.addWidget(palettes_combo)

    save_btn = QPushButton("Save…")
    save_btn.setStyleSheet(btn_qss)
    save_btn.setToolTip("Snapshot the current colors as a named palette")

    def _on_save_palette():
        name, ok = QInputDialog.getText(
            None, "Save palette", "Palette name:"
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        if "/" in name or "\\" in name:
            QMessageBox.warning(
                None,
                "Invalid name",
                "Palette names can't contain slashes.",
            )
            return
        if name in ct.list_palettes():
            ans = QMessageBox.question(
                None,
                "Overwrite palette",
                f"Palette '{name}' already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        ct.save_palette(name)
        _refresh_palettes_combo()

    save_btn.clicked.connect(_on_save_palette)
    toolbar.addWidget(save_btn)

    delete_btn = QPushButton("Delete…")
    delete_btn.setStyleSheet(btn_qss)
    delete_btn.setToolTip("Remove a saved palette")

    def _on_delete_palette():
        names = ct.list_palettes()
        if not names:
            QMessageBox.information(None, "No palettes", "No saved palettes to delete.")
            return
        name, ok = QInputDialog.getItem(
            None, "Delete palette", "Pick a palette:", names, 0, False
        )
        if not ok or not name:
            return
        ct.delete_palette(name)
        _refresh_palettes_combo()

    delete_btn.clicked.connect(_on_delete_palette)
    toolbar.addWidget(delete_btn)

    toolbar.addSpacing(8)

    export_btn = QPushButton("Export…")
    export_btn.setStyleSheet(btn_qss)
    export_btn.setToolTip("Save current colors to a JSON file")

    def _on_export():
        import json

        path, _ = QFileDialog.getSaveFileName(
            None, "Export palette", "jellytoast-palette.json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ct.export_palette(), f, indent=2)
        except Exception as exc:
            QMessageBox.warning(None, "Export failed", str(exc))

    export_btn.clicked.connect(_on_export)
    toolbar.addWidget(export_btn)

    import_btn = QPushButton("Import…")
    import_btn.setStyleSheet(btn_qss)
    import_btn.setToolTip("Load colors from a JSON file")

    def _on_import():
        import json

        path, _ = QFileDialog.getOpenFileName(
            None, "Import palette", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                palette = json.load(f)
            applied = ct.import_palette(palette)
        except Exception as exc:
            QMessageBox.warning(None, "Import failed", str(exc))
            return
        for r in rows:
            r.refresh()
        QMessageBox.information(
            None,
            "Palette imported",
            f"Applied {applied} color{'s' if applied != 1 else ''}.",
        )

    import_btn.clicked.connect(_on_import)
    toolbar.addWidget(import_btn)

    toolbar.addStretch(1)

    reset_all_btn = QPushButton("Reset all")
    reset_all_btn.setStyleSheet(btn_qss)
    reset_all_btn.setToolTip("Restore every token to its shipped default")

    def _on_reset_all():
        ct.reset_all()
        for r in rows:
            r.refresh()

    reset_all_btn.clicked.connect(_on_reset_all)
    toolbar.addWidget(reset_all_btn)
    v.addLayout(toolbar)

    # Categorised sections.
    for category, tokens in ct.tokens_by_category().items():
        header = QLabel(ct.CATEGORY_LABELS.get(category, category.title()))
        header.setStyleSheet(
            f"color: {ink_alpha(0.85)}; {type_qss(TYPE_SUBHEAD)} "
            f"padding: 8px 0 2px 0; border-bottom: 1px solid {ink_alpha(0.08)};"
        )
        v.addWidget(header)

        for token in tokens:
            row = _ColorTokenRow(token)
            rows.append(row)
            v.addWidget(row)

    v.addStretch(1)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet(
        "QScrollArea { background: transparent; border: none; } "
        "QScrollArea > QWidget > QWidget { background: transparent; }"
    )
    # As-needed (not AlwaysOff): the toolbar can be wider than a narrow dialog,
    # and AlwaysOff stranded its right end off-screen with no way to reach it.
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(body)
    return scroll
