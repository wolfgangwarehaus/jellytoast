"""Tests for the EQ-related Settings properties added alongside the
v1 Equalizer surface: ``eq_preamp`` + ``eq_user_presets``. Also covers
the ``apply_eq`` chain-string composition so the pre-amp + band split
is verified end-to-end on the backend side.

Existing ``test_eq_presets.py`` covers the pure formatter + preset
shape. This file is the settings + backend-chain layer.

Pattern follows ``test_jellyfin_local_radio.py`` for QSettings
isolation: clean specific keys via ``isolated_settings._s.remove``
before and after each test since QSettings test-mode shares one file
process-wide.
"""

from __future__ import annotations

import json

import pytest

from modules.eq_presets import BAND_COUNT, GAIN_LIMIT_DB


# ── Fixture: an isolated Settings with EQ keys nuked between tests ──────


@pytest.fixture
def eq_settings(isolated_settings):
    keys = [
        "playback/eq_preamp",
        "playback/eq_user_presets",
        "playback/eq_bands",
        "playback/eq_enabled",
        "playback/eq_preset",
    ]
    for k in keys:
        isolated_settings._s.remove(k)
    yield isolated_settings
    for k in keys:
        isolated_settings._s.remove(k)


# ── eq_preamp ───────────────────────────────────────────────────────────


class TestEqPreamp:
    def test_default_is_zero(self, eq_settings):
        assert eq_settings.eq_preamp == 0.0

    def test_round_trip(self, eq_settings):
        eq_settings.eq_preamp = 3.5
        assert eq_settings.eq_preamp == 3.5

    def test_round_trip_negative(self, eq_settings):
        eq_settings.eq_preamp = -6.0
        assert eq_settings.eq_preamp == -6.0

    def test_clamp_high(self, eq_settings):
        eq_settings.eq_preamp = 50.0
        assert eq_settings.eq_preamp == GAIN_LIMIT_DB

    def test_clamp_low(self, eq_settings):
        eq_settings.eq_preamp = -50.0
        assert eq_settings.eq_preamp == -GAIN_LIMIT_DB

    def test_setter_clamps_at_boundary(self, eq_settings):
        eq_settings.eq_preamp = 12.0
        assert eq_settings.eq_preamp == 12.0
        eq_settings.eq_preamp = -12.0
        assert eq_settings.eq_preamp == -12.0

    def test_non_numeric_setter_falls_back_to_zero(self, eq_settings):
        eq_settings.eq_preamp = "not a number"
        assert eq_settings.eq_preamp == 0.0

    def test_corrupt_stored_value_returns_zero(self, eq_settings):
        # Manually poison the underlying QSettings key.
        eq_settings._s.setValue("playback/eq_preamp", "garbage")
        # Getter should return 0.0 rather than raise.
        assert eq_settings.eq_preamp == 0.0


# ── eq_user_presets ──────────────────────────────────────────────────────


class TestEqUserPresets:
    def test_default_is_empty_dict(self, eq_settings):
        assert eq_settings.eq_user_presets == {}

    def test_round_trip_single_preset(self, eq_settings):
        eq_settings.eq_user_presets = {
            "MyPreset": {"preamp": -2.0, "bands": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}
        }
        out = eq_settings.eq_user_presets
        assert set(out.keys()) == {"MyPreset"}
        assert out["MyPreset"]["preamp"] == -2.0
        assert out["MyPreset"]["bands"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    def test_round_trip_multiple_presets(self, eq_settings):
        zeros = [0.0] * BAND_COUNT
        eq_settings.eq_user_presets = {
            "A": {"preamp": 1.0, "bands": list(zeros)},
            "B": {"preamp": 2.0, "bands": list(zeros)},
        }
        out = eq_settings.eq_user_presets
        assert set(out.keys()) == {"A", "B"}
        assert out["A"]["preamp"] == 1.0
        assert out["B"]["preamp"] == 2.0

    def test_short_band_list_pads_to_band_count(self, eq_settings):
        eq_settings.eq_user_presets = {"Short": {"preamp": 0.0, "bands": [1, 2, 3]}}
        out = eq_settings.eq_user_presets
        assert out["Short"]["bands"] == [1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def test_long_band_list_truncates_to_band_count(self, eq_settings):
        eq_settings.eq_user_presets = {
            "Long": {"preamp": 0.0, "bands": list(range(BAND_COUNT + 5))}
        }
        out = eq_settings.eq_user_presets
        assert len(out["Long"]["bands"]) == BAND_COUNT

    def test_non_numeric_band_collapses_to_zero(self, eq_settings):
        bands = [1, "x", 3, 4, 5, 6, 7, 8, 9, 10]
        eq_settings.eq_user_presets = {"Mixed": {"preamp": 0.0, "bands": bands}}
        out = eq_settings.eq_user_presets
        # "x" becomes 0.0 on store.
        assert out["Mixed"]["bands"][1] == 0.0
        assert out["Mixed"]["bands"][0] == 1.0

    def test_malformed_json_returns_empty_dict(self, eq_settings):
        eq_settings._s.setValue("playback/eq_user_presets", "{not json")
        assert eq_settings.eq_user_presets == {}

    def test_non_dict_stored_value_returns_empty(self, eq_settings):
        eq_settings._s.setValue("playback/eq_user_presets", json.dumps([1, 2, 3]))
        assert eq_settings.eq_user_presets == {}

    def test_preset_entry_wrong_band_count_in_stored_value_drops(self, eq_settings):
        # If a stored entry's band list isn't BAND_COUNT long, the getter
        # silently drops it rather than returning a malformed dict.
        eq_settings._s.setValue(
            "playback/eq_user_presets",
            json.dumps(
                {
                    "Ok": {"preamp": 0.0, "bands": [0] * BAND_COUNT},
                    "Bad": {"preamp": 0.0, "bands": [1, 2]},
                }
            ),
        )
        out = eq_settings.eq_user_presets
        assert "Ok" in out
        assert "Bad" not in out

    def test_non_dict_entry_in_stored_value_drops(self, eq_settings):
        eq_settings._s.setValue(
            "playback/eq_user_presets",
            json.dumps(
                {
                    "Ok": {"preamp": 0.0, "bands": [0] * BAND_COUNT},
                    "JustAString": "not-a-dict",
                }
            ),
        )
        out = eq_settings.eq_user_presets
        assert "JustAString" not in out

    def test_empty_setter_persists_empty(self, eq_settings):
        # Seed something then wipe.
        eq_settings.eq_user_presets = {
            "X": {"preamp": 0.0, "bands": [0] * BAND_COUNT}
        }
        assert "X" in eq_settings.eq_user_presets
        eq_settings.eq_user_presets = {}
        assert eq_settings.eq_user_presets == {}


# ── apply_eq chain composition (preamp wiring) ─────────────────────────


class _FakeMpv(dict):
    """dict-like stand-in for libmpv. ``apply_eq`` writes to
    ``self._mpv["af"]`` which the dict captures verbatim."""


class _FakeSettings:
    """Replicates the Settings attributes apply_eq reads —
    ``eq_preamp`` and ``eq_linear_phase`` (EQ T2). apply_eq tolerates
    missing attrs via try/except so existing tests with defaults
    don't need to set linear_phase explicitly."""

    def __init__(self, preamp: float = 0.0, linear_phase: bool = False):
        self.eq_preamp = preamp
        self.eq_linear_phase = linear_phase


class _FakeBackend:
    """Minimal subset of PlayerBackend that ``apply_eq`` touches.
    Lets us exercise the chain-string composition without spinning up
    libmpv or QApplication."""

    def __init__(self, preamp: float = 0.0, linear_phase: bool = False):
        self._mpv = _FakeMpv()
        self.settings = _FakeSettings(preamp, linear_phase)
        self._last_eq_state = None

    apply_eq = None  # type: ignore[assignment]


@pytest.fixture
def fake_backend():
    from modules.player_backend import MpvController

    backend = _FakeBackend(preamp=0.0)
    # Bind the unbound method onto our fake.
    backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
    return backend


class TestApplyEqChain:
    def test_disabled_writes_empty_string(self, fake_backend):
        fake_backend.settings.eq_preamp = 0.0
        fake_backend.apply_eq(False, [0.0] * BAND_COUNT)
        assert fake_backend._mpv["af"] == ""

    def test_enabled_flat_no_preamp(self, fake_backend):
        fake_backend.settings.eq_preamp = 0.0
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = fake_backend._mpv["af"]
        # No volume= prefix when preamp is zero — keeps the chain short.
        # EQ T1 (2026-05-27) switched to a single ``anequalizer`` filter
        # with per-channel band entries; see docs/research/eq_dsp_v2.md
        # §2 for the wart-fix story.
        assert chain.startswith("anequalizer=")
        assert "volume=" not in chain

    def test_enabled_with_positive_preamp_prepends_volume(self, fake_backend):
        fake_backend.settings.eq_preamp = 3.0
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = fake_backend._mpv["af"]
        assert chain.startswith("volume=3dB,anequalizer=")

    def test_enabled_with_negative_preamp_prepends_volume(self, fake_backend):
        fake_backend.settings.eq_preamp = -6.0
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = fake_backend._mpv["af"]
        assert chain.startswith("volume=-6dB,anequalizer=")

    def test_enabled_with_fractional_preamp(self, fake_backend):
        fake_backend.settings.eq_preamp = 1.5
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = fake_backend._mpv["af"]
        assert chain.startswith("volume=1.5dB,anequalizer=")

    def test_idempotent_no_rewrite_when_state_unchanged(self, fake_backend):
        fake_backend.settings.eq_preamp = 2.0
        bands = [1.0] * BAND_COUNT
        fake_backend.apply_eq(True, bands)
        first_chain = fake_backend._mpv["af"]
        # Mutate the recorded chain to detect a re-write.
        fake_backend._mpv["af"] = "SENTINEL"
        fake_backend.apply_eq(True, bands)
        # Cache hit — chain isn't rewritten.
        assert fake_backend._mpv["af"] == "SENTINEL"
        assert first_chain != "SENTINEL"

    def test_preamp_change_alone_re_applies(self, fake_backend):
        bands = [0.0] * BAND_COUNT
        fake_backend.settings.eq_preamp = 0.0
        fake_backend.apply_eq(True, bands)
        first = fake_backend._mpv["af"]

        fake_backend.settings.eq_preamp = 4.0
        fake_backend.apply_eq(True, bands)
        second = fake_backend._mpv["af"]

        assert first != second
        assert second.startswith("volume=4dB,")

    def test_wrong_band_count_skipped(self, fake_backend):
        fake_backend.settings.eq_preamp = 0.0
        fake_backend.apply_eq(True, [0.0, 1.0, 2.0])  # too short
        # Should NOT have written anything.
        assert "af" not in fake_backend._mpv

    def test_non_numeric_bands_skipped(self, fake_backend):
        fake_backend.settings.eq_preamp = 0.0
        fake_backend.apply_eq(True, ["not", "numeric", *(0,) * (BAND_COUNT - 2)])
        # Float coercion of "not" raises ValueError → early return.
        assert "af" not in fake_backend._mpv

    # ── EQ T2 — linear-phase FIR formatter pick ─────────────────────────

    def test_linear_phase_off_uses_anequalizer(self, fake_backend):
        """Default mode — IIR Butterworth via ``anequalizer``."""
        fake_backend.settings.eq_linear_phase = False
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = fake_backend._mpv["af"]
        assert "anequalizer=" in chain
        assert "firequalizer=" not in chain

    def test_linear_phase_on_uses_firequalizer(self):
        """Opt-in mode — FIR, ``zero_phase=on``. Fresh backend so the
        cache key reflects linear_phase from construction."""
        from modules.player_backend import MpvController

        backend = _FakeBackend(linear_phase=True)
        backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert "firequalizer=" in chain
        assert "zero_phase=on" in chain
        assert "anequalizer=" not in chain

    def test_toggling_linear_phase_re_applies(self, fake_backend):
        """``_last_eq_state`` must include linear_phase so toggling
        the mode forces a rewrite rather than short-circuiting on
        the cache hit (same (enabled, bands, preamp) tuple otherwise)."""
        fake_backend.settings.eq_linear_phase = False
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        first = fake_backend._mpv["af"]
        assert "anequalizer=" in first

        fake_backend.settings.eq_linear_phase = True
        fake_backend.apply_eq(True, [0.0] * BAND_COUNT)
        second = fake_backend._mpv["af"]
        assert "firequalizer=" in second
        assert first != second

    def test_linear_phase_with_preamp(self):
        """The pre-amp ``volume=`` filter chains the same way
        regardless of which EQ filter follows it."""
        from modules.player_backend import MpvController

        backend = _FakeBackend(preamp=-6.0, linear_phase=True)
        backend.apply_eq = MpvController.apply_eq.__get__(backend, MpvController)
        backend.apply_eq(True, [0.0] * BAND_COUNT)
        chain = backend._mpv["af"]
        assert chain.startswith("volume=-6dB,firequalizer=")
