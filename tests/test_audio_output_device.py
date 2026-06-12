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
    from unittest.mock import MagicMock

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
    # the healthy branch probes bit-perfect contention — keep it inert
    ctrl.bus = MagicMock()
    ctrl.bus.bit_perfect_active = False
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


def test_watchdog_with_exclusive_on_sheds_immediately(out_settings):
    """Exclusive is the known PipeWire-killer: stage 0 must go straight
    to the shed instead of wasting a cycle on ao-reload — during an
    untimed race, tracks restart faster than check cycles."""
    ctrl = _watchdog_ctrl(out_settings)
    out_settings.audio_exclusive = True
    ctrl._check_audio_health()
    assert ctrl._mpv.sets.get("audio-device") == "auto"
    assert ctrl._mpv.sets.get("audio-exclusive") == "no"
    assert ctrl._mpv.sets.get("aid") == "auto"
    assert ctrl._audio_health_shed_exclusive is True
    assert ctrl._audio_health_stage == 2


# ── Error-reason end-file recovery ────────────────────────────────────
# 2026-06-11 follow-up: with Exclusive persisted ON, EVERY PipeWire open
# fails — even plain Auto playback was dead on arrival, and the error
# end-files were silently ignored. The construction-time fallback never
# sees it (mpv opens the AO at first play, not at handle init).


def _error_ctrl(out_settings, monkeypatch, np=None):
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = out_settings
    ctrl._mpv = _ZombieHandle()
    ctrl._cast_active = lambda: False
    ctrl._consecutive_play_errors = 0
    ctrl._played = []
    ctrl.play = lambda np: ctrl._played.append(np)
    import jellytoast.player_state as ps

    monkeypatch.setattr(ps, "get_now_playing", lambda: np)
    # retries are QTimer-deferred; run them inline for the test
    from jellytoast import player_backend as pb

    monkeypatch.setattr(
        pb.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn())
    )
    return ctrl


class _FakeNp:
    item_id = "x1"
    stream_url = "http://srv/stream/x1"


def test_play_error_sheds_exclusive_and_retries(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    out_settings.audio_exclusive = True
    ctrl._on_playback_error()
    assert ctrl._mpv.sets.get("audio-exclusive") == "no"
    assert ctrl._audio_health_shed_exclusive is True
    assert ctrl._played == [np]


def test_play_error_second_strike_falls_back_to_auto(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    out_settings.audio_exclusive = False
    ctrl._consecutive_play_errors = 1
    ctrl._on_playback_error()
    assert ctrl._mpv.sets.get("audio-device") == "auto"
    assert ctrl._played == [np]


def test_play_error_gives_up_after_cap(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    ctrl._consecutive_play_errors = 3
    ctrl._on_playback_error()
    assert ctrl._played == []  # no retry past the cap


def test_play_error_ignored_while_casting(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    ctrl._cast_active = lambda: True
    ctrl._on_playback_error()
    assert ctrl._played == []
    assert ctrl._consecutive_play_errors == 0


# ── Pinned-ALSA busy handling ─────────────────────────────────────────
# 2026-06-11: YouTube playing first (PipeWire holds the device) +
# jellytoast playing second on a pinned alsa/ device produced sound
# through the SHARED mixer — the silent-fallback ladder betrayed the
# exclusivity choice. A pinned direct device that fails while still
# enumerable now stops playback and emits audio_device_busy instead.


def test_pinned_alsa_busy_emits_signal_and_stops(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    out_settings.audio_exclusive = False
    out_settings.audio_output_device = "alsa/front:CARD=A2,DEV=0"
    ctrl.audio_device_choices = lambda: [
        ("alsa/front:CARD=A2,DEV=0", "Audioengine 2+")
    ]
    from unittest.mock import MagicMock

    ctrl.bus = MagicMock()
    ctrl._on_playback_error()
    ctrl.bus.audio_device_busy.emit.assert_called_once_with(
        "alsa/front:CARD=A2,DEV=0"
    )
    assert ctrl._played == []  # no retry, no silent fallback
    assert "audio-device" not in ctrl._mpv.sets


def test_pinned_alsa_busy_notifies_once_per_episode(out_settings, monkeypatch):
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    out_settings.audio_output_device = "alsa/front:CARD=A2,DEV=0"
    ctrl.audio_device_choices = lambda: [
        ("alsa/front:CARD=A2,DEV=0", "Audioengine 2+")
    ]
    from unittest.mock import MagicMock

    ctrl.bus = MagicMock()
    ctrl._on_playback_error()
    ctrl._on_playback_error()
    assert ctrl.bus.audio_device_busy.emit.call_count == 1


def test_pinned_alsa_vanished_falls_back_to_auto(out_settings, monkeypatch):
    """Unplugged (not enumerable) keeps the documented fallback-to-Auto
    story — that's a different situation than 'busy'."""
    np = _FakeNp()
    ctrl = _error_ctrl(out_settings, monkeypatch, np)
    out_settings.audio_output_device = "alsa/front:CARD=GONE,DEV=0"
    ctrl.audio_device_choices = lambda: [("pipewire", "Default (pipewire)")]
    from unittest.mock import MagicMock

    ctrl.bus = MagicMock()
    ctrl._on_playback_error()
    assert ctrl._mpv.sets.get("audio-device") == "auto"
    assert ctrl._played == [np]
    ctrl.bus.audio_device_busy.emit.assert_not_called()


def test_device_busy_dialog_offers_pipewire_escape(qapp):
    from jellytoast.device_busy_dialog import DeviceBusyDialog

    fired = []
    dlg = DeviceBusyDialog(
        device_label="Audioengine 2+",
        on_play_via_pipewire=lambda: fired.append(True),
    )
    try:
        assert "Audioengine 2+" in dlg._message.text()
        assert "exclusively" in dlg._message.text()
        dlg._pipewire_btn.click()
        assert fired == [True]
    finally:
        dlg.deleteLater()


# ── Bit-perfect contention (PipeWire path) ────────────────────────────
# Other apps' streams mixing on the sink while bit-perfect plays =
# sound works, the claim doesn't. Detected via pactl sink-inputs;
# toasted once per episode.


_PACTL_FIXTURE = """Sink Input #61
\tDriver: protocol-native.c
\tCorked: no
\tProperties:
\t\tapplication.name = "Firefox"
\t\tapplication.process.id = "9999"

Sink Input #62
\tDriver: protocol-native.c
\tCorked: no
\tProperties:
\t\tapplication.name = "jellytoast"
\t\tapplication.process.id = "{own}"

Sink Input #63
\tDriver: protocol-native.c
\tCorked: yes
\tProperties:
\t\tapplication.name = "Spotify"
\t\tapplication.process.id = "8888"
"""


def test_foreign_sink_inputs_parsing():
    from jellytoast.player_backend import _foreign_sink_inputs

    text = _PACTL_FIXTURE.format(own=4321)
    # Firefox counts; our own stream and the corked Spotify don't.
    assert _foreign_sink_inputs(text, own_pid=4321) == 1
    # A stream with no readable pid counts as foreign.
    assert _foreign_sink_inputs("Sink Input #9\n\tCorked: no\n", 4321) == 1
    assert _foreign_sink_inputs("", 4321) == 0


def _contention_ctrl(out_settings, monkeypatch, *, foreign=1, family="pipewire"):
    from unittest.mock import MagicMock

    from jellytoast import player_backend as pb
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    ctrl.settings = out_settings
    ctrl.bus = MagicMock()
    ctrl.bus.bit_perfect_active = True
    ctrl.current_output_family = lambda: family
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/pactl")
    monkeypatch.setattr(
        pb, "_foreign_sink_inputs", lambda _t, _p: foreign
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": "x"})(),
    )
    import jellytoast.async_io as aio

    monkeypatch.setattr(
        aio,
        "run_async",
        lambda fn, on_result=None, on_error=None: on_result(fn())
        if on_result
        else fn(),
    )
    return ctrl


def test_contention_toasts_once_per_episode(out_settings, monkeypatch):
    ctrl = _contention_ctrl(out_settings, monkeypatch, foreign=2)
    ctrl._probe_bit_perfect_contention()
    ctrl._probe_bit_perfect_contention()
    assert ctrl.bus.bit_perfect_contested.emit.call_count == 1
    assert ctrl._bp_contested_notified is True


def test_contention_rearms_when_mixer_clears(out_settings, monkeypatch):
    ctrl = _contention_ctrl(out_settings, monkeypatch, foreign=1)
    ctrl._probe_bit_perfect_contention()
    # mixer clears -> flag re-arms
    from jellytoast import player_backend as pb

    monkeypatch.setattr(pb, "_foreign_sink_inputs", lambda _t, _p: 0)
    ctrl._bp_contested_notified = False  # healthy path cleared it... no:
    ctrl._bp_contested_notified = True
    ctrl._probe_bit_perfect_contention()  # short-circuits while notified
    assert ctrl.bus.bit_perfect_contested.emit.call_count == 1


def test_contention_skipped_off_pipewire_or_without_bp(out_settings, monkeypatch):
    ctrl = _contention_ctrl(out_settings, monkeypatch, foreign=3, family="alsa")
    ctrl._probe_bit_perfect_contention()
    ctrl2 = _contention_ctrl(out_settings, monkeypatch, foreign=3)
    ctrl2.bus.bit_perfect_active = False
    ctrl2._probe_bit_perfect_contention()
    assert ctrl.bus.bit_perfect_contested.emit.call_count == 0
    assert ctrl2.bus.bit_perfect_contested.emit.call_count == 0
