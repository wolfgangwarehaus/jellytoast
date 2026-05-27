"""Tests for ``modules.eq_presets`` — preset shape + the mpv
``anequalizer`` filter-string formatter. Qt-free, mpv-free."""

import pytest

from modules.eq_presets import (
    BAND_COUNT,
    BAND_FREQUENCIES,
    DEFAULT_PRESET,
    FIREQUALIZER_DELAY_S,
    GAIN_LIMIT_DB,
    PRESETS,
    format_anequalizer_string,
    format_firequalizer_string,
    get_preset,
)


_ANEQ_PREFIX = 'anequalizer=params="'


def _aneq_entries(result: str) -> list[str]:
    """Return the ``|``-separated band entries inside an
    ``anequalizer=params="…"`` filter spec. ffmpeg 8 requires the
    named-AVOption wrapping; this helper hides that detail from
    individual entry-shape assertions."""
    assert result.startswith(_ANEQ_PREFIX), result
    assert result.endswith('"'), result
    return result[len(_ANEQ_PREFIX):-1].split("|")


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
    """EQ T1 — the v2 implementation emits a single ``anequalizer``
    filter with one band entry per (channel × frequency). See
    ``docs/research/eq_dsp_v2.md`` §2 (the "wart" fix) for why the
    output shape changed from cascaded ``equalizer`` biquads."""

    def test_flat_preset_yields_well_formed_filter(self):
        # Decision: Flat (all zeros) still returns a fully-formed
        # filter string rather than an empty string. Callers that
        # want "bypass" should disable the filter via ``apply_eq``;
        # the formatter's job is to produce a valid filter spec for
        # whatever band gains it's given. Keeps the calling convention
        # uniform — the output type doesn't depend on the values.
        result = format_anequalizer_string([0] * BAND_COUNT)
        # Single ``anequalizer=params="…"`` filter; entries
        # pipe-separated inside the params string.
        assert result.startswith(_ANEQ_PREFIX)
        entries = _aneq_entries(result)
        # Default channel_count is 2 (stereo) → 2 × BAND_COUNT entries.
        assert len(entries) == 2 * BAND_COUNT
        for entry in entries:
            assert "g=0 " in entry or entry.endswith("g=0 t=0")

    def test_rock_preset_contains_all_ten_bands_per_channel(self):
        result = format_anequalizer_string(PRESETS["Rock"])
        entries = _aneq_entries(result)
        assert len(entries) == 2 * BAND_COUNT
        # Channel 0 entries come first, then channel 1, each in
        # BAND_FREQUENCIES order — guards against re-ordering or
        # cross-channel mismatches.
        for ch in range(2):
            for i, freq in enumerate(BAND_FREQUENCIES):
                entry = entries[ch * BAND_COUNT + i]
                assert entry.startswith(f"c{ch} "), (
                    f"ch{ch} band {i}: entry {entry!r} missing c{ch} prefix"
                )
                assert f"f={freq} " in entry, (
                    f"ch{ch} band {i}: entry {entry!r} missing f={freq}"
                )

    def test_rock_preset_gains_present_per_channel(self):
        result = format_anequalizer_string(PRESETS["Rock"])
        for ch in range(2):
            for freq, gain in zip(BAND_FREQUENCIES, PRESETS["Rock"]):
                # Integer gains emit without a decimal point.
                assert f"c{ch} f={freq} w={freq} g={gain} t=0" in result

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

    def test_band_width_is_one_octave_butterworth(self):
        # ``anequalizer`` takes width in Hz (not octaves). ``w = f`` is
        # the canonical one-octave Butterworth bandwidth and matches
        # the ffmpeg + real-world MPD examples cited in
        # ``docs/research/eq_dsp_v2.md`` §2.
        result = format_anequalizer_string(PRESETS["Flat"])
        for entry in _aneq_entries(result):
            # Each entry shape: "c<n> f=<freq> w=<freq> g=<dB> t=0"
            parts = entry.split()
            f_part = next(p for p in parts if p.startswith("f="))
            w_part = next(p for p in parts if p.startswith("w="))
            assert f_part[len("f="):] == w_part[len("w="):], (
                f"width must equal freq for 1-octave Butterworth — {entry!r}"
            )
            assert "t=0" in entry  # Butterworth type explicit

    def test_uses_concrete_channel_indices_not_c_minus_one(self):
        """The original "wart" was the use of ``c-1`` as an
        all-channels sentinel — ``anequalizer`` doesn't have one and
        silently dropped every band. The fix is concrete indices."""
        result = format_anequalizer_string(PRESETS["Flat"])
        assert "c-1" not in result
        assert "c0 " in result
        assert "c1 " in result

    def test_mono_channel_count_emits_single_channel_only(self):
        result = format_anequalizer_string(PRESETS["Flat"], channel_count=1)
        entries = _aneq_entries(result)
        assert len(entries) == BAND_COUNT
        for entry in entries:
            assert entry.startswith("c0 ")

    def test_surround_channel_count_emits_cross_product(self):
        """5.1 = 6 channels → 6 × BAND_COUNT entries, each channel
        getting the full band set. Guards against the formatter
        forgetting a channel on surround sources."""
        result = format_anequalizer_string(PRESETS["Flat"], channel_count=6)
        entries = _aneq_entries(result)
        assert len(entries) == 6 * BAND_COUNT
        for ch in range(6):
            assert any(e.startswith(f"c{ch} ") for e in entries), (
                f"missing channel {ch}"
            )

    def test_invalid_channel_count_falls_back_to_stereo(self):
        # Defence-in-depth: a misbehaving mpv property could return
        # None, a string, 0, or a huge number. The formatter must
        # not crash and must produce a syntactically valid filter.
        result_none = format_anequalizer_string(PRESETS["Flat"], channel_count=None)
        result_zero = format_anequalizer_string(PRESETS["Flat"], channel_count=0)
        result_huge = format_anequalizer_string(PRESETS["Flat"], channel_count=999)
        # None / non-numeric → stereo default; 0 clamps up to 1;
        # 999 clamps down to 8.
        assert result_none.startswith(_ANEQ_PREFIX)
        assert result_zero.startswith(_ANEQ_PREFIX)
        assert result_huge.startswith(_ANEQ_PREFIX)
        # Huge value capped at 8 channels, no further.
        assert "c8 " not in result_huge


# ── format_firequalizer_string (EQ T2 — linear-phase FIR) ──────────────


class TestFormatFirequalizerString:
    """EQ T2 — opt-in linear-phase FIR mode. See
    ``docs/research/eq_dsp_v2.md`` §6 for the rationale; the formatter
    swaps the IIR filter for ffmpeg's ``firequalizer`` with
    ``zero_phase=on``."""

    def test_flat_preset_yields_well_formed_filter(self):
        result = format_firequalizer_string([0] * BAND_COUNT)
        assert result.startswith("firequalizer=")
        # gain_entry option is single-quoted so the inner semicolons
        # don't collide with mpv's filter-chain separator.
        assert "gain_entry='" in result
        # All ten ISO bands present.
        for freq in BAND_FREQUENCIES:
            assert f"entry({freq},0)" in result

    def test_zero_phase_and_delay_options_present(self):
        result = format_firequalizer_string(PRESETS["Flat"])
        assert "zero_phase=on" in result
        assert f"delay={FIREQUALIZER_DELAY_S}" in result

    def test_rock_preset_gains_present(self):
        result = format_firequalizer_string(PRESETS["Rock"])
        for freq, gain in zip(BAND_FREQUENCIES, PRESETS["Rock"]):
            # Integer gains emit without a decimal point — same rule
            # as the IIR formatter.
            assert f"entry({freq},{gain})" in result

    def test_non_integer_gain_emits_decimal(self):
        bands = [1.5, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        result = format_firequalizer_string(bands)
        assert "entry(31,1.5)" in result

    def test_gain_clamped_to_envelope(self):
        result = format_firequalizer_string([99, -99] + [0] * (BAND_COUNT - 2))
        assert f"entry(31,{int(GAIN_LIMIT_DB)})" in result
        assert f"entry(62,-{int(GAIN_LIMIT_DB)})" in result

    def test_wrong_length_raises_value_error(self):
        with pytest.raises(ValueError):
            format_firequalizer_string([0] * 5)
        with pytest.raises(ValueError):
            format_firequalizer_string([])
        with pytest.raises(ValueError):
            format_firequalizer_string([0] * (BAND_COUNT + 1))

    def test_entries_in_iso_band_order(self):
        # Order matters for firequalizer's interpolation between
        # entries — re-ordering would skew the curve.
        result = format_firequalizer_string(PRESETS["Rock"])
        # Pull out the gain_entry list between the single quotes.
        start = result.index("gain_entry='") + len("gain_entry='")
        end = result.index("'", start)
        entries = result[start:end].split(";")
        assert len(entries) == BAND_COUNT
        for i, (entry, freq) in enumerate(zip(entries, BAND_FREQUENCIES)):
            assert entry.startswith(f"entry({freq},"), (
                f"band {i}: entry {entry!r} should lead with f={freq}"
            )
