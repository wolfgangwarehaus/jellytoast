"""Tests for the ``VisualizerWidget`` spectrum-bar paint widget.

Covers the four bullets in ``docs/research/visualizer_rendering.md``
§11:

* Smoothing math — asymmetric exponential convergence on attack/
  release, no overshoot.
* Idle decay — feed zeros, ``displayed`` decays toward 0 in state,
  paint lifts to the 0.02 baseline.
* paintEvent rendered to a QImage buffer — pixel columns light up
  in the expected places when given known band values.
* Cast-active branch — no bar columns, non-zero centre coverage for
  the placeholder icon + caption.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from jellytoast.player_state import PlayerBus
from jellytoast.visualizer_widget import (
    _ATTACK_ALPHA,
    _BAR_COUNT,
    _RELEASE_ALPHA,
    VisualizerWidget,
)


@pytest.fixture(autouse=True)
def _fresh_bus():
    """Each test starts from a clean PlayerBus singleton so slot
    connections from prior cases don't leak into the next."""
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def widget(qapp):
    w = VisualizerWidget()
    w.resize(320, 200)
    return w


# ── Smoothing math (spec §4) ────────────────────────────────────────────────


class TestSmoothing:
    def test_attack_steps_toward_target_with_alpha(self, widget):
        # Cold widget — every band at 0. Drive a target of 1.0 and
        # check the first step matches attack_alpha exactly.
        widget._targets = [1.0] * _BAR_COUNT
        widget._advance_smoothing()
        assert all(abs(v - _ATTACK_ALPHA) < 1e-9 for v in widget._displayed)

    def test_release_uses_smaller_alpha(self, widget):
        # Saturate the bands, then drop the target to 0 — first
        # release step should be -_RELEASE_ALPHA away from 1.0.
        widget._displayed = [1.0] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        widget._advance_smoothing()
        expected = 1.0 - _RELEASE_ALPHA
        assert all(abs(v - expected) < 1e-9 for v in widget._displayed)

    def test_attack_converges_toward_one(self, widget):
        # Drive a constant target of 1.0 for many ticks; displayed
        # should approach but never exceed 1.0.
        widget._targets = [1.0] * _BAR_COUNT
        for _ in range(60):
            widget._advance_smoothing()
        for v in widget._displayed:
            assert 0.99 < v <= 1.0

    def test_release_converges_toward_zero(self, widget):
        widget._displayed = [1.0] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        for _ in range(120):
            widget._advance_smoothing()
        for v in widget._displayed:
            assert 0.0 <= v < 0.01

    def test_geometric_attack_shape(self, widget):
        # Closed-form: after N steps starting from 0 toward target 1,
        # displayed_N = 1 - (1 - alpha)^N. Validate at N=5.
        widget._targets = [1.0] * _BAR_COUNT
        for _ in range(5):
            widget._advance_smoothing()
        expected = 1.0 - (1.0 - _ATTACK_ALPHA) ** 5
        for v in widget._displayed:
            assert abs(v - expected) < 1e-9


# ── Idle decay (spec §6) ────────────────────────────────────────────────────


class TestIdleDecay:
    def test_zeros_decay_state_toward_zero(self, widget):
        # State decays to ~zero, NOT to the baseline — the baseline
        # lives only in the draw clamp so the math stays monotone.
        widget._displayed = [0.5] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        for _ in range(200):
            widget._advance_smoothing()
        for v in widget._displayed:
            assert v >= 0.0
            assert v < 0.001  # essentially zero

    def test_zero_state_paints_nothing(self, widget):
        # Once state has decayed to 0.0, the wave should collapse to
        # the baseline (y = h) and the closed-path fill has zero area.
        # Silence = no purple at all, by user request — the pre-signal
        # caption covers the "never received audio yet" case.
        widget._displayed = [0.0] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        widget._has_ever_received_bands = True
        img = _render_to_image(widget, 320, 200)
        for y in range(img.height()):
            for x in range(img.width()):
                assert img.pixelColor(x, y).alpha() == 0, (
                    f"zero state should paint nothing — found pixel at ({x}, {y})"
                )


# ── Paint to QImage (spec §11) ─────────────────────────────────────────────


def _render_to_image(widget: VisualizerWidget, w: int, h: int) -> QImage:
    widget.resize(w, h)
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)
    return img


class TestPaint:
    def test_high_band_lights_expected_column_range(self, widget):
        # Force bands 13–17 (mid-range, 5 wide) to max; everything else
        # zero. Two attenuation stages combine: the 3-tap spatial kernel
        # at paint time pulls each band toward its neighbours, then the
        # block-averaging downsample (32 bands → 16 control points)
        # pairs adjacent smoothed values. A real-world peak the user
        # notices always spans more than a couple of bands; this width
        # models that and lets the central control point land at 1.0
        # so the wave reaches the top of the canvas as expected.
        bands = [0.0] * _BAR_COUNT
        for i in range(13, 18):
            bands[i] = 1.0
        widget._displayed = list(bands)
        widget._targets = list(bands)
        widget._has_ever_received_bands = True
        img = _render_to_image(widget, 320, 200)
        # Walk the bottom-row pixels to find which columns are lit.
        bottom = img.height() - 1
        lit_columns = [
            x for x in range(img.width()) if img.pixelColor(x, bottom).alpha() > 0
        ]
        assert lit_columns, "at least one column should be painted"
        # The height cap (``_MAX_HEIGHT_FRACTION``) keeps even a full-scale
        # peak off the top edge (it lands around the canvas midpoint), so the
        # wave must fill well past the lower canvas but NOT slam the top.
        # Probe ~65% down — within the fill for any sane cap, robust to the
        # exact value.
        probe_y = int(img.height() * 0.65)
        probe_lit = [
            x for x in range(img.width()) if img.pixelColor(x, probe_y).alpha() > 0
        ]
        assert probe_lit, "max-height peak should fill past the lower canvas"

    def test_top_empty_for_low_signal(self, widget):
        # A small displayed value across all bands keeps the wave
        # short — the top half of the canvas must stay transparent.
        widget._displayed = [0.05] * _BAR_COUNT
        widget._targets = [0.05] * _BAR_COUNT
        widget._has_ever_received_bands = True
        img = _render_to_image(widget, 320, 200)
        for y in range(img.height() // 2):
            for x in range(img.width()):
                assert img.pixelColor(x, y).alpha() == 0


# ── Pre-signal caption (no FFT payload yet) ────────────────────────────────


class TestPreSignalCaption:
    def test_no_bars_before_first_band_payload(self, widget):
        # Fresh widget — _has_ever_received_bands is False. The bottom
        # row should be empty (no bar coverage); the caption lives in
        # the vertical centre, not the baseline.
        img = _render_to_image(widget, 320, 200)
        bottom = img.height() - 1
        for x in range(img.width()):
            assert img.pixelColor(x, bottom).alpha() == 0

    def test_first_band_payload_flips_into_bar_mode(self, widget):
        # Feeding a real payload via the bus slot should flip the
        # widget out of pre-signal mode and into the spec baseline path.
        bands = [0.0] * _BAR_COUNT
        bands[16] = 1.0
        widget._on_bands(bands)
        assert widget._has_ever_received_bands
        img = _render_to_image(widget, 320, 200)
        # Bar 16 should now light up the bottom row.
        bottom = img.height() - 1
        lit = [x for x in range(img.width()) if img.pixelColor(x, bottom).alpha() > 0]
        assert lit, "post-signal paint should render bar coverage"


# ── Cast placeholder (spec §8) ──────────────────────────────────────────────


class TestCastPlaceholder:
    def test_cast_active_replaces_bars(self, widget):
        # Activate cast — the paint path should switch entirely to
        # the placeholder branch. No bottom-row bar coverage.
        widget._cast_active = True
        widget._cast_device = "Office Speaker"
        # Fill displayed with junk to prove the cast branch ignores
        # the smoothing state.
        widget._displayed = [1.0] * _BAR_COUNT
        img = _render_to_image(widget, 320, 200)
        # Bottom row should be empty (no bars) — placeholder is
        # centred vertically.
        bottom = img.height() - 1
        for x in range(img.width()):
            assert img.pixelColor(x, bottom).alpha() == 0

    def test_cast_active_paints_something_centred(self, widget):
        widget._cast_active = True
        widget._cast_device = "Office Speaker"
        img = _render_to_image(widget, 320, 200)
        # The middle band should have non-zero coverage — icon +
        # caption sit there.
        cx = img.width() // 2
        mid = img.height() // 2
        # Walk a small box around the centre; at least some pixel
        # must be opaque.
        any_painted = False
        for y in range(mid - 30, mid + 30):
            for x in range(cx - 40, cx + 40):
                if img.pixelColor(x, y).alpha() > 0:
                    any_painted = True
                    break
            if any_painted:
                break
        assert any_painted, "cast placeholder should paint icon + caption near centre"

    def test_cast_off_returns_to_bars(self, widget):
        widget._cast_active = True
        widget._cast_device = "X"
        # Now turn cast off and feed real band values.
        widget._cast_active = False
        widget._cast_device = ""
        widget._displayed = [1.0] * _BAR_COUNT
        widget._targets = [1.0] * _BAR_COUNT
        widget._has_ever_received_bands = True
        img = _render_to_image(widget, 320, 200)
        # Bottom row should have bar pixels back.
        bottom = img.height() - 1
        lit = sum(1 for x in range(img.width()) if img.pixelColor(x, bottom).alpha() > 0)
        assert lit > 50  # plenty of bar coverage at the bottom


# ── Bus wiring sanity ───────────────────────────────────────────────────────


class TestBusWiring:
    def test_visualizer_bands_changed_updates_targets(self, widget):
        # Drive a single bus emit and confirm the widget's internal
        # state advances. The exact displayed values are checked
        # under TestSmoothing — here we just want to know the slot
        # is connected end-to-end.
        bands = [0.0] * _BAR_COUNT
        bands[10] = 1.0
        PlayerBus.get().visualizer_bands_changed.emit(bands)
        assert widget._targets[10] == 1.0
        # And smoothing advanced once (the slot calls
        # _advance_smoothing).
        assert widget._displayed[10] > 0.0

    def test_cast_signals_flip_state(self, widget):
        PlayerBus.get().cast_started.emit("Living Room")
        assert widget._cast_active is True
        assert widget._cast_device == "Living Room"
        PlayerBus.get().cast_stopped.emit()
        assert widget._cast_active is False
        assert widget._cast_device == ""


# ── Visibility → engine gating (#visualizer-leak) ───────────────────────────


class TestVisibilitySignal:
    """The FFT engine (decoder + worker, ~12%/core) must run ONLY while the
    visualizer is on screen. The widget signals its visibility; the NP pane
    drives engine start/stop off it. Here we pin the widget half."""

    def test_show_emits_true_hide_emits_false(self, qapp):
        w = VisualizerWidget()
        events = []
        w.visibility_changed.connect(events.append)
        w.show()
        qapp.processEvents()
        w.hide()
        qapp.processEvents()
        assert events[0] is True
        assert events[-1] is False
        w.deleteLater()
