"""Tests for the AutoEQ ParametricEQ.txt parser + parametric formatters
(EQ T3a — see ``docs/research/eq_dsp_v2.md`` §6).

AutoEQ profiles look like this (autoeq.app output):

    Preamp: -6.6 dB
    Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41
    Filter 2: ON PK Fc 195 Hz Gain -2.3 dB Q 0.71
    Filter 3: ON LSC Fc 50 Hz Gain 3 dB Q 0.71

The parser keeps PK (peaking) filters, records non-PK in ``skipped``,
and converts Q → Hz-bandwidth via the standard ``w = f / Q`` rule.
"""

import json

import pytest

from modules.eq_presets import (
    AUTOEQ_TYPE_LOW_SHELF,
    BAND_COUNT,
    BAND_FREQUENCIES,
    FIREQUALIZER_DELAY_S,
    GAIN_LIMIT_DB,
    build_default_parametric_bands,
    format_anequalizer_parametric,
    format_firequalizer_parametric,
    parse_autoeq_profile,
    q_to_width_hz,
)

# ── Parser ──────────────────────────────────────────────────────────────


class TestParseAutoEqProfile:
    def test_empty_string_returns_empty_profile(self):
        result = parse_autoeq_profile("")
        assert result == {"preamp_db": 0.0, "bands": [], "skipped": []}

    def test_preamp_only(self):
        result = parse_autoeq_profile("Preamp: -6.6 dB")
        assert result["preamp_db"] == pytest.approx(-6.6)
        assert result["bands"] == []
        assert result["skipped"] == []

    def test_single_peaking_filter(self):
        result = parse_autoeq_profile(
            "Filter 1: ON PK Fc 1000 Hz Gain 3 dB Q 1.41"
        )
        assert len(result["bands"]) == 1
        b = result["bands"][0]
        assert b["f"] == 1000
        # Q=1.41 → w = 1000 / 1.41 ≈ 709.22
        assert b["w"] == pytest.approx(1000 / 1.41)
        assert b["g"] == pytest.approx(3.0)
        assert b["t"] == 0  # Butterworth peaking

    def test_full_profile_with_preamp_and_filters(self):
        text = """
Preamp: -6.6 dB
Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41
Filter 2: ON PK Fc 195 Hz Gain -2.3 dB Q 0.71
Filter 3: ON PK Fc 2200 Hz Gain 4.0 dB Q 1.0
"""
        result = parse_autoeq_profile(text)
        assert result["preamp_db"] == pytest.approx(-6.6)
        assert len(result["bands"]) == 3
        assert [b["f"] for b in result["bands"]] == [105, 195, 2200]
        assert [b["g"] for b in result["bands"]] == [
            pytest.approx(5.5),
            pytest.approx(-2.3),
            pytest.approx(4.0),
        ]
        # No skipped filters when every line is PK + ON.
        assert result["skipped"] == []

    def test_shelf_filters_recorded_in_skipped(self):
        text = """
Filter 1: ON LSC Fc 50 Hz Gain 3 dB Q 0.71
Filter 2: ON HSC Fc 10000 Hz Gain -2 dB Q 0.71
Filter 3: ON PK Fc 1000 Hz Gain 2 dB Q 1.0
"""
        result = parse_autoeq_profile(text)
        # Only the PK band makes it through.
        assert len(result["bands"]) == 1
        assert result["bands"][0]["f"] == 1000
        # Both shelves recorded as skipped.
        assert len(result["skipped"]) == 2
        types = [s["type"] for s in result["skipped"]]
        assert AUTOEQ_TYPE_LOW_SHELF in types
        assert "HSC" in types
        # Each carries a human-readable reason and the freq it
        # would have peaked at.
        for s in result["skipped"]:
            assert "shelf" in s["reason"].lower()
            assert s["freq"] > 0

    def test_off_filters_recorded_in_skipped(self):
        text = """
Filter 1: OFF PK Fc 1000 Hz Gain 3 dB Q 1.41
Filter 2: ON PK Fc 2000 Hz Gain -1 dB Q 1.0
"""
        result = parse_autoeq_profile(text)
        assert len(result["bands"]) == 1
        assert result["bands"][0]["f"] == 2000
        assert len(result["skipped"]) == 1
        assert "off" in result["skipped"][0]["reason"].lower()

    def test_case_insensitive(self):
        # autoeq.app emits title-case; some tools emit upper or lower.
        # The parser should tolerate all common variants.
        text = "filter 1: on pk fc 1000 hz gain 2.5 db q 1.0"
        result = parse_autoeq_profile(text)
        assert len(result["bands"]) == 1
        assert result["bands"][0]["g"] == pytest.approx(2.5)

    def test_lenient_to_unrecognised_lines(self):
        text = """
# my custom comment
Equalizer APO header text
Preamp: -3 dB
Filter 1: ON PK Fc 500 Hz Gain 4 dB Q 1.0
random gibberish 12345
"""
        result = parse_autoeq_profile(text)
        # The two real lines parse; the rest are silently skipped.
        assert result["preamp_db"] == pytest.approx(-3.0)
        assert len(result["bands"]) == 1
        assert result["bands"][0]["f"] == 500

    def test_gain_clamped_to_envelope(self):
        # AutoEQ profiles occasionally suggest huge boosts; clamp to
        # the same ±12 dB envelope as the graphic-EQ surface.
        result = parse_autoeq_profile(
            "Filter 1: ON PK Fc 1000 Hz Gain 99 dB Q 1.0"
        )
        assert result["bands"][0]["g"] == pytest.approx(GAIN_LIMIT_DB)
        result2 = parse_autoeq_profile(
            "Filter 1: ON PK Fc 1000 Hz Gain -99 dB Q 1.0"
        )
        assert result2["bands"][0]["g"] == pytest.approx(-GAIN_LIMIT_DB)

    def test_q_value_floored_for_safety(self):
        # Q=0 would divide-by-zero. The parser's q_to_width_hz clamps
        # to a minimum of 0.1 — the resulting filter is wide, not
        # broken.
        result = parse_autoeq_profile(
            "Filter 1: ON PK Fc 1000 Hz Gain 1 dB Q 0"
        )
        assert len(result["bands"]) == 1
        # w = 1000 / 0.1 = 10000
        assert result["bands"][0]["w"] == pytest.approx(10000.0)


# ── q_to_width_hz ───────────────────────────────────────────────────────


class TestQToWidthHz:
    def test_standard_relation(self):
        # Q = f / w  ⇒  w = f / Q.
        assert q_to_width_hz(1000, 1.0) == pytest.approx(1000.0)
        assert q_to_width_hz(1000, 2.0) == pytest.approx(500.0)
        assert q_to_width_hz(1000, 0.5) == pytest.approx(2000.0)

    def test_zero_q_safe(self):
        # Defence-in-depth: a malformed profile mustn't divide by zero.
        assert q_to_width_hz(1000, 0) == pytest.approx(10000.0)

    def test_negative_q_safe(self):
        # Negative Q is meaningless; clamp identically to zero.
        result = q_to_width_hz(1000, -1.0)
        assert result == pytest.approx(10000.0)


# ── format_anequalizer_parametric ───────────────────────────────────────


class TestFormatAnequalizerParametric:
    def test_empty_bands_returns_empty_string(self):
        # Caller's signal that no parametric filter should be applied.
        # An empty ``anequalizer=`` is a ffmpeg syntax error.
        assert format_anequalizer_parametric([]) == ""

    def test_single_band_stereo(self):
        bands = [{"f": 1000, "w": 707.0, "g": 3.0, "t": 0}]
        result = format_anequalizer_parametric(bands, channel_count=2)
        assert result.startswith("anequalizer=")
        entries = result[len("anequalizer="):].split("|")
        # Stereo → 2 entries, both same band.
        assert len(entries) == 2
        assert "c0 f=1000 w=707 g=3 t=0" in result
        assert "c1 f=1000 w=707 g=3 t=0" in result

    def test_autoeq_profile_round_trip(self):
        # End-to-end: parse a real-world-ish profile, push through the
        # formatter, check the result reflects the parsed centres + Qs.
        text = """
Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41
Filter 2: ON PK Fc 2200 Hz Gain -3 dB Q 1.0
"""
        profile = parse_autoeq_profile(text)
        result = format_anequalizer_parametric(
            profile["bands"], channel_count=2
        )
        assert "c0 f=105 " in result
        assert "c0 f=2200 " in result
        assert "g=5.5" in result
        assert "g=-3 " in result
        # Q=1.41 → w = 105 / 1.41 ≈ 74.46. Integer-ish (gets the :g
        # short-form treatment).
        assert "w=" in result

    def test_custom_filter_type_preserved(self):
        # We use t=0 (Butterworth) exclusively, but the formatter
        # carries through whatever the band specifies — future-proof
        # for if T3 wants to expose other types.
        bands = [{"f": 500, "w": 500, "g": 1.0, "t": 1}]
        result = format_anequalizer_parametric(bands, channel_count=1)
        assert "t=1" in result


# ── format_firequalizer_parametric ──────────────────────────────────────


class TestFormatFirequalizerParametric:
    def test_empty_bands_returns_empty_string(self):
        assert format_firequalizer_parametric([]) == ""

    def test_single_band(self):
        bands = [{"f": 1000, "w": 707, "g": 3.0, "t": 0}]
        result = format_firequalizer_parametric(bands)
        assert result.startswith("firequalizer=")
        assert "entry(1000,3)" in result
        assert "zero_phase=on" in result
        assert f"delay={FIREQUALIZER_DELAY_S}" in result

    def test_autoeq_profile_round_trip(self):
        text = """
Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41
Filter 2: ON PK Fc 2200 Hz Gain -3 dB Q 1.0
"""
        profile = parse_autoeq_profile(text)
        result = format_firequalizer_parametric(profile["bands"])
        # Centres + gains land in the gain_entry list. Q isn't used by
        # firequalizer (it interpolates between entries).
        assert "entry(105,5.5)" in result
        assert "entry(2200,-3)" in result


# ── build_default_parametric_bands ──────────────────────────────────────


class TestBuildDefaultParametricBands:
    def test_constructs_iso_octave_centres(self):
        # Flat preset → 10 bands at the ISO frequencies, all g=0.
        bands = build_default_parametric_bands([0.0] * BAND_COUNT)
        assert len(bands) == BAND_COUNT
        for band, freq in zip(bands, BAND_FREQUENCIES, strict=False):
            assert band["f"] == freq
            assert band["w"] == float(freq)
            assert band["g"] == 0.0
            assert band["t"] == 0

    def test_gains_propagate(self):
        gains = [1.0, -2.0, 3.0, -4.0, 5.0, -6.0, 7.0, -8.0, 9.0, -10.0]
        bands = build_default_parametric_bands(gains)
        assert [b["g"] for b in bands] == gains

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            build_default_parametric_bands([0.0] * 5)


# ── apply_eq with AutoEQ profile takes precedence over graphic ─────────


class TestApplyEqWithAutoEqProfile:
    """apply_eq must use the parametric path when an AutoEQ profile is
    loaded; the 10-slider graphic gains are ignored in that mode."""

    def _make_backend(self, profile_dict, *, linear_phase=False, preamp=0.0):
        from modules.player_backend import MpvController

        class _FakeMpv(dict):
            pass

        class _FakeSettings:
            def __init__(self):
                self.eq_preamp = preamp
                self.eq_linear_phase = linear_phase
                self.eq_autoeq_profile_json = json.dumps(profile_dict)

        backend = type("B", (), {})()
        backend._mpv = _FakeMpv()
        backend.settings = _FakeSettings()
        backend._last_eq_state = None
        backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
        # apply_eq delegates chain-building to these helpers.
        backend._eq_af_chain = MpvController._eq_af_chain.__get__(backend, MpvController)
        backend._eq_channel_count = MpvController._eq_channel_count.__get__(
            backend, MpvController
        )
        return backend

    def test_autoeq_anequalizer_path(self):
        profile = {
            "preamp_db": -2.0,
            "bands": [
                {"f": 1000, "w": 700, "g": 5.0, "t": 0},
                {"f": 4000, "w": 2000, "g": -3.0, "t": 0},
            ],
            "skipped": [],
        }
        backend = self._make_backend(profile)
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert "anequalizer=" in chain
        # Profile centres land in the filter (not the ISO 31/62/…
        # frequencies the graphic slider supplies).
        assert "c0 f=1000" in chain
        assert "c0 f=4000" in chain
        assert "f=31" not in chain  # graphic-mode default ISO freq

    def test_autoeq_firequalizer_path_when_linear_phase_on(self):
        profile = {
            "preamp_db": 0.0,
            "bands": [{"f": 800, "w": 800, "g": 2.0, "t": 0}],
            "skipped": [],
        }
        backend = self._make_backend(profile, linear_phase=True)
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert "firequalizer=" in chain
        assert "entry(800,2)" in chain

    def test_autoeq_preamp_adds_to_user_preamp(self):
        # Profile preamp -6 + user preamp -2 → volume=-8dB on chain.
        profile = {
            "preamp_db": -6.0,
            "bands": [{"f": 1000, "w": 1000, "g": 0.0, "t": 0}],
            "skipped": [],
        }
        backend = self._make_backend(profile, preamp=-2.0)
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert chain.startswith("volume=-8dB,")

    def test_malformed_profile_falls_back_to_graphic(self):
        """A broken JSON string must not break audio — apply_eq falls
        back to the 10-band graphic gains and audio keeps playing."""
        from modules.player_backend import MpvController

        class _FakeMpv(dict):
            pass

        class _FakeSettings:
            eq_preamp = 0.0
            eq_linear_phase = False
            eq_autoeq_profile_json = "not even close to JSON {{{"

        backend = type("B", (), {})()
        backend._mpv = _FakeMpv()
        backend.settings = _FakeSettings()
        backend._last_eq_state = None
        backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
        backend._eq_af_chain = MpvController._eq_af_chain.__get__(backend, MpvController)
        backend._eq_channel_count = MpvController._eq_channel_count.__get__(
            backend, MpvController
        )
        backend.apply_eq(True, [1.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        # Graphic-mode chain → ISO frequencies present, not arbitrary
        # centres.
        assert "anequalizer=" in chain
        assert "c0 f=31" in chain

    def test_empty_profile_json_uses_graphic_path(self):
        """Default state — no profile loaded; the graphic-EQ surface
        is the active path. Same chain shape as pre-T3a."""
        from modules.player_backend import MpvController

        class _FakeMpv(dict):
            pass

        class _FakeSettings:
            eq_preamp = 0.0
            eq_linear_phase = False
            eq_autoeq_profile_json = ""

        backend = type("B", (), {})()
        backend._mpv = _FakeMpv()
        backend.settings = _FakeSettings()
        backend._last_eq_state = None
        backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
        backend._eq_af_chain = MpvController._eq_af_chain.__get__(backend, MpvController)
        backend._eq_channel_count = MpvController._eq_channel_count.__get__(
            backend, MpvController
        )
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert "anequalizer=" in chain
        assert "c0 f=31" in chain
