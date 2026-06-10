"""Tests for the EQ T3b curve-editor band math
(``jellytoast.eq_curve_editor``).

The widget itself is hard to unit-test without a QApplication +
synthetic mouse events; the bulk of the testable surface is the pure
functions that map (freq, dB) ↔ (x, y) and compute cumulative
response. The widget delegates to these on every paint + mouse event,
so getting them right gets the widget right.
"""

import math

import pytest

from jellytoast.eq_curve_editor import (
    DB_MAX,
    DB_MIN,
    FREQ_MAX_HZ,
    FREQ_MIN_HZ,
    MAX_BANDS,
    band_contribution_db,
    cumulative_response_db,
    db_to_y,
    freq_to_x,
    width_to_q,
    x_to_freq,
    y_to_db,
)
from jellytoast.eq_presets import q_to_width_hz

# ── Coordinate transforms ─────────────────────────────────────────────


class TestFreqToX:
    def test_min_freq_maps_to_left_padding(self):
        assert freq_to_x(FREQ_MIN_HZ, width=400, pad_l=32, pad_r=8) == pytest.approx(32)

    def test_max_freq_maps_to_right_edge(self):
        assert freq_to_x(FREQ_MAX_HZ, width=400, pad_l=32, pad_r=8) == pytest.approx(392)

    def test_log_centre_lands_at_pixel_centre(self):
        # Geometric mean of FREQ_MIN_HZ × FREQ_MAX_HZ should map to the
        # midpoint of the plot area — log-axis correctness check.
        centre = math.sqrt(FREQ_MIN_HZ * FREQ_MAX_HZ)
        x = freq_to_x(centre, width=400, pad_l=32, pad_r=8)
        plot_w = 400 - 32 - 8
        midpoint = 32 + plot_w / 2
        assert x == pytest.approx(midpoint, abs=0.5)

    def test_below_min_clamps_to_left(self):
        assert freq_to_x(5.0, width=400, pad_l=32, pad_r=8) == pytest.approx(32)

    def test_above_max_clamps_to_right(self):
        assert freq_to_x(40000, width=400, pad_l=32, pad_r=8) == pytest.approx(392)


class TestXToFreq:
    def test_left_padding_yields_min_freq(self):
        assert x_to_freq(32, width=400, pad_l=32, pad_r=8) == pytest.approx(FREQ_MIN_HZ)

    def test_right_edge_yields_max_freq(self):
        assert x_to_freq(392, width=400, pad_l=32, pad_r=8) == pytest.approx(FREQ_MAX_HZ)

    def test_round_trip(self):
        # Mid-band frequencies should round-trip through freq_to_x →
        # x_to_freq with very small floating-point error.
        for f in (60.0, 250.0, 1000.0, 4000.0, 12000.0):
            x = freq_to_x(f, width=400, pad_l=32, pad_r=8)
            f_back = x_to_freq(x, width=400, pad_l=32, pad_r=8)
            assert f_back == pytest.approx(f, rel=1e-6)

    def test_out_of_range_clamps(self):
        # Outside the plot area, results clamp to the endpoints —
        # protects the host from negative or out-of-range freqs when
        # the user drags a node past the canvas edge.
        below = x_to_freq(0, width=400, pad_l=32, pad_r=8)
        above = x_to_freq(500, width=400, pad_l=32, pad_r=8)
        assert below == pytest.approx(FREQ_MIN_HZ)
        assert above == pytest.approx(FREQ_MAX_HZ)


class TestDbToY:
    def test_max_db_lands_at_top(self):
        assert db_to_y(DB_MAX, height=120, pad_t=10, pad_b=18) == pytest.approx(10)

    def test_min_db_lands_at_bottom(self):
        # height=120, pad_t=10, pad_b=18 → plot_h=92, so DB_MIN sits at
        # pad_t + plot_h = 102.
        assert db_to_y(DB_MIN, height=120, pad_t=10, pad_b=18) == pytest.approx(102)

    def test_zero_db_centred(self):
        y = db_to_y(0, height=120, pad_t=10, pad_b=18)
        plot_h = 120 - 10 - 18
        assert y == pytest.approx(10 + plot_h / 2)

    def test_clamps_out_of_range(self):
        # Out-of-range dB values clamp to the plot edges so a malformed
        # band can't paint outside the canvas.
        assert db_to_y(99, height=120, pad_t=10, pad_b=18) == pytest.approx(10)
        assert db_to_y(-99, height=120, pad_t=10, pad_b=18) == pytest.approx(102)


class TestYToDb:
    def test_round_trip(self):
        for db in (-12.0, -6.0, -3.0, 0.0, 3.0, 6.0, 12.0):
            y = db_to_y(db, height=120, pad_t=10, pad_b=18)
            db_back = y_to_db(y, height=120, pad_t=10, pad_b=18)
            assert db_back == pytest.approx(db, abs=1e-6)


# ── Band contribution + cumulative response ──────────────────────────


class TestBandContribution:
    def test_zero_gain_contributes_zero(self):
        # Optimisation path — bands with g=0 short-circuit. Verify the
        # contract holds even at the band's own centre frequency.
        b = {"f": 1000, "w": 1000, "g": 0.0, "t": 0}
        assert band_contribution_db(b, 1000) == 0.0
        assert band_contribution_db(b, 500) == 0.0

    def test_peak_at_band_centre(self):
        # Bell shape: maximum at f_c, decays away. At the centre the
        # contribution equals the band's gain (×1 = the Gaussian's
        # maximum).
        b = {"f": 1000, "w": 1000, "g": 3.0, "t": 0}
        assert band_contribution_db(b, 1000) == pytest.approx(3.0, abs=1e-6)

    def test_falls_off_away_from_centre(self):
        b = {"f": 1000, "w": 1000, "g": 6.0, "t": 0}
        # Several octaves away: contribution should be near zero.
        far = band_contribution_db(b, 16000)
        assert abs(far) < 0.5

    def test_symmetric_in_log_frequency(self):
        # Bell is symmetric on the log axis: 500 Hz (1 oct below 1k)
        # contributes the same as 2000 Hz (1 oct above 1k).
        b = {"f": 1000, "w": 1000, "g": 4.0, "t": 0}
        below = band_contribution_db(b, 500)
        above = band_contribution_db(b, 2000)
        assert below == pytest.approx(above, rel=1e-6)

    def test_narrow_band_falls_off_faster(self):
        # Same centre + gain, narrower bandwidth → steeper skirt.
        wide = {"f": 1000, "w": 1000, "g": 6.0, "t": 0}
        narrow = {"f": 1000, "w": 100, "g": 6.0, "t": 0}
        # At 1 octave away, the narrow band should be much further
        # below 6 dB than the wide band.
        wide_at_2k = band_contribution_db(wide, 2000)
        narrow_at_2k = band_contribution_db(narrow, 2000)
        assert narrow_at_2k < wide_at_2k

    def test_invalid_inputs_safe(self):
        # Defence-in-depth: malformed bands shouldn't crash the paint.
        assert band_contribution_db({"f": 0, "w": 1, "g": 3, "t": 0}, 1000) == 0.0
        assert band_contribution_db({"f": 1000, "w": 0, "g": 3, "t": 0}, 1000) != 0.0
        assert band_contribution_db({"f": 1000, "w": 1000, "g": 3, "t": 0}, -1) == 0.0


class TestCumulativeResponse:
    def test_empty_band_list(self):
        assert cumulative_response_db([], 1000) == 0.0

    def test_sums_independent_bands(self):
        # Two bands far apart in frequency contribute approximately
        # additively at one of their centres (the other is negligible).
        bands = [
            {"f": 100, "w": 100, "g": 4.0, "t": 0},
            {"f": 10000, "w": 10000, "g": -3.0, "t": 0},
        ]
        at_100 = cumulative_response_db(bands, 100)
        assert at_100 == pytest.approx(4.0, abs=0.1)
        at_10k = cumulative_response_db(bands, 10000)
        assert at_10k == pytest.approx(-3.0, abs=0.1)

    def test_adjacent_bands_combine(self):
        # Two bands at nearby centres should sum constructively at the
        # midpoint between them — both contribute non-trivially.
        bands = [
            {"f": 1000, "w": 1000, "g": 3.0, "t": 0},
            {"f": 2000, "w": 2000, "g": 3.0, "t": 0},
        ]
        # Halfway between (geometric mean) — both bands above 0.
        midpoint = math.sqrt(1000 * 2000)
        assert cumulative_response_db(bands, midpoint) > 3.0


# ── T3c — width_to_q (Q recovery from anequalizer w) ──────────────────


class TestWidthToQ:
    def test_one_octave_returns_q_1(self):
        # The graphic-mode default: w = f → Q = 1.
        assert width_to_q(1000, 1000) == pytest.approx(1.0)

    def test_round_trip_with_q_to_width_hz(self):
        # Inverse of jellytoast.eq_presets.q_to_width_hz — round-tripping
        # an AutoEQ Q value through both must reproduce the original
        # within Q's clamp range.
        for q in (0.5, 0.71, 1.0, 1.41, 2.0, 4.0):
            w = q_to_width_hz(1000, q)
            q_back = width_to_q(1000, w)
            assert q_back == pytest.approx(q, abs=1e-6)

    def test_low_q_clamps(self):
        # Q < 0.1 isn't a useful peaking filter (too wide).
        assert width_to_q(1000, 100000) == pytest.approx(0.1)

    def test_high_q_clamps(self):
        # Q > 20 is a notch filter; clamp so the visualization stays
        # reasonable + the wheel can't be over-cranked.
        assert width_to_q(1000, 10) == pytest.approx(20.0)

    def test_zero_freq_safe(self):
        # Defensive — a band with f=0 mustn't divide by zero.
        assert width_to_q(0, 1000) == pytest.approx(1.0)


# ── T3c — band-count cap ──────────────────────────────────────────────


def test_max_bands_constant():
    """The soft cap is part of the public API — UI gates ``Add band``
    at this number. Researched value per docs/research/eq_dsp_v2.md
    §6 T3 is 16; locking the constant prevents accidental drift."""
    assert MAX_BANDS == 16
