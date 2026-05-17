"""Tests for ``modules.eq_presets`` — preset shape + the mpv
``anequalizer`` filter-string formatter. Qt-free, mpv-free."""

import pytest

from modules.eq_presets import (
    BAND_COUNT,
    BAND_FREQUENCIES,
    DEFAULT_PRESET,
    GAIN_LIMIT_DB,
    PRESETS,
    format_anequalizer_string,
    get_preset,
)


# ── Preset shape ────────────────────────────────────────────────────────────


class TestPresetShape:
    def test_band_count_matches_band_frequencies_length(self):
        assert BAND_COUNT == len(BAND_FREQUENCIES) == 10

    def test_band_frequencies_are_iso_octave_centres(self):
        # The exact 10-band ISO list — order matters because every
        # ``bands`` list indexes through these.
        assert BAND_FREQUENCIES == (
            31,
            62,
            125,
            250,
            500,
            1000,
            2000,
            4000,
            8000,
            16000,
        )

    def test_eight_built_in_presets(self):
        # Eight presets exactly — the research doc's v1 set. Adding
        # one without updating this test should be a deliberate
        # decision, not silent drift.
        assert len(PRESETS) == 8

    def test_every_preset_has_ten_bands(self):
        for name, bands in PRESETS.items():
            assert len(bands) == BAND_COUNT, (
                f"preset {name!r} has {len(bands)} bands, expected {BAND_COUNT}"
            )

    def test_every_preset_within_gain_limit(self):
        # Defence against typos in the preset table that would push
        # mpv into hearing-damage territory.
        for name, bands in PRESETS.items():
            for g in bands:
                assert -GAIN_LIMIT_DB <= g <= GAIN_LIMIT_DB, (
                    f"preset {name!r} has out-of-range gain {g}"
                )

    def test_default_preset_is_flat(self):
        assert DEFAULT_PRESET == "Flat"
        assert PRESETS[DEFAULT_PRESET] == [0] * BAND_COUNT


# ── get_preset ──────────────────────────────────────────────────────────────


class TestGetPreset:
    def test_known_preset_returns_band_list(self):
        rock = get_preset("Rock")
        assert len(rock) == BAND_COUNT
        # Rock has the classic smile curve — bass + treble lifted,
        # midrange cut. Sanity-check the shape without pinning every
        # value (the doc may revise).
        assert rock[0] > 0  # 31 Hz lifted
        assert rock[9] > 0  # 16 kHz lifted
        assert min(rock[2:5]) < 0  # mids cut somewhere

    def test_unknown_preset_falls_back_to_flat(self):
        # An old QSettings value that's drifted from the shipped
        # preset names degrades to bypass rather than crashing.
        assert get_preset("does-not-exist") == [0] * BAND_COUNT

    def test_returns_a_copy(self):
        # Caller mutating the result must not poison the PRESETS
        # dict for the next call.
        flat = get_preset("Flat")
        flat[0] = 99
        assert get_preset("Flat") == [0] * BAND_COUNT


# ── format_anequalizer_string ───────────────────────────────────────────────


class TestFormatAnequalizerString:
    def test_flat_preset_yields_well_formed_filter(self):
        # Decision: Flat (all zeros) still returns a fully-formed
        # filter string rather than an empty string. Callers that
        # want "bypass" should disable the filter via ``apply_eq``;
        # the formatter's job is to produce a valid filter spec for
        # whatever band gains it's given. Keeps the calling convention
        # uniform — the output type doesn't depend on the values.
        result = format_anequalizer_string([0] * BAND_COUNT)
        assert result.startswith("anequalizer=")
        # Ten pipe-separated bands → nine pipes inside the value.
        value = result.split("=", 1)[1]
        assert value.count("|") == BAND_COUNT - 1
        # Every band carries g=0
        for entry in value.split("|"):
            assert " g=0 " in entry or entry.endswith(" g=0 t=0")

    def test_rock_preset_contains_all_ten_bands_in_order(self):
        result = format_anequalizer_string(PRESETS["Rock"])
        value = result.split("=", 1)[1]
        entries = value.split("|")
        assert len(entries) == BAND_COUNT
        # Each entry must reference the matching frequency at the
        # matching position — guards against the formatter ever
        # silently re-ordering or duplicating bands.
        for i, (entry, freq) in enumerate(zip(entries, BAND_FREQUENCIES)):
            assert f"f={freq} " in entry, f"band {i}: entry {entry!r} missing f={freq}"

    def test_rock_preset_gains_present_in_string(self):
        result = format_anequalizer_string(PRESETS["Rock"])
        for freq, gain in zip(BAND_FREQUENCIES, PRESETS["Rock"]):
            # Integer gains are emitted without a decimal point.
            assert f"f={freq} w={freq} g={gain} t=0" in result

    def test_wrong_length_raises_value_error(self):
        # Decision: hard-fail on a malformed list. A truncated input
        # would otherwise produce a syntactically valid filter with
        # missing bands, and the resulting "bands 8-10 sit at 0 dB"
        # behaviour is harder to diagnose than a stack trace.
        with pytest.raises(ValueError):
            format_anequalizer_string([0] * 5)
        with pytest.raises(ValueError):
            format_anequalizer_string([])
        with pytest.raises(ValueError):
            format_anequalizer_string([0] * (BAND_COUNT + 1))

    def test_gain_clamped_to_envelope(self):
        # ±60 dB would shred drivers — clamp before the string ever
        # hits mpv. ±12 is the envelope the research doc specifies.
        result = format_anequalizer_string([99, -99] + [0] * (BAND_COUNT - 2))
        assert f"g={int(GAIN_LIMIT_DB)} " in result
        assert f"g=-{int(GAIN_LIMIT_DB)} " in result

    def test_non_integer_gain_emits_decimal(self):
        # Bands the UI will eventually produce won't always be whole
        # dB — make sure those round-trip into the string without
        # weird formatting.
        bands = [1.5, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        result = format_anequalizer_string(bands)
        assert "g=1.5 " in result

    def test_uses_c_minus_one_for_all_channels(self):
        # ``c-1`` is mpv's "apply to every audio channel" sentinel.
        # If the formatter ever switched to per-channel addressing
        # without updating the calling convention, stereo balance
        # would silently break.
        result = format_anequalizer_string(PRESETS["Flat"])
        for entry in result.split("=", 1)[1].split("|"):
            assert entry.startswith("c-1 ")

    def test_butterworth_response_type(self):
        # ``t=0`` is Butterworth — one-octave skirts, the standard
        # graphic-EQ response. The other ``t`` values (Chebyshev I/II)
        # are not what users expect from a "graphic EQ" knob.
        result = format_anequalizer_string(PRESETS["Flat"])
        for entry in result.split("=", 1)[1].split("|"):
            assert entry.endswith(" t=0")
