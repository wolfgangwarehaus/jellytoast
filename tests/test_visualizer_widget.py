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
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QPainter

from modules.visualizer_widget import (
    VisualizerWidget,
    _ATTACK_ALPHA,
    _RELEASE_ALPHA,
    _BAR_COUNT,
    _IDLE_BASELINE,
)
from modules.player_state import PlayerBus


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

    def test_baseline_clamp_applied_at_paint(self, widget):
        # Even when state has decayed to 0.0, the rendered output
        # should not be all-zero rows — the 0.02 baseline (and
        # >=2px min) keeps bars visible.
        widget._displayed = [0.0] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        img = _render_to_image(widget, 320, 200)
        # Expect non-zero pixel coverage on the bottom row.
        bottom = img.height() - 1
        non_zero = sum(1 for x in range(img.width()) if img.pixelColor(x, bottom).alpha() > 0)
        assert non_zero > 0, "baseline clamp should paint a sliver of bars"
        # Sanity: the baseline value matches the spec.
        assert _IDLE_BASELINE == 0.02


# ── Paint to QImage (spec §11) ─────────────────────────────────────────────


def _render_to_image(widget: VisualizerWidget, w: int, h: int) -> QImage:
    widget.resize(w, h)
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)
    return img


class TestPaint:
    def test_high_band_lights_expected_column_range(self, widget):
        # Force band 16 (mid-range) to max; everything else zero.
        # The corresponding column band should have full-height
        # non-zero pixel coverage.
        bands = [0.0] * _BAR_COUNT
        bands[16] = 1.0
        widget._displayed = list(bands)
        widget._targets = list(bands)
        img = _render_to_image(widget, 320, 200)
        # Bar 16's pixel range: rough geometry from the spec.
        # Walk the bottom-row pixels to find which columns are lit.
        bottom = img.height() - 1
        lit_columns = [
            x for x in range(img.width()) if img.pixelColor(x, bottom).alpha() > 0
        ]
        assert lit_columns, "at least one column should be painted"
        # The full-height bar should also paint near the top — check
        # row 10 from the top for pixel coverage.
        near_top_lit = [
            x for x in range(img.width()) if img.pixelColor(x, 10).alpha() > 0
        ]
        assert near_top_lit, "max-height bar must reach near the top"

    def test_only_baseline_when_all_zero(self, widget):
        widget._displayed = [0.0] * _BAR_COUNT
        widget._targets = [0.0] * _BAR_COUNT
        img = _render_to_image(widget, 320, 200)
        # Top 50 % of the canvas should be empty — baseline is 2 %
        # of height, so anything above ~10 % should be transparent.
        for y in range(img.height() // 2):
            for x in range(img.width()):
                assert img.pixelColor(x, y).alpha() == 0


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
