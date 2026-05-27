"""Tests for the Bit-perfect mode toggle and its enforcement boundary.

T2 of the audiophile playback path — see
``docs/research/bit_perfect_playback.md`` §7 and ``docs/bit_perfect.md``.

The toggle is a master gate: when on, dependent settings (volume,
ReplayGain, EQ, Crossfade) must end up at their bit-perfect-safe
values, and the volume-input boundary (``PlayerBackend.set_volume``)
must clamp any input to 100.

These tests cover the setting itself + the volume-input guard. The
Settings → Playback UI gating is checked via the dialog tests already
exercising the broader Playback page.
"""

from unittest.mock import MagicMock

import pytest


# QSettings persists across tests in the same process — see
# `test_eq_settings.py` for the same pattern. Explicitly remove the
# bit-perfect keys before AND after each test so the "default is off"
# checks don't pick up state left by an earlier test.
_BP_KEYS = ("playback/bit_perfect_mode", "playback/audio_exclusive")


@pytest.fixture
def bp_settings(isolated_settings):
    for k in _BP_KEYS:
        isolated_settings._s.remove(k)
    yield isolated_settings
    for k in _BP_KEYS:
        isolated_settings._s.remove(k)


def test_bit_perfect_mode_defaults_off(bp_settings):
    """Bit-perfect is opt-in — fresh installs aren't subjected to the
    contract by default."""
    assert bp_settings.bit_perfect_mode is False


def test_bit_perfect_mode_round_trip(bp_settings):
    """Setter persists; getter reads back; bool coercion holds."""
    bp_settings.bit_perfect_mode = True
    assert bp_settings.bit_perfect_mode is True
    bp_settings.bit_perfect_mode = False
    assert bp_settings.bit_perfect_mode is False


def test_volume_guard_clamps_to_100_when_bit_perfect_active(bp_settings):
    """The set_volume floor — any value, any input path, becomes 100
    while the *runtime* bit-perfect contract is active. Covers MPRIS /
    keyboard / system media keys, not just the slider widget.

    The lock keys off ``bus.bit_perfect_active`` (the runtime flag),
    not ``settings.bit_perfect_mode`` (the setting), so the mode is
    only honoured when the source is actually lossless. See
    ``modules.player_backend.PlayerBackend._compute_bit_perfect_active``."""
    from modules.player_backend import MpvController

    bp_settings.bit_perfect_mode = True

    # Stub the heavyweight bits MpvController normally wires up. We only
    # need the set_volume method to run its guard branch and observe the
    # final value written to the mpv handle.
    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = bp_settings
    ctrl._mpv = MagicMock()
    ctrl._mpv.__setitem__ = MagicMock()
    ctrl._muted_volume = None
    ctrl.bus = MagicMock()
    ctrl.bus.bit_perfect_active = True
    ctrl._cast_active = lambda: False

    for input_vol in (0, 25, 50, 75, 99, 100, 150):
        ctrl._mpv.__setitem__.reset_mock()
        ctrl.set_volume(input_vol)
        # mpv["volume"] write captures the final value the guard
        # produced — should always be 100 while the contract is active.
        written = ctrl._mpv.__setitem__.call_args
        assert written is not None
        key, value = written.args
        assert key == "volume"
        assert value == 100, f"input {input_vol} should clamp to 100, got {value}"


def test_volume_guard_inactive_when_runtime_contract_not_in_force(bp_settings):
    """With the runtime contract inactive (bit-perfect setting on but
    the current source is lossy, or setting simply off), set_volume
    honours its input verbatim after the standard 0..100 clamp. This
    is the "MP3 with bit-perfect checked unlocks the slider" path."""
    from modules.player_backend import MpvController

    bp_settings.bit_perfect_mode = False

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = bp_settings
    ctrl._mpv = MagicMock()
    ctrl._mpv.__setitem__ = MagicMock()
    ctrl._muted_volume = None
    ctrl.bus = MagicMock()
    ctrl.bus.bit_perfect_active = False
    ctrl._cast_active = lambda: False

    ctrl.set_volume(42)
    key, value = ctrl._mpv.__setitem__.call_args.args
    assert key == "volume"
    assert value == 42


# ── T3: exclusive output ─────────────────────────────────────────────


def test_audio_exclusive_defaults_off(bp_settings):
    """Exclusive output is opt-in within bit-perfect — even users who
    enable bit-perfect shouldn't silently lose every other app's sound."""
    assert bp_settings.audio_exclusive is False


def test_audio_exclusive_round_trip(bp_settings):
    bp_settings.audio_exclusive = True
    assert bp_settings.audio_exclusive is True
    bp_settings.audio_exclusive = False
    assert bp_settings.audio_exclusive is False


def test_make_mpv_handle_passes_audio_exclusive_when_enabled(
    bp_settings, monkeypatch
):
    """With both bit-perfect mode and audio_exclusive on, the mpv handle
    factory must include ``audio_exclusive='yes'`` in its kwargs."""
    from modules.player_backend import MpvController
    from modules import player_backend as pb_mod

    bp_settings.bit_perfect_mode = True
    bp_settings.audio_exclusive = True
    bp_settings.volume = 80

    captured = {}

    def _fake_mpv(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_fake_mpv))

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = bp_settings
    ctrl._make_mpv_handle()

    assert captured.get("audio_exclusive") == "yes"


def test_make_mpv_handle_omits_audio_exclusive_when_bit_perfect_off(
    bp_settings, monkeypatch
):
    """The exclusive flag is gated behind bit-perfect — flicking it on
    while bit-perfect is off should NOT plumb through to mpv. The UI
    already prevents this combination (the sub-toggle is disabled), but
    the factory must not trust UI ordering."""
    from modules.player_backend import MpvController
    from modules import player_backend as pb_mod

    bp_settings.bit_perfect_mode = False
    bp_settings.audio_exclusive = True  # stale value
    bp_settings.volume = 80

    captured = {}

    def _fake_mpv(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_fake_mpv))

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = bp_settings
    ctrl._make_mpv_handle()

    assert "audio_exclusive" not in captured


def test_make_mpv_handle_falls_back_to_shared_on_construction_failure(
    bp_settings, monkeypatch
):
    """Windows WASAPI #11600 / #11733 — some DACs refuse exclusive open
    and mpv raises during construction. The factory must catch, drop
    the flag, and retry in shared mode so the app still launches."""
    from modules.player_backend import MpvController
    from modules import player_backend as pb_mod

    bp_settings.bit_perfect_mode = True
    bp_settings.audio_exclusive = True
    bp_settings.volume = 80

    calls = []

    def _flaky_mpv(**kwargs):
        calls.append(kwargs.copy())
        if "audio_exclusive" in kwargs:
            raise RuntimeError("simulated WASAPI exclusive open failure")
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_flaky_mpv))

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = bp_settings
    handle = ctrl._make_mpv_handle()

    assert handle is not None
    # Two construction attempts: first with audio_exclusive (raises),
    # second without (succeeds).
    assert len(calls) == 2
    assert calls[0].get("audio_exclusive") == "yes"
    assert "audio_exclusive" not in calls[1]
