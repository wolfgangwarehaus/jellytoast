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


class _PropertyOnlyHandle:
    """Mirrors real python-mpv semantics: ``audio-device-list`` is a runtime
    PROPERTY (attribute API). Dict-style ``handle["audio-device-list"]``
    targets the options/ namespace and raises — exactly what live mpv does.
    Pins the 2026-06-11 live-round finding: the picker read the list via
    ``__getitem__`` and silently fell back to Auto-only on every platform."""

    audio_device_list = [
        {"name": "auto", "description": "Autoselect device"},
        {"name": "pipewire/sink1", "description": "Speakers"},
        {"name": "alsa/hw:CARD=DAC", "description": ""},
        {"name": "", "description": "ghost"},
    ]

    def __getitem__(self, key):
        raise AttributeError("mpv property does not exist", -8, key)


def test_choices_parse_name_and_description(out_settings):
    ctrl = _ctrl_with(out_settings)
    ctrl._mpv = _PropertyOnlyHandle()
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


# ── Picker curation ───────────────────────────────────────────────────
# Fixture = the real 32-entry enumeration from the dev box (2026-06-11):
# one pipewire sink (+ its pulse/ twin), ALSA plugin aliases, surround
# profile variants, gadget usbstream endpoints, and dev-backend defaults.


_RAW_LINUX_DESKTOP = [
    ("auto", "Autoselect device"),
    ("pipewire", "Default (pipewire)"),
    ("pipewire/alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1", "HDMI"),
    ("pulse/alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1", "HDMI"),
    ("alsa", "Default (alsa)"),
    ("alsa/lavrate", "Rate Converter Plugin Using Libav/FFmpeg Library"),
    ("alsa/samplerate", "Rate Converter Plugin Using Samplerate Library"),
    ("alsa/speexrate", "Rate Converter Plugin Using Speex Resampler"),
    ("alsa/jack", "JACK Audio Connection Kit"),
    ("alsa/oss", "Open Sound System"),
    ("alsa/pipewire", "PipeWire Sound Server"),
    ("alsa/speex", "Plugin using Speex DSP (resample, agc, denoise, echo, dereverb)"),
    ("alsa/upmix", "Plugin for channel upmix (4,6,8)"),
    ("alsa/vdownmix", "Plugin for channel downmix (stereo) with a simple spacialization"),
    ("alsa/sysdefault:CARD=PCH", "HDA Intel PCH, ALC1220 Analog/Default Audio Device"),
    ("alsa/front:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/Front output / input"),
    ("alsa/surround21:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/2.1 Surround output"),
    ("alsa/surround40:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/4.0 Surround output"),
    ("alsa/surround41:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/4.1 Surround output"),
    ("alsa/surround50:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/5.0 Surround output"),
    ("alsa/surround51:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/5.1 Surround output"),
    ("alsa/surround71:CARD=PCH,DEV=0", "HDA Intel PCH, ALC1220 Analog/7.1 Surround output"),
    ("alsa/usbstream:CARD=PCH", "HDA Intel PCH/USB Stream Output"),
    ("alsa/hdmi:CARD=NVidia,DEV=0", "HDA NVidia, HDMI 0/HDMI Audio Output"),
    ("alsa/hdmi:CARD=NVidia,DEV=1", "HDA NVidia, LG ULTRAGEAR/HDMI Audio Output"),
    ("alsa/hdmi:CARD=NVidia,DEV=2", "HDA NVidia, HDMI 2/HDMI Audio Output"),
    ("alsa/hdmi:CARD=NVidia,DEV=3", "HDA NVidia, HDMI 3/HDMI Audio Output"),
    ("alsa/usbstream:CARD=NVidia", "HDA NVidia/USB Stream Output"),
    ("jack", "Default (jack)"),
    ("openal", "Default (openal)"),
    ("sdl", "Default (sdl)"),
    ("sndio", "Default (sndio)"),
]


def test_curation_keeps_only_real_destinations():
    from jellytoast.player_backend import _curate_audio_devices

    names = [n for n, _ in _curate_audio_devices(_RAW_LINUX_DESKTOP)]
    assert names == [
        "auto",
        "pipewire",
        "pipewire/alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1",
        "alsa/front:CARD=PCH,DEV=0",
        "alsa/hdmi:CARD=NVidia,DEV=0",
        "alsa/hdmi:CARD=NVidia,DEV=1",
        "alsa/hdmi:CARD=NVidia,DEV=2",
        "alsa/hdmi:CARD=NVidia,DEV=3",
    ]


def test_curation_pulse_sinks_survive_without_pipewire():
    from jellytoast.player_backend import _curate_audio_devices

    raw = [
        ("auto", "Autoselect device"),
        ("pulse", "Default (pulse)"),
        ("pulse/sink1", "Speakers"),
        ("alsa", "Default (alsa)"),
        ("jack", "Default (jack)"),
    ]
    names = [n for n, _ in _curate_audio_devices(raw)]
    assert names == ["auto", "pulse", "pulse/sink1"]


def test_curation_windows_wasapi_endpoints():
    from jellytoast.player_backend import _curate_audio_devices

    raw = [
        ("auto", "Autoselect device"),
        ("wasapi", "Default (wasapi)"),
        ("wasapi/{guid-1}", "Speakers (Realtek)"),
        ("wasapi/{guid-2}", "LG ULTRAGEAR (NVIDIA HDA)"),
        ("openal", "Default (openal)"),
        ("sdl", "Default (sdl)"),
    ]
    names = [n for n, _ in _curate_audio_devices(raw)]
    assert names == ["auto", "wasapi", "wasapi/{guid-1}", "wasapi/{guid-2}"]


def test_choices_apply_curation(out_settings):
    """End to end through audio_device_choices: the noise families are
    gone, the real sinks survive."""
    ctrl = _ctrl_with(out_settings)

    class _Handle:
        audio_device_list = [
            {"name": n, "description": d} for n, d in _RAW_LINUX_DESKTOP
        ]

        def __getitem__(self, key):
            raise AttributeError("mpv property does not exist", -8, key)

    ctrl._mpv = _Handle()
    names = [n for n, _ in ctrl.audio_device_choices()]
    assert "alsa/lavrate" not in names
    assert "sndio" not in names
    assert "pulse/alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1" not in names
    assert "pipewire/alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1" in names
    assert len(names) == 8


def test_choices_env_flag_returns_everything(out_settings, monkeypatch):
    ctrl = _ctrl_with(out_settings)

    class _Handle:
        audio_device_list = [
            {"name": n, "description": d} for n, d in _RAW_LINUX_DESKTOP
        ]

        def __getitem__(self, key):
            raise AttributeError("mpv property does not exist", -8, key)

    ctrl._mpv = _Handle()
    monkeypatch.setenv("JT_AUDIO_DEVICES_ALL", "1")
    assert len(ctrl.audio_device_choices()) == len(_RAW_LINUX_DESKTOP)


def test_choices_fall_back_to_raw_when_curation_empties(out_settings):
    """A box whose only outputs are families we drop (pure JACK) must
    still get a usable picker."""
    ctrl = _ctrl_with(out_settings)

    class _Handle:
        audio_device_list = [
            {"name": "auto", "description": "Autoselect device"},
            {"name": "jack", "description": "Default (jack)"},
            {"name": "sdl", "description": "Default (sdl)"},
        ]

        def __getitem__(self, key):
            raise AttributeError("mpv property does not exist", -8, key)

    ctrl._mpv = _Handle()
    names = [n for n, _ in ctrl.audio_device_choices()]
    assert names == ["auto", "jack", "sdl"]


# ── Audio-health watchdog ─────────────────────────────────────────────
# 2026-06-11 live find: switching ALSA-direct → Auto mid-play left mpv
# with a dead audio output — it raced untimed through the internal
# gapless playlist (scrubber flying, no sound). The watchdog detects
# the zombie (file loaded + unpaused + audio-params None) and recovers
# in stages: ao-reload → shed to auto/shared → pause.


class _ZombieHandle:
    """File 'playing' with a dead AO: time advances, audio-params None."""

    idle_active = False
    pause = False
    time_pos = 42.0
    audio_params = None

    def __init__(self):
        self.commands: list = []
        self.sets: dict = {}

    def command(self, *args):
        self.commands.append(args)

    def __setitem__(self, key, value):
        self.sets[key] = value


def _watchdog_ctrl(out_settings):
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = out_settings
    ctrl._mpv = _ZombieHandle()
    ctrl._cast_active = lambda: False
    ctrl._scheduled = 0
    ctrl._schedule_audio_health_check = lambda: setattr(
        ctrl, "_scheduled", ctrl._scheduled + 1
    )
    ctrl._audio_health_stage = 0
    return ctrl


def test_watchdog_stage0_reloads_ao(out_settings):
    ctrl = _watchdog_ctrl(out_settings)
    ctrl._check_audio_health()
    assert ("ao-reload",) in ctrl._mpv.commands
    assert ctrl._audio_health_stage == 1
    assert ctrl._scheduled == 1  # re-armed


def test_watchdog_stage1_sheds_to_auto_shared(out_settings):
    ctrl = _watchdog_ctrl(out_settings)
    ctrl._audio_health_stage = 1
    ctrl._check_audio_health()
    assert ctrl._mpv.sets.get("audio-device") == "auto"
    assert ctrl._mpv.sets.get("audio-exclusive") == "no"
    assert ("ao-reload",) in ctrl._mpv.commands
    assert ctrl._audio_health_stage == 2


def test_watchdog_stage2_pauses_instead_of_racing(out_settings):
    ctrl = _watchdog_ctrl(out_settings)
    ctrl._audio_health_stage = 2
    ctrl._check_audio_health()
    assert ctrl._mpv.sets.get("pause") is True
    assert ctrl._audio_health_stage == 0  # reset for the next episode


def test_watchdog_healthy_audio_resets_stage(out_settings):
    ctrl = _watchdog_ctrl(out_settings)
    ctrl._mpv.audio_params = {"channel-count": 2}
    ctrl._audio_health_stage = 1
    ctrl._check_audio_health()
    assert ctrl._audio_health_stage == 0
    assert not ctrl._mpv.commands  # no action taken


def test_watchdog_ignores_paused_and_idle(out_settings):
    ctrl = _watchdog_ctrl(out_settings)
    ctrl._mpv.pause = True
    assert ctrl._audio_is_zombie() is False
    ctrl._mpv.pause = False
    ctrl._mpv.idle_active = True
    assert ctrl._audio_is_zombie() is False


def test_device_switch_clears_prefetch_and_arms_watchdog(out_settings):
    """The gapless-boundary guard: a device change drops the prefetched
    playlist entry (the next track must arrive via a clean AO open, not
    a gapless transition across the change) and arms the watchdog."""
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = out_settings
    ctrl._mpv = _ZombieHandle()
    ctrl._crossfader = None
    cleared = []
    ctrl._clear_prefetch = lambda: cleared.append(True)
    armed = []
    ctrl._schedule_audio_health_check = lambda: armed.append(True)
    ctrl.set_audio_output_device("auto")
    assert ctrl._mpv.sets.get("audio-device") == "auto"
    assert cleared and armed
    assert ctrl._audio_health_stage == 0


def test_watchdog_persists_exclusive_off_after_successful_shed(out_settings):
    """If the stage-1 shed (exclusive -> shared) is what revived audio,
    the divergence must become honest: the persisted setting flips off
    and the bus broadcasts it (mpv PipeWire exclusive failing every
    open is a real observed mode)."""
    from unittest.mock import MagicMock

    ctrl = _watchdog_ctrl(out_settings)
    out_settings.audio_exclusive = True
    ctrl.bus = MagicMock()
    # stage 1 shed happens while zombie...
    ctrl._audio_health_stage = 1
    ctrl._check_audio_health()
    assert ctrl._audio_health_shed_exclusive is True
    # ...audio comes back; the next check reconciles.
    ctrl._mpv.audio_params = {"channel-count": 2}
    ctrl._check_audio_health()
    assert out_settings.audio_exclusive is False
    ctrl.bus.audio_exclusive_changed.emit.assert_called_once_with(False)
    assert ctrl._audio_health_shed_exclusive is False
