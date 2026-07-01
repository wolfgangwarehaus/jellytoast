"""EQ settings page — 10-band graphic equalizer + master pre-amp,
parametric curve editor, AutoEQ import, and presets.

Extracted from ``settings_dialog.py`` (2026-06-02) to shrink the dialog
god-file; mirrors the ``settings_colors_page`` extraction. The page is a
self-contained ``QWidget`` that owns every EQ widget, the ~30 handlers,
the 30 ms settle timer, and the slider double-click ``eventFilter``. It
reads/writes the shared ``Settings`` object passed in and talks to the
rest of the app only through ``PlayerBus.eq_changed`` — byte-identical
behaviour to the in-dialog version it replaced.

``SettingsDialog`` builds this lazily in ``_build_eq_section`` and
re-exposes ``_eq_enabled_check`` / ``_eq_linear_phase_check`` /
``_refresh_eq_enabled_state`` (pointing at this page) so its bit-perfect
gating keeps reaching the EQ enable checks by name; the dialog's accent
re-stamp calls :meth:`reapply_accent`.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    font,
    rad,
    type_qss,
)
from jellytoast.player_state import PlayerBus
from jellytoast.selector import Selector as _Selector
from jellytoast.ui_helpers import (
    DISABLED_FG,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)

# Full "Linear phase" explanation, surfaced by the ⓘ button next to the
# checkbox (hover tooltip + click dialog) instead of a wall-of-text checkbox
# tooltip. Pre-wrapped to short lines so it renders tidily in both the hover
# popup and the click dialog.
_LINEAR_PHASE_INFO = (
    "Linear phase changes how the EQ filters the audio.\n"
    "\n"
    "Off (default) — minimum phase, like a classic analog EQ:\n"
    "near-zero latency, no pre-ringing, but a little phase shift\n"
    "around the bands you adjust.\n"
    "\n"
    "On — linear-phase FIR: every frequency stays time-aligned\n"
    "for cleaner transients (drums, plucked strings), at the cost\n"
    "of ~20 ms latency, ~3× CPU, and faint pre-ringing (a pre-echo\n"
    "just before sharp transients).\n"
    "\n"
    "Neither is strictly better — it's a trade-off. Only affects\n"
    "local playback, not casting."
)


class EqSettingsPage(QWidget):
    """The Equalizer section of the Settings → Playback page, as a
    standalone widget. See module docstring for the seam contract."""

    def __init__(self, settings, *, label_col_w: int = 130, parent=None):
        super().__init__(parent)
        self.s = settings
        self._label_col_w = int(label_col_w)
        # Initialised here (the in-dialog version relied on getattr(...,
        # False) defaults before the first event set them).
        self._eq_cast_blocking = False
        self._eq_dragging = False
        _lay = QVBoxLayout(self)
        _lay.setContentsMargins(0, 0, 0, 0)
        _lay.addWidget(self._build_eq_section())

    def _build_eq_section(self) -> QWidget:
        """10-band graphic EQ + master pre-amp. Per docs/research/
        eq_dsp.md. Off by default; explicit "no longer bit-perfect"
        disclosure on the toggle. Cast-greying observes
        PlayerBus.cast_started / cast_stopped — the EQ chain lives
        inside mpv and doesn't apply to the cast device's own decoder."""
        from jellytoast.eq_presets import (
            BAND_FREQUENCIES,
            PRESETS,
        )

        wrap = QFrame()
        wrap.setObjectName("jtEqSection")
        wrap.setStyleSheet(
            "QFrame#jtEqSection { background: transparent; border: none; }"
        )
        wv = QVBoxLayout(wrap)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(6)

        wv.addWidget(self._section_header("EQUALIZER"))
        # Same header-to-content gap pattern as Crossfade.
        wv.addSpacing(4)

        # Enable checkbox + Linear-phase sub-toggle on one row — the
        # master toggle plus EQ T2's opt-in mode. "Enable" reads
        # rather than "Equalizer" since the section header already
        # labels the section.
        eq_toggle_row = QHBoxLayout()
        eq_toggle_row.setSpacing(20)
        self._eq_enabled_check = QCheckBox("Enable")
        self._eq_enabled_check.setChecked(self.s.eq_enabled)
        self._eq_enabled_check.toggled.connect(self._on_eq_enabled_toggled)
        eq_toggle_row.addWidget(self._eq_enabled_check)
        # "Linear phase" + a small ⓘ info button. The full explanation was too
        # long for a checkbox tooltip, so a tight sub-row pairs the box with an
        # info button that surfaces the trade-offs on hover or click.
        lp_row = QHBoxLayout()
        # Match the breathing room before the other ⓘ icons in the dialog. A
        # touch wider than their 10 because this button has padding:0 (no
        # built-in side margin like the _info_button IconButtons).
        lp_row.setSpacing(12)
        lp_row.setContentsMargins(0, 0, 0, 0)
        self._eq_linear_phase_check = QCheckBox("Linear phase")
        self._eq_linear_phase_check.setChecked(self.s.eq_linear_phase)
        self._eq_linear_phase_check.setToolTip(
            "Linear-phase FIR EQ — see the ⓘ for the trade-offs."
        )
        self._eq_linear_phase_check.toggled.connect(self._on_eq_linear_phase_toggled)
        lp_row.addWidget(self._eq_linear_phase_check)
        from jellytoast.icons import icon as _icon

        self._eq_lp_info = QToolButton()
        self._eq_lp_info.setIcon(_icon("info", size=16))
        self._eq_lp_info.setAutoRaise(True)
        self._eq_lp_info.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eq_lp_info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._eq_lp_info.setToolTip(_LINEAR_PHASE_INFO)
        self._eq_lp_info.setAccessibleName("About linear phase")
        self._eq_lp_info.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0; }"
        )
        self._eq_lp_info.clicked.connect(self._show_linear_phase_info)
        lp_row.addWidget(self._eq_lp_info)
        eq_toggle_row.addLayout(lp_row)
        # EQ T3b — view-mode toggle. "Curve" replaces the 10-band
        # slider strip with the parametric curve editor; same band
        # data, different surface. Settings persists the choice so the
        # user's preferred view comes back next session.
        self._eq_advanced_check = QCheckBox("Curve")
        self._eq_advanced_check.setChecked(self.s.eq_view_advanced)
        self._eq_advanced_check.setToolTip(
            "Parametric curve editor — drag nodes on a log-frequency "
            "canvas. AutoEQ profiles allow horizontal drag (movable "
            "centres); graphic mode keeps freqs locked, gain only."
        )
        self._eq_advanced_check.toggled.connect(self._on_eq_advanced_toggled)
        eq_toggle_row.addWidget(self._eq_advanced_check)
        # Stretch AFTER Curve so the order reads Enable · Linear phase · Curve,
        # grouped at the left, instead of Curve floating at the far right.
        eq_toggle_row.addStretch(1)
        wv.addLayout(eq_toggle_row)
        wv.addSpacing(6)

        # Preset row: combo + Save / Delete. Right-padded so the
        # Delete button sits a pinch inside the 16k band's right
        # edge. Content width ≈ 516 after the dialog 820→720 cut;
        # band-grid 16k right edge at ~482 → 46-px trailing pad
        # leaves a 12-px breath inside the 16k boundary.
        # Label uses the same width + body-type styling as Quality /
        # Normalization / Duration above so the four labels line up
        # in one column and the four field starts in another.
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 46, 0)
        preset_row.setSpacing(16)

        preset_lbl = self._field_label("Preset:")
        preset_lbl.setMinimumWidth(self._label_col_w)
        preset_row.addWidget(preset_lbl)

        self._eq_preset_combo = _Selector()
        self._populate_eq_preset_combo()
        self._select_combo_by_data(self._eq_preset_combo, self.s.eq_preset)
        self._eq_preset_combo.currentIndexChanged.connect(self._on_eq_preset_changed)
        # Sized to fit the longest preset name comfortably without
        # gobbling all remaining horizontal space — at full stretch the
        # box read absurdly wide for the one short word "Flat". Stretch
        # is added AFTER the combo so the trailing Save / Delete buttons
        # still pin to the right edge of the row.
        self._eq_preset_combo.setFixedWidth(120)
        preset_row.addWidget(self._eq_preset_combo)
        preset_row.addStretch(1)

        self._eq_save_btn = QPushButton("Save…")
        self._eq_save_btn.clicked.connect(self._on_eq_save_preset)
        preset_row.addWidget(self._eq_save_btn)

        self._eq_delete_btn = QPushButton("Delete")
        self._eq_delete_btn.clicked.connect(self._on_eq_delete_preset)
        preset_row.addWidget(self._eq_delete_btn)

        wv.addLayout(preset_row)

        # Caption row — empty by default; populated during cast-greying
        # with "Casting — EQ inactive". Hidden when empty so it costs
        # zero vertical space on the default layout — the EQ section
        # is the tallest block on this page and the dialog has no
        # room to spare.
        self._eq_caption = QLabel("")
        self._eq_caption.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: 0 0 0 22px;"
        )
        self._eq_caption.setVisible(False)
        wv.addWidget(self._eq_caption)

        # ── Slider grid: pre-amp + 10 bands ─────────────────────────────────
        # Vertical sliders in a QGridLayout. Row 0 = live dB readout,
        # row 1 = slider, row 2 = band-centre label. Indices: column 0
        # is pre-amp, columns 1..10 are bands 31Hz..16kHz.
        self._eq_sliders: list[QSlider] = []
        self._eq_readouts: list[QLabel] = []
        # Band labels kept on instance so _refresh_eq_enabled_state can
        # restyle them (faint when EQ is off, dim when on) for the
        # "draw it in when checked" effect.
        self._eq_band_labels: list[QLabel] = []

        # Each column is its own QWidget with a QVBoxLayout —
        # using a single QGridLayout for all 11 columns let the slider
        # widget visually overflow into the band-label row at min/max
        # value. Per-column widgets give Qt strict bounds per column
        # so the layout can't bleed across rows.
        slider_frame = QFrame()
        slider_frame.setStyleSheet("QFrame { background: transparent; }")
        sf_layout = QHBoxLayout(slider_frame)
        # Left margin 0 (was 4) so the Pre column starts flush with
        # the page's content edge, lining up under the Preset: label
        # above. Horizontal spacing 2 (was 6) packs the bands tight
        # so the eleven columns read as a single equalizer block
        # rather than 11 separate widgets.
        sf_layout.setContentsMargins(0, 0, 4, 0)
        sf_layout.setSpacing(2)

        def _fmt_freq(hz: int) -> str:
            return f"{hz // 1000}k" if hz >= 1000 else str(hz)

        labels = ["Pre"] + [_fmt_freq(f) for f in BAND_FREQUENCIES]
        initial = [self.s.eq_preamp] + list(self.s.eq_bands)

        for label_text, val in zip(labels, initial, strict=False):
            col_widget = QWidget()
            col_widget.setStyleSheet("background: transparent;")
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(0, 0, 0, 0)
            # Default spacing is 0 here; explicit ``addSpacing()`` calls
            # below the readout / slider give precise control over how
            # far the handle sits from the readout and band labels.
            # Layout-spacing alone wasn't enough — the handle's vertical
            # range is the full slider widget, so the dot at -12 hits
            # the widget's bottom edge regardless of QSS groove margin.
            col_layout.setSpacing(0)

            readout = QLabel(self._fmt_db_readout(val))
            readout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            readout.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            readout.setFixedHeight(16)
            col_layout.addWidget(readout)
            self._eq_readouts.append(readout)

            # Small gap above the slider so the readout doesn't kiss
            # the +12 dot.
            col_layout.addSpacing(4)

            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setSingleStep(1)
            slider.setPageStep(3)
            slider.setTickPosition(QSlider.TickPosition.TicksRight)
            slider.setTickInterval(6)  # ticks at -12, -6, 0, +6, +12
            slider.setValue(int(round(float(val))))
            # With groove margin = handle radius (7 px) in the QSS,
            # the +12 handle's top edge sits flush with the slider
            # widget's top edge and the -12 handle's bottom edge sits
            # flush with the widget bottom. ``addSpacing`` on either
            # side of the widget gives the dots breathing room from
            # the readout / band labels — without that, the handle
            # at -12 lands on the "31" / "62" label below.
            slider.setFixedHeight(88)
            slider.setStyleSheet(self._eq_slider_qss())
            # Double-click returns the slider to 0 dB. Qt has no signal
            # for double-click on a slider, so we install an event
            # filter on the slider widget to catch the event.
            slider.installEventFilter(self)
            slider.valueChanged.connect(self._on_eq_slider_changed)
            # Defer the actual filter-chain apply until the user
            # releases the slider — mid-drag mpv["af"] rewrites cause
            # audible pops as the filter graph re-plugs. valueChanged
            # still fires for keyboard / click / double-click and those
            # paths go through the settle timer because no
            # sliderReleased fires.
            slider.sliderPressed.connect(self._on_eq_drag_started)
            slider.sliderReleased.connect(self._on_eq_drag_ended)
            # The slider gets center-aligned in its column so the
            # 4px-wide groove sits flush under the readout label.
            col_layout.addWidget(slider, 0, Qt.AlignmentFlag.AlignHCenter)
            self._eq_sliders.append(slider)

            # Gap below the slider so the -12 dot doesn't sit on the
            # band label.
            col_layout.addSpacing(8)

            band_lbl = QLabel(label_text)
            band_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            # TEXT_DIM (not TEXT_FAINT) so the freq label is legible
            # against the dialog background — the row sits right under
            # the slider's accent-purple fill and needed more contrast.
            band_lbl.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            band_lbl.setFixedHeight(16)
            col_layout.addWidget(band_lbl)
            self._eq_band_labels.append(band_lbl)

            col_widget.setFixedWidth(42)
            sf_layout.addWidget(col_widget)

        sf_layout.addStretch(1)
        wv.addWidget(slider_frame)
        self._eq_slider_frame = slider_frame

        # ── EQ T3b — parametric curve editor (alternative view) ───────
        # Same height envelope as the slider strip so toggling between
        # the two views doesn't reflow the rest of the dialog.
        from jellytoast.eq_curve_editor import EqCurveEditor

        self._eq_curve_editor = EqCurveEditor()
        self._eq_curve_editor.setFixedHeight(slider_frame.sizeHint().height() or 130)
        self._eq_curve_editor.band_dragging.connect(self._on_curve_band_dragging)
        self._eq_curve_editor.band_edited.connect(self._on_curve_band_edited)
        # T3c — Q wheel, add band (double-click empty), remove band
        # (right-click on node). All three only fire in PEQ mode.
        self._eq_curve_editor.band_q_changed.connect(self._on_curve_band_q_changed)
        self._eq_curve_editor.band_added.connect(self._on_curve_band_added)
        self._eq_curve_editor.band_removed.connect(self._on_curve_band_removed)
        self._eq_curve_editor.setVisible(False)
        wv.addWidget(self._eq_curve_editor)

        # ── AutoEQ profile (EQ T3a) ──────────────────────────────────
        # Headphone-correction profiles from autoeq.app or similar.
        # When loaded, the parametric path takes precedence over the
        # 10-band graphic gains; the sliders dim to make that clear.
        # See `docs/research/eq_dsp_v2.md` §6 (T3a) for the rationale.
        wv.addSpacing(4)
        autoeq_row = QHBoxLayout()
        autoeq_row.setSpacing(8)
        self._autoeq_status = QLabel("AutoEQ: not loaded")
        self._autoeq_status.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        autoeq_row.addWidget(self._autoeq_status, 1)
        self._autoeq_import_btn = QPushButton("Import…")
        self._autoeq_import_btn.clicked.connect(self._on_autoeq_import_clicked)
        autoeq_row.addWidget(self._autoeq_import_btn)
        self._autoeq_clear_btn = QPushButton("Clear")
        self._autoeq_clear_btn.clicked.connect(self._on_autoeq_clear_clicked)
        autoeq_row.addWidget(self._autoeq_clear_btn)
        wv.addLayout(autoeq_row)
        self._refresh_autoeq_status()

        # Slider drag → 30ms settle timer → one settings write + signal
        # emit. Per docs/research/eq_dsp.md §3 throttling: a 60Hz drag
        # × 11 sliders is otherwise 660 `af` writes/sec.
        self._eq_settle_timer = QTimer(self)
        self._eq_settle_timer.setSingleShot(True)
        self._eq_settle_timer.setInterval(30)
        self._eq_settle_timer.timeout.connect(self._on_eq_settled)
        self._eq_pending_user_drag = False

        # Cast-greying: observe cast_started / cast_stopped. EQ lives in
        # mpv; cast devices decode their own stream, so EQ doesn't apply
        # while casting. Disable the whole section + show a clarifying
        # caption while a cast is active.
        try:
            bus = PlayerBus.get()
            bus.cast_started.connect(self._on_eq_cast_active)
            bus.cast_stopped.connect(self._on_eq_cast_cleared)
        except Exception:
            pass

        # Initialize enabled / disabled state from current settings +
        # check if a cast is already active at dialog construction time.
        self._refresh_eq_enabled_state()

        # EQ T3b — restore the persisted view (Simple sliders vs
        # Advanced curve editor). Done after _refresh_eq_enabled_state
        # so any disabled-state styling already applies.
        if bool(self.s.eq_view_advanced):
            self._eq_slider_frame.setVisible(False)
            self._eq_curve_editor.setVisible(True)
            self._sync_curve_editor_from_bands()

        # Stash refs to enable bulk en/disable in cast-greying.
        self._eq_section_widget = wrap
        self._eq_preset_count_builtin = len(PRESETS)
        return wrap

    # ── EQ helpers ──────────────────────────────────────────────────────────

    def _eq_slider_qss(self) -> str:
        """Vertical EQ slider QSS. Reads accent live so a theme change
        rebuilds it on the next dialog open / theme_changed.

        The handle was previously solid ACCENT + 2px white border —
        too bright and high-contrast compared to the rest of the
        dialog's subtle accent treatment (combo borders use
        rgba(accent, 0.45)). Toned down to ACCENT_DEEP fill + 1px
        rgba(accent, 0.55) border so the dot reads as part of the
        dialog family instead of a hot spotlight."""
        from jellytoast.theme import _hex_to_rgb
        from jellytoast.ui_helpers import ACCENT_DEEP as _ACCENT_DEEP

        try:
            ar, ag, ab = _hex_to_rgb(_ACCENT_DEEP)
        except Exception:
            ar, ag, ab = 124, 102, 208

        return f"""
            QSlider:vertical {{
                background: transparent;
            }}
            QSlider::groove:vertical {{
                width: 4px;
                /* Margin matches the handle radius (7 px) so the
                   handle's top edge at +12 sits flush with the rail
                   top and the bottom edge at -12 sits flush with the
                   rail bottom — no overshoot. Breathing room from
                   the readout / band labels comes from
                   ``addSpacing`` calls around the slider widget, not
                   from this margin. */
                margin: 7px 0;
                background: {ink_alpha(0.10)};
                border-radius: {rad(2)}px;
            }}
            /* Sub-page / add-page intentionally transparent. Qt paints
               them on top of the groove without honouring the groove's
               margin, so any non-transparent fill leaks past the rail
               ends as faint over-drawn bits at top / bottom. The
               groove background is the rail; that's enough. */
            QSlider::sub-page:vertical,
            QSlider::add-page:vertical {{
                background: transparent;
                border: none;
            }}
            QSlider::handle:vertical {{
                width: 14px; height: 14px; margin: 0 -5px;
                background: {_ACCENT_DEEP}; border-radius: 7px;
                border: 1px solid rgba({ar},{ag},{ab},0.55);
            }}
            /* When EQ is disabled (master toggle off OR cast active),
               the accent handle reads as still-active. Strip the accent
               and use an ink wash + lighter border so the whole grid
               obviously feels OFF — "draws in" the accent when the
               user re-enables. */
            QSlider::handle:vertical:disabled {{
                background: {ink_alpha(0.18)};
                border: 1px solid {ink_alpha(0.10)};
            }}
            QSlider::groove:vertical:disabled {{
                background: {ink_alpha(0.05)};
            }}
            QSlider::tick:vertical {{
                background: {ink_alpha(0.18)};
            }}
        """

    def _fmt_db_readout(self, val) -> str:
        try:
            x = float(val)
        except (TypeError, ValueError):
            x = 0.0
        if abs(x) < 0.05:
            return "0"
        sign = "+" if x > 0 else "−"
        return f"{sign}{abs(x):g}"

    def _populate_eq_preset_combo(self):
        """Built-in presets + user presets + Custom. Custom is the
        sentinel for "the user dragged a slider so no named preset
        applies". Always selected programmatically from the slider
        path; never user-picked."""
        from jellytoast.eq_presets import PRESETS

        self._eq_preset_combo.blockSignals(True)
        try:
            self._eq_preset_combo.clear()
            for name in PRESETS:
                self._eq_preset_combo.addItem(name, name)
            user = self.s.eq_user_presets
            if user:
                # Separator-ish — insert a divider via a non-selectable
                # disabled item so user presets visually group.
                for name in sorted(user):
                    self._eq_preset_combo.addItem(name, name)
            # Custom always last. Stored so the combo always has a
            # selectable label that reflects the slider state.
            self._eq_preset_combo.addItem("Custom", "Custom")
        finally:
            self._eq_preset_combo.blockSignals(False)

    def _on_eq_enabled_toggled(self, val: bool):
        self.s.eq_enabled = val
        self._refresh_eq_enabled_state()
        self._emit_eq_changed()

    def _on_eq_linear_phase_toggled(self, val: bool):
        """EQ T2 — flip between IIR (``anequalizer``) and linear-phase
        FIR (``firequalizer``). ``apply_eq`` reads the setting at apply
        time and picks the right formatter; firing ``eq_changed`` here
        triggers the re-apply (the ``_last_eq_state`` cache key includes
        ``linear_phase`` so the rewrite isn't short-circuited)."""
        self.s.eq_linear_phase = bool(val)
        self._emit_eq_changed()

    def _show_linear_phase_info(self):
        """Show the full linear-phase explanation on click (same text as the
        ⓘ button's hover tooltip), so it surfaces even when hover-tooltips
        are globally disabled. App-styled frosted dialog for consistency with
        the other ⓘ buttons."""
        from jellytoast.frosted_dialog import frosted_info

        frosted_info(self, "Linear phase", _LINEAR_PHASE_INFO)

    # ── AutoEQ profile import (EQ T3a) ──────────────────────────────

    def _refresh_autoeq_status(self):
        """Update the status label + Clear button enabled state based
        on whether a profile is loaded. Called on page build, after
        every import, and after clear. The 10-band slider grid greying
        is handled by ``_refresh_eq_enabled_state`` which reads the
        same setting."""
        if not hasattr(self, "_autoeq_status"):
            return
        raw = self.s.eq_autoeq_profile_json or ""
        if not raw:
            self._autoeq_status.setText("AutoEQ: not loaded")
            self._autoeq_clear_btn.setEnabled(False)
            return
        try:
            parsed = json.loads(raw)
            n_bands = len(parsed.get("bands", []))
            n_skipped = len(parsed.get("skipped", []))
            preamp = parsed.get("preamp_db", 0.0)
            parts = [f"{n_bands} bands"]
            if abs(preamp) > 0.05:
                parts.append(f"preamp {preamp:+.1f} dB")
            if n_skipped:
                parts.append(f"{n_skipped} skipped")
            self._autoeq_status.setText("AutoEQ: " + " · ".join(parts))
            self._autoeq_clear_btn.setEnabled(True)
        except (json.JSONDecodeError, TypeError):
            self._autoeq_status.setText("AutoEQ: (corrupt — Clear to reset)")
            self._autoeq_clear_btn.setEnabled(True)

    def _on_autoeq_import_clicked(self):
        """Open a small paste-area dialog. On accept, parse the input as
        an AutoEQ ``ParametricEQ.txt`` profile, save it to settings, and
        re-apply the EQ. Validation errors surface in a status line in
        the dialog rather than as a separate message box — the user can
        edit and retry without dismissing."""
        from jellytoast import ui_helpers as _u
        from jellytoast.eq_presets import parse_autoeq_profile
        from jellytoast.frosted_dialog import FrostedDialog

        # App-styled frosted dialog (was a bare QDialog → native palette,
        # near-black on a light theme). content_layout hosts the widgets.
        # Read colour tokens live off ui_helpers — this module imported them
        # by value, so a dark↔light switch since import would leave the
        # frozen copies stale; the dialog is built fresh on each open.
        dlg = FrostedDialog(self, title="Import AutoEQ profile")
        dlg.resize(560, 380)
        layout = dlg.content_layout
        layout.setSpacing(10)

        instructions = QLabel(
            "Paste a ParametricEQ.txt-style profile from autoeq.app or "
            "your headphone correction source. Each line should look "
            "like: <code>Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41</code>. "
            "Shelf filters (LSC / HSC) are skipped — most headphone "
            "profiles are predominantly peaking filters."
        )
        instructions.setWordWrap(True)
        instructions.setStyleSheet(
            f"color: {_u.TEXT_DIM}; {type_qss(TYPE_CAPTION)} background: transparent;"
        )
        layout.addWidget(instructions)

        text_edit = QPlainTextEdit()
        text_edit.setPlaceholderText(
            "Preamp: -6.6 dB\nFilter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41\n…"
        )
        # GLOBAL_STYLE themes QLineEdit but not QPlainTextEdit — mirror the
        # QLineEdit treatment so the paste area matches the input surface.
        text_edit.setStyleSheet(
            f"QPlainTextEdit {{ background: {_u.ink_alpha(0.05)}; "
            f"border: 1px solid {_u.BORDER}; border-radius: {rad(8)}px; "
            f"padding: 8px 12px; color: {_u.TEXT}; "
            f"selection-background-color: {_u.ACCENT_DEEP}; }}"
        )
        layout.addWidget(text_edit, 1)

        preview = QLabel("")
        preview.setStyleSheet(
            f"color: {_u.TEXT_DIM}; {type_qss(TYPE_CAPTION)} background: transparent;"
        )
        preview.setWordWrap(True)
        layout.addWidget(preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Import")
        ok_btn.setEnabled(False)
        layout.addWidget(btns)

        # Live preview as the user types/pastes — shows band count +
        # preamp + skipped count without committing the profile.
        def _on_text_changed():
            text = text_edit.toPlainText()
            if not text.strip():
                preview.setText("")
                ok_btn.setEnabled(False)
                return
            parsed = parse_autoeq_profile(text)
            if not parsed["bands"]:
                preview.setText(
                    "No peaking filters detected — check the format."
                )
                ok_btn.setEnabled(False)
                return
            n_bands = len(parsed["bands"])
            n_skipped = len(parsed["skipped"])
            parts = [f"{n_bands} bands"]
            if abs(parsed["preamp_db"]) > 0.05:
                parts.append(f"preamp {parsed['preamp_db']:+.1f} dB")
            if n_skipped:
                parts.append(f"{n_skipped} non-peaking filters will be skipped")
            preview.setText("Ready: " + " · ".join(parts))
            ok_btn.setEnabled(True)

        text_edit.textChanged.connect(_on_text_changed)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        text = text_edit.toPlainText()
        parsed = parse_autoeq_profile(text)
        if not parsed["bands"]:
            return  # safety — UI shouldn't have enabled Ok
        # Persist the full parsed dict (including skipped) so the
        # status row can surface "N skipped" without re-parsing the
        # raw text. apply_eq reads bands + preamp_db only.
        self.s.eq_autoeq_profile_json = json.dumps(parsed)
        self._refresh_autoeq_status()
        self._refresh_eq_enabled_state()  # grey the sliders
        self._sync_curve_editor_from_bands()  # T3b — update editor view
        self._emit_eq_changed()  # trigger re-apply

    def _on_autoeq_clear_clicked(self):
        """Drop the active profile. Sliders un-grey; graphic EQ takes
        the floor again. Doesn't touch the slider gains — the user's
        last-saved curve resumes immediately."""
        if not self.s.eq_autoeq_profile_json:
            return
        self.s.eq_autoeq_profile_json = ""
        self._refresh_autoeq_status()
        self._refresh_eq_enabled_state()
        # Re-seed the curve editor with the (now-active) graphic bands.
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    # ── EQ T3b — curve editor view toggle + sync ────────────────────

    def _on_eq_advanced_toggled(self, on: bool):
        """Swap the slider grid for the curve editor (or back). Bands
        are the same data underneath, just visualized differently."""
        self.s.eq_view_advanced = bool(on)
        self._eq_slider_frame.setVisible(not on)
        self._eq_curve_editor.setVisible(on)
        if on:
            self._sync_curve_editor_from_bands()

    def _sync_curve_editor_from_bands(self):
        """Push the active band list into the curve editor. Reads
        AutoEQ profile if loaded; else builds parametric bands from
        the graphic-mode slider gains. ``lock_freq`` follows mode:
        AutoEQ → False (movable centres); graphic → True (ISO-locked)."""
        if not hasattr(self, "_eq_curve_editor"):
            return
        from jellytoast.eq_presets import build_default_parametric_bands

        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if autoeq_raw:
            try:
                parsed = json.loads(autoeq_raw)
                bands = list(parsed.get("bands", []))
                self._eq_curve_editor.set_bands(bands, lock_freq=False)
                return
            except (json.JSONDecodeError, TypeError):
                pass
        # Graphic mode — synthesize parametric bands from slider gains.
        try:
            gains = list(self.s.eq_bands)
            bands = build_default_parametric_bands(gains)
        except Exception:
            bands = []
        self._eq_curve_editor.set_bands(bands, lock_freq=True)

    def _on_curve_band_dragging(self, idx: int, freq: float, gain: float):
        """Live updates from the curve editor — mirror back into the
        corresponding slider so the user sees both surfaces move in
        lockstep. No settings write yet (cheap repaint only); the
        final settings persist happens on band_edited (release)."""
        # In AutoEQ mode the curve bands don't map to the fixed 10-band ISO
        # sliders, so mirroring gain into _eq_sliders[idx+1] writes the WRONG
        # slider (and leaves a stale value when AutoEQ is later cleared). The
        # curve editor owns its own live repaint there.
        if self.s.eq_autoeq_profile_json:
            return
        if 0 <= idx < len(getattr(self, "_eq_sliders", [])):
            # _eq_sliders is [pre-amp, band0, band1, ..., band9]; band
            # at index idx maps to slider idx+1.
            slider = self._eq_sliders[idx + 1] if idx + 1 < len(self._eq_sliders) else None
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(int(round(gain)))
                slider.blockSignals(False)
        # Update the readout label in-place for the dragging band.
        if 0 <= idx < len(getattr(self, "_eq_readouts", []) or []) - 1:
            readout = self._eq_readouts[idx + 1]
            readout.setText(self._fmt_db_readout(gain))

    def _on_curve_band_edited(self, idx: int, freq: float, gain: float):
        """Drag-release — persist the change. In graphic mode the
        slider state IS the persistence; in AutoEQ mode we re-serialise
        the modified profile."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if autoeq_raw:
            try:
                parsed = json.loads(autoeq_raw)
                bands = list(parsed.get("bands", []))
                if 0 <= idx < len(bands):
                    # If the centre moved, the user-set Q stays put —
                    # recompute w under the same Q so the bandwidth in
                    # octaves stays consistent as the band slides. Capture
                    # the OLD centre+width FIRST: reading f/w after
                    # overwriting f would compute Q from the new centre,
                    # making the Q-preserve a no-op (the bug this fixes).
                    from jellytoast.eq_curve_editor import width_to_q

                    old_f = float(bands[idx].get("f", freq))
                    old_w = float(bands[idx].get("w", old_f))
                    old_q = width_to_q(old_f, old_w)
                    bands[idx]["f"] = int(round(freq))
                    bands[idx]["g"] = float(gain)
                    bands[idx]["w"] = bands[idx]["f"] / max(0.1, old_q)
                    parsed["bands"] = bands
                    self.s.eq_autoeq_profile_json = json.dumps(parsed)
            except (json.JSONDecodeError, TypeError):
                return
        else:
            # Graphic mode — persist the modified slider gain. Freq is
            # locked in this mode so freq changes are ignored.
            try:
                gains = list(self.s.eq_bands)
                if 0 <= idx < len(gains):
                    gains[idx] = float(gain)
                    self.s.eq_bands = gains
            except Exception:
                return
        self._emit_eq_changed()

    def _on_curve_band_q_changed(self, idx: int, new_w_hz: float):
        """Wheel-over-node — adjust the band's bandwidth. PEQ mode
        only (the editor blocks wheel in graphic mode), so we always
        write to the AutoEQ profile."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            if 0 <= idx < len(bands):
                bands[idx]["w"] = float(new_w_hz)
                parsed["bands"] = bands
                self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._emit_eq_changed()

    def _on_curve_band_added(self, freq: float, gain: float):
        """Double-click-on-empty — append a new band at the clicked
        spot. Default Q = 1.0 (one-octave wide). PEQ mode only — the
        editor blocks the gesture in graphic mode."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            f = int(round(freq))
            bands.append({"f": f, "w": float(f), "g": float(gain), "t": 0})
            parsed["bands"] = bands
            self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    def _on_curve_band_removed(self, idx: int):
        """Right-click-on-node — drop the band. The editor refuses to
        remove the last one, so this can't end up with an empty list."""
        autoeq_raw = self.s.eq_autoeq_profile_json or ""
        if not autoeq_raw:
            return
        try:
            parsed = json.loads(autoeq_raw)
            bands = list(parsed.get("bands", []))
            if 0 <= idx < len(bands):
                del bands[idx]
                parsed["bands"] = bands
                self.s.eq_autoeq_profile_json = json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            return
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    def _refresh_eq_enabled_state(self):
        """Apply the enable/disable cascade based on current settings
        AND any active cast. Sliders dim but remain legible when EQ is
        off (per research doc §4) so the user can preview a curve; they
        hard-disable on cast so the user understands the section is
        inactive."""
        eq_on = bool(self.s.eq_enabled)
        cast_active = getattr(self, "_eq_cast_blocking", False)
        # Controls below the enabled toggle gate on the master switch.
        # Disable entirely while casting since the chain has no effect.
        section_active = not cast_active
        # AutoEQ profile loaded → graphic-band controls dim so the
        # user knows the slider gains aren't currently driving the
        # filter. Preset combo + Save/Delete dim for the same reason
        # (they only affect the graphic curve).
        autoeq_active = bool(self.s.eq_autoeq_profile_json)
        graphic_active = section_active and eq_on and not autoeq_active
        self._eq_preset_combo.setEnabled(graphic_active)
        self._eq_save_btn.setEnabled(graphic_active)
        self._eq_delete_btn.setEnabled(
            graphic_active and self._current_preset_is_user()
        )
        # Linear-phase sub-toggle is only meaningful when EQ is on + not
        # gated by cast. Bit-perfect needs no separate override here:
        # settings_dialog._refresh_bit_perfect_gating forces s.eq_enabled
        # =False (and greys this same widget object) when bit-perfect turns
        # on, so eq_on is already False and this line keeps the check
        # disabled — it's re-greyed directly there, not from this method.
        if hasattr(self, "_eq_linear_phase_check"):
            self._eq_linear_phase_check.setEnabled(section_active and eq_on)
        for s in self._eq_sliders:
            s.setEnabled(graphic_active)
        # The curve editor is the advanced-view twin of the sliders (same
        # graphic bands), so it greys on the same condition. Its paintEvent
        # dims itself when disabled; repaint so the change shows immediately.
        if hasattr(self, "_eq_curve_editor"):
            self._eq_curve_editor.setEnabled(graphic_active)
            self._eq_curve_editor.update()
        for r in self._eq_readouts:
            # Readouts stay visible even when EQ is off so the user can
            # see what curve they have queued; just dim them further.
            r.setStyleSheet(
                f"color: {DISABLED_FG if not eq_on else TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
        # Band labels mirror the readouts — TEXT_DIM when on, deeper
        # DISABLED_FG when off — so the entire grid reads as one
        # cohesive "drawn in" cluster when the user enables EQ.
        for b in self._eq_band_labels:
            b.setStyleSheet(
                f"color: {DISABLED_FG if not eq_on else TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
        if cast_active:
            self._eq_caption.setText(
                "Casting — EQ applies to local playback only and is inactive now."
            )
            self._eq_caption.setVisible(True)
        else:
            self._eq_caption.setText("")
            self._eq_caption.setVisible(False)

    def _current_preset_is_user(self) -> bool:
        name = self._eq_preset_combo.currentData() or ""
        return name in self.s.eq_user_presets

    def _on_eq_cast_active(self, *_args):
        self._eq_cast_blocking = True
        self._refresh_eq_enabled_state()

    def _on_eq_cast_cleared(self, *_args):
        self._eq_cast_blocking = False
        self._refresh_eq_enabled_state()

    def _on_eq_drag_started(self):
        """Mouse drag began on a slider. While dragging, valueChanged
        updates only the readout — the actual filter-chain apply waits
        for sliderReleased. mpv["af"] rewrites mid-drag cause audible
        pops as the filter graph re-plugs; deferring to release means
        one clean apply per slider gesture."""
        self._eq_dragging = True

    def _on_eq_drag_ended(self):
        """Mouse drag ended. Cancel any pending settle and apply
        immediately so the user hears the new curve as soon as they
        release the slider."""
        self._eq_dragging = False
        if self._eq_settle_timer.isActive():
            self._eq_settle_timer.stop()
        self._eq_pending_user_drag = True
        self._on_eq_settled()

    def _on_eq_slider_changed(self, _val: int):
        # Update the readout immediately for live visual feedback.
        sender = self.sender()
        if isinstance(sender, QSlider):
            try:
                idx = self._eq_sliders.index(sender)
                self._eq_readouts[idx].setText(self._fmt_db_readout(sender.value()))
            except (ValueError, IndexError):
                pass
        self._eq_pending_user_drag = True
        # Mid-drag: only the readout updates; the filter apply waits
        # for sliderReleased to avoid mid-drag chain rewrites and
        # the resulting audible pops. Non-drag value changes
        # (keyboard arrows, click on track, double-click-to-zero,
        # preset pick) don't fire sliderPressed/Released, so they
        # fall through to the settle timer which collapses rapid
        # sequential changes into one apply.
        if getattr(self, "_eq_dragging", False):
            return
        self._eq_settle_timer.start()

    def _on_eq_settled(self):
        """Slider settle — persist + emit. Switches preset to Custom
        if the user dragged manually."""
        if not self._eq_pending_user_drag:
            return
        self._eq_pending_user_drag = False
        preamp = float(self._eq_sliders[0].value())
        bands = [float(s.value()) for s in self._eq_sliders[1:]]
        self.s.eq_preamp = preamp
        self.s.eq_bands = bands
        # Drag → Custom unless the slider state happens to match the
        # currently-selected preset (e.g. user dragged then dragged
        # back). Cheap check; named-preset preservation is nicer UX.
        current_name = self._eq_preset_combo.currentData() or ""
        if not self._slider_state_matches_preset(current_name, preamp, bands):
            self._select_combo_by_data(self._eq_preset_combo, "Custom")
            self.s.eq_preset = "Custom"
        # EQ T3b — keep the curve editor in sync with slider edits so
        # toggling Simple → Curve doesn't show stale data. Cheap when
        # the editor is hidden (no paint until visible).
        self._sync_curve_editor_from_bands()
        self._emit_eq_changed()

    def _slider_state_matches_preset(
        self, name: str, preamp: float, bands: list
    ) -> bool:
        from jellytoast.eq_presets import PRESETS

        if name == "Custom":
            return False
        if name in PRESETS:
            ref = PRESETS[name]
            # Built-in presets carry auto-attenuated pre-amp (see
            # _on_eq_preset_changed) — match the same formula here so
            # the combo doesn't flip to Custom after the user picks a
            # built-in.
            max_positive = max(ref) if ref else 0.0
            expected_preamp = -max_positive if max_positive > 0 else 0.0
            return (
                all(abs(a - b) < 1e-6 for a, b in zip(ref, bands, strict=False))
                and abs(preamp - expected_preamp) < 1e-6
            )
        user = self.s.eq_user_presets.get(name)
        if user is None:
            return False
        ref_bands = user.get("bands", [])
        ref_preamp = float(user.get("preamp", 0.0))
        return (
            abs(preamp - ref_preamp) < 1e-6
            and all(abs(a - b) < 1e-6 for a, b in zip(ref_bands, bands, strict=False))
        )

    def _on_eq_preset_changed(self, _idx: int):
        name = self._eq_preset_combo.currentData() or ""
        if name == "Custom":
            # Selecting Custom keeps current slider values; just persist
            # the preset name so the combo reflects state on next open.
            self.s.eq_preset = "Custom"
            self._refresh_eq_enabled_state()
            return
        # Pull preset values from built-in or user table.
        from jellytoast.eq_presets import PRESETS, get_preset

        if name in PRESETS:
            bands = get_preset(name)
            # Auto-attenuate pre-amp by the max positive band so
            # cascaded biquad gain doesn't clip on hot masters. Without
            # this, Pop / Rock / Bass Boost (which all have +5 to +7 dB
            # bands) sound garbly/muddy because the cumulative chain
            # gain pushes into clipping territory. Per the research
            # doc's "Pre-amp clipping" edge case — Audacious-style
            # auto-attenuation. User-saved presets carry their own
            # explicit pre-amp so we don't touch those.
            max_positive = max(bands) if bands else 0.0
            preamp = -max_positive if max_positive > 0 else 0.0
        else:
            user = self.s.eq_user_presets.get(name)
            if user is None:
                return
            preamp = float(user.get("preamp", 0.0))
            bands = [float(b) for b in user.get("bands", [])]
            from jellytoast.eq_presets import BAND_COUNT

            if len(bands) != BAND_COUNT:
                return
        # Snap sliders without firing the drag handler.
        self._apply_slider_values(preamp, bands)
        self.s.eq_preamp = preamp
        self.s.eq_bands = bands
        self.s.eq_preset = name
        self._refresh_eq_enabled_state()
        self._emit_eq_changed()

    def _apply_slider_values(self, preamp: float, bands: list):
        """Set slider values without triggering the user-drag handler."""
        values = [preamp] + list(bands)
        for slider, readout, val in zip(self._eq_sliders, self._eq_readouts, values, strict=False):
            iv = max(-12, min(12, int(round(float(val)))))
            slider.blockSignals(True)
            try:
                slider.setValue(iv)
            finally:
                slider.blockSignals(False)
            readout.setText(self._fmt_db_readout(iv))

    def _on_eq_save_preset(self):
        """Prompt for a preset name; if it already exists (built-in
        or user), refuse with a message. Save to eq_user_presets,
        refresh the combo, select the new entry."""
        from jellytoast.eq_presets import PRESETS
        from jellytoast.frosted_dialog import frosted_confirm, frosted_warning

        name, ok = QInputDialog.getText(
            self, "Save preset", "Preset name:"
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        if name in PRESETS or name == "Custom":
            frosted_warning(
                self,
                "Name taken",
                f"'{name}' is a built-in preset name. Pick a different name.",
            )
            return
        existing = self.s.eq_user_presets
        if name in existing:
            if not frosted_confirm(
                self,
                "Overwrite preset",
                f"User preset '{name}' already exists. Overwrite?",
                confirm_text="Overwrite",
            ):
                return
        preamp = float(self._eq_sliders[0].value())
        bands = [float(s.value()) for s in self._eq_sliders[1:]]
        existing[name] = {"preamp": preamp, "bands": bands}
        self.s.eq_user_presets = existing
        self._populate_eq_preset_combo()
        self._select_combo_by_data(self._eq_preset_combo, name)
        self.s.eq_preset = name
        self._refresh_eq_enabled_state()

    def _on_eq_delete_preset(self):
        """Delete the current preset from eq_user_presets. Built-ins
        can't be deleted (Delete button greys out for them via
        _current_preset_is_user)."""
        from jellytoast.frosted_dialog import frosted_confirm

        name = self._eq_preset_combo.currentData() or ""
        existing = self.s.eq_user_presets
        if name not in existing:
            return
        if not frosted_confirm(
            self,
            "Delete preset",
            f"Delete user preset '{name}'?",
            confirm_text="Delete",
            destructive=True,
        ):
            return
        del existing[name]
        self.s.eq_user_presets = existing
        self._populate_eq_preset_combo()
        # Fall back to Flat after a delete so the section reads
        # cleanly. Slider values stay as-is; user can dial back.
        self._select_combo_by_data(self._eq_preset_combo, "Custom")
        self.s.eq_preset = "Custom"
        self._refresh_eq_enabled_state()

    def _emit_eq_changed(self):
        try:
            PlayerBus.get().eq_changed.emit(
                bool(self.s.eq_enabled), list(self.s.eq_bands)
            )
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # Double-click on an EQ slider → snap to 0 dB. Qt's QSlider
        # has no double-click signal, so we install ourselves as an
        # event filter on each slider.
        try:
            from PySide6.QtCore import QEvent

            sliders = getattr(self, "_eq_sliders", None)
            if (
                sliders is not None
                and obj in sliders
                and event.type() == QEvent.Type.MouseButtonDblClick
            ):
                obj.setValue(0)
                # setValue fires valueChanged → triggers the settle
                # timer → persists + emits on its own.
                return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ── Dialog-helper reimplementations ─────────────────────────────────
    # The page is self-contained (mirrors settings_colors_page) and
    # reuses these trivial builders rather than reaching back into the
    # dialog. Kept byte-identical to SettingsDialog's versions.
    def _section_header(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        return label

    def _select_combo_by_data(self, combo: QComboBox, key: str):
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def reapply_accent(self):
        """Re-stamp the EQ slider handles' accent-baked QSS. Called from
        SettingsDialog._reapply_dialog_accent_styling on theme_changed
        (the dialog used to iterate _eq_sliders directly)."""
        for s in getattr(self, "_eq_sliders", ()):
            s.setStyleSheet(self._eq_slider_qss())
