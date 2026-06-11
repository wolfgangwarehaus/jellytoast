"""Audio output routing — Settings → Playback → Audio output.

mpv ``--audio-device`` pinning (docs/research/audio_output_routing.md):
Auto by default; any enumerated device otherwise, including raw ALSA
``hw:`` for the direct audiophile path. Pins the setting contract, the
handle-factory kwarg + layered open fallback, the live runtime push to
both mpv handles, the ALSA crossfade guardrail, and the visualizer's
direct-ALSA caption state.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

_KEYS = ["playback/audio_output_device"]


@pytest.fixture
def out_settings(isolated_settings):
    for k in _KEYS:
        isolated_settings._s.remove(k)
    yield isolated_settings
    for k in _KEYS:
        isolated_settings._s.remove(k)


# ── Setting contract ──────────────────────────────────────────────────


def test_defaults_to_auto(out_settings):
    assert out_settings.audio_output_device == "auto"


def test_round_trip(out_settings):
    out_settings.audio_output_device = "alsa/hw:CARD=DAC"
    assert out_settings.audio_output_device == "alsa/hw:CARD=DAC"


def test_empty_normalizes_to_auto(out_settings):
    out_settings.audio_output_device = "   "
    assert out_settings.audio_output_device == "auto"


# ── Handle factory ────────────────────────────────────────────────────


def _ctrl_with(settings):
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = settings
    return ctrl


def test_factory_passes_pinned_device(out_settings, monkeypatch):
    from jellytoast import player_backend as pb_mod

    out_settings.audio_output_device = "pipewire/alsa_output.usb-DAC"
    out_settings.volume = 80
    captured = {}

    def _fake_mpv(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_fake_mpv))
    _ctrl_with(out_settings)._make_mpv_handle()
    assert captured.get("audio_device") == "pipewire/alsa_output.usb-DAC"


def test_factory_omits_device_on_auto(out_settings, monkeypatch):
    from jellytoast import player_backend as pb_mod

    out_settings.volume = 80
    captured = {}

    def _fake_mpv(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_fake_mpv))
    _ctrl_with(out_settings)._make_mpv_handle()
    assert "audio_device" not in captured


def test_factory_retries_on_auto_when_device_open_fails(out_settings, monkeypatch):
    """A pinned device can vanish (USB DAC unplugged, stale persisted
    name) — the factory must drop it and retry on auto so the app never
    launches silent."""
    from jellytoast import player_backend as pb_mod

    out_settings.audio_output_device = "alsa/hw:CARD=Gone"
    out_settings.volume = 80
    calls = []

    def _flaky_mpv(**kwargs):
        calls.append(kwargs.copy())
        if "audio_device" in kwargs:
            raise RuntimeError("ao open failed")
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_flaky_mpv))
    handle = _ctrl_with(out_settings)._make_mpv_handle()
    assert handle is not None
    assert len(calls) == 2
    assert "audio_device" in calls[0] and "audio_device" not in calls[1]
    # The persisted choice is left alone — the device may return.
    assert out_settings.audio_output_device == "alsa/hw:CARD=Gone"


def test_factory_layered_fallback_device_then_exclusive(out_settings, monkeypatch):
    """Worst case: pinned device fails AND exclusive open fails — the
    factory sheds one layer at a time and still produces a handle."""
    from jellytoast import player_backend as pb_mod

    out_settings.audio_output_device = "alsa/hw:CARD=Gone"
    out_settings.bit_perfect_mode = True
    out_settings.audio_exclusive = True
    out_settings.audio_quality = "original"
    out_settings.volume = 80
    calls = []

    def _flaky_mpv(**kwargs):
        calls.append(kwargs.copy())
        if "audio_device" in kwargs or "audio_exclusive" in kwargs:
            raise RuntimeError("ao open failed")
        return MagicMock()

    monkeypatch.setattr(pb_mod, "mpv", MagicMock(MPV=_flaky_mpv))
    handle = _ctrl_with(out_settings)._make_mpv_handle()
    assert handle is not None
    assert len(calls) == 3
    assert "audio_device" not in calls[-1] and "audio_exclusive" not in calls[-1]


# ── Runtime push ──────────────────────────────────────────────────────


class _FakeHandle(dict):
    pass


def test_runtime_push_targets_both_handles(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = _FakeHandle()
    sibling = _FakeHandle()
    ctrl._crossfader = MagicMock(sibling=sibling)
    ctrl.set_audio_output_device("pulse/some-sink")
    assert ctrl._mpv["audio-device"] == "pulse/some-sink"
    assert sibling["audio-device"] == "pulse/some-sink"


def test_runtime_push_normalizes_empty_to_auto(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = _FakeHandle()
    ctrl._crossfader = None
    ctrl.set_audio_output_device("")
    assert ctrl._mpv["audio-device"] == "auto"


def test_runtime_push_without_handle_is_noop(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = None
    ctrl._crossfader = None
    ctrl.set_audio_output_device("pulse/x")  # must not raise


# ── Device enumeration ────────────────────────────────────────────────


def test_choices_parse_name_and_description(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = {
        "audio-device-list": [
            {"name": "auto", "description": "Autoselect device"},
            {"name": "pipewire/sink1", "description": "Speakers"},
            {"name": "alsa/hw:CARD=DAC", "description": ""},
            {"name": "", "description": "ghost"},
        ]
    }
    choices = ctrl.audio_device_choices()
    assert ("pipewire/sink1", "Speakers") in choices
    assert ("alsa/hw:CARD=DAC", "alsa/hw:CARD=DAC") in choices
    assert all(name for name, _ in choices)


def test_choices_empty_without_handle(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = None
    assert ctrl.audio_device_choices() == []


# ── ALSA crossfade guardrail ──────────────────────────────────────────


def test_alsa_direct_suppresses_crossfader(out_settings):
    ctrl = _ctrl_with(out_settings)
    out_settings.crossfade_enabled = True
    out_settings.audio_output_device = "alsa/hw:CARD=DAC"
    ctrl._cast_active = lambda: False
    ctrl._mpv = _FakeHandle()
    ctrl._crossfader = None
    assert ctrl._ensure_crossfader() is None


def test_pipewire_device_keeps_crossfader_path(out_settings):
    """Non-ALSA pins must not trip the guardrail — only raw hw: access
    is exclusive by nature."""
    from jellytoast.player_backend import MpvController

    ctrl = _ctrl_with(out_settings)
    out_settings.crossfade_enabled = True
    out_settings.audio_output_device = "pipewire/sink1"
    ctrl._cast_active = lambda: False
    ctrl._mpv = None  # stop before Crossfader construction
    ctrl._crossfader = None
    # Falls through the alsa gate and exits on the mpv-is-None gate
    # (returns None for THAT reason, not the device).
    assert ctrl._ensure_crossfader() is None
    assert isinstance(ctrl, MpvController)


# ── Visualizer direct-ALSA caption ────────────────────────────────────


def test_visualizer_flips_alsa_state_on_bus_signal(qapp, out_settings):
    from jellytoast.player_state import PlayerBus
    from jellytoast.visualizer_widget import VisualizerWidget

    w = VisualizerWidget()
    try:
        assert w._alsa_direct is False
        PlayerBus.get().audio_output_device_changed.emit("alsa/hw:CARD=DAC")
        assert w._alsa_direct is True
        PlayerBus.get().audio_output_device_changed.emit("auto")
        assert w._alsa_direct is False
    finally:
        w.deleteLater()


def test_visualizer_seeds_alsa_state_from_setting(qapp, out_settings):
    from jellytoast.visualizer_widget import VisualizerWidget

    out_settings.audio_output_device = "alsa/hw:CARD=DAC"
    w = VisualizerWidget()
    try:
        assert w._alsa_direct is True
    finally:
        w.deleteLater()
