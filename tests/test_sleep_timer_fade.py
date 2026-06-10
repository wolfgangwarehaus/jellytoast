"""Tests for the fade-to-stop sleep-timer ramp on MpvController.

These cover the volume-ramp helper, mid-fade cancel, post-ramp pause
+ volume restore, and the playback/sleep_fade_duration_ms setting
clamp. Ramp ticks are exercised by driving the QTimer's `timeout`
signal directly — no wall-clock waits.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from jellytoast.player_state import (
    NowPlaying,
    PlayerBus,
    set_now_playing,
)

# ── Test doubles ────────────────────────────────────────────────────────────


class FakeMpv:
    def __init__(self) -> None:
        self.path: Any = None
        self.idle_active: bool = True
        self.core_idle: bool = True
        self.pause: bool = False
        self.options: Dict[str, Any] = {"volume": 80}

    def __getitem__(self, key):
        return self.options.get(key)

    def __setitem__(self, key, value):
        self.options[key] = value

    def stop(self):
        self.path = None
        self.idle_active = True

    def terminate(self):
        pass


class FakeProvider:
    kind = "fake"

    def get_audio_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_video_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_image_url(self, item_id, image_type, width):
        return f"img://{item_id}"

    def report_playback_start(self, *a, **kw): ...
    def report_playback_progress(self, *a, **kw): ...
    def report_playback_stopped(self, *a, **kw): ...
    def get_audio_transcode_url(self, *a, **kw):
        return ""


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_provider(monkeypatch):
    fp = FakeProvider()
    import jellytoast.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(isolated_settings):
    # Canonical tmp_path-backed Settings pinned as the get_settings()
    # singleton with QSettings cleared — see tests/conftest.py.
    return isolated_settings


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def controller(qapp, fake_provider, isolated_settings_singleton, fresh_bus, monkeypatch):
    import jellytoast.player_backend as backend_mod

    monkeypatch.setattr(backend_mod, "MPV_AVAILABLE", False)
    monkeypatch.setattr(backend_mod, "_MPV_ERROR", "test mode", raising=False)
    from jellytoast.player_backend import MpvController

    c = MpvController()
    c._mpv = FakeMpv()
    c._connect_bus()
    set_now_playing(NowPlaying())
    return c


def _drain_fade(controller, max_ticks: int = 10000) -> int:
    """Tick the fade timer until it stops or `max_ticks` is reached.
    Returns the number of ticks fired."""
    ticks = 0
    while controller._sleep_fade_timer is not None and ticks < max_ticks:
        controller._on_sleep_fade_tick()
        ticks += 1
    return ticks


# ── Setting clamp ───────────────────────────────────────────────────────────


class TestSleepFadeSetting:
    def test_default_is_eight_seconds(self, isolated_settings_singleton):
        assert isolated_settings_singleton.sleep_fade_duration_ms == 8000

    def test_below_min_clamps_to_min(self, isolated_settings_singleton):
        isolated_settings_singleton.sleep_fade_duration_ms = 500
        assert isolated_settings_singleton.sleep_fade_duration_ms == 1000

    def test_above_max_clamps_to_max(self, isolated_settings_singleton):
        isolated_settings_singleton.sleep_fade_duration_ms = 100000
        assert isolated_settings_singleton.sleep_fade_duration_ms == 60000

    def test_within_range_is_preserved(self, isolated_settings_singleton):
        isolated_settings_singleton.sleep_fade_duration_ms = 15000
        assert isolated_settings_singleton.sleep_fade_duration_ms == 15000


# ── Ramp behaviour ─────────────────────────────────────────────────────────


class TestSleepFadeRamp:
    def test_ramp_decrements_monotonically(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "pause", lambda: None)
        controller._mpv.options["volume"] = 100

        controller._fade_volume_to_zero_then_pause(1000)
        readings: List[int] = []
        # Drive a handful of ticks and snapshot the mpv volume each time.
        for _ in range(5):
            if controller._sleep_fade_timer is None:
                break
            controller._on_sleep_fade_tick()
            readings.append(int(controller._mpv.options["volume"]))

        assert all(readings[i] >= readings[i + 1] for i in range(len(readings) - 1))
        assert readings[0] < 100  # first tick already started the descent

    def test_ramp_pauses_once_at_end(self, controller, monkeypatch):
        pause_calls: List[bool] = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        controller._mpv.options["volume"] = 60

        controller._fade_volume_to_zero_then_pause(1000)
        ticks = _drain_fade(controller)

        assert ticks > 0
        assert pause_calls == [True]
        assert controller._sleep_fade_timer is None

    def test_ramp_restores_original_volume(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "pause", lambda: None)
        controller._mpv.options["volume"] = 75

        controller._fade_volume_to_zero_then_pause(1000)
        _drain_fade(controller)

        assert controller._mpv.options["volume"] == 75

    def test_short_duration_still_yields_a_pause(self, controller, monkeypatch):
        # Duration shorter than the tick interval is clamped up so the
        # ramp still has at least one tick before pausing.
        pause_calls: List[bool] = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        controller._mpv.options["volume"] = 30

        controller._fade_volume_to_zero_then_pause(1)
        _drain_fade(controller)

        assert pause_calls == [True]
        assert controller._mpv.options["volume"] == 30


# ── Mid-fade cancel ─────────────────────────────────────────────────────────


class TestSleepFadeCancel:
    def test_cancel_mid_fade_restores_volume(self, controller, monkeypatch):
        pause_calls: List[bool] = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        controller._mpv.options["volume"] = 80

        controller.start_sleep_timer(30, on_fire="fade_stop")
        controller._on_sleep_timer_elapsed()
        # Tick a couple times to drop the volume mid-ramp.
        controller._on_sleep_fade_tick()
        controller._on_sleep_fade_tick()
        assert controller._mpv.options["volume"] < 80

        controller.cancel_sleep_timer()

        assert controller._sleep_fade_timer is None
        assert controller._mpv.options["volume"] == 80
        assert pause_calls == []

    def test_cancel_mid_fade_emits_cancelled_once(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "pause", lambda: None)
        cancelled: List[tuple] = []
        controller.bus.sleep_timer_cancelled.connect(lambda *a: cancelled.append(a))
        controller._mpv.options["volume"] = 80

        controller.start_sleep_timer(30, on_fire="fade_stop")
        controller._on_sleep_timer_elapsed()
        controller._on_sleep_fade_tick()
        controller.cancel_sleep_timer()

        assert len(cancelled) == 1

    def test_cancel_with_no_fade_active_is_noop(self, controller):
        cancelled: List[tuple] = []
        controller.bus.sleep_timer_cancelled.connect(lambda *a: cancelled.append(a))

        controller.cancel_sleep_timer()

        assert cancelled == []


# ── Integration with the elapsed slot ──────────────────────────────────────


class TestFadeElapsedIntegration:
    def test_elapsed_uses_settings_duration(
        self, controller, monkeypatch, isolated_settings_singleton
    ):
        monkeypatch.setattr(controller, "pause", lambda: None)
        controller._mpv.options["volume"] = 100
        isolated_settings_singleton.sleep_fade_duration_ms = 2000

        controller.start_sleep_timer(30, on_fire="fade_stop")
        controller._on_sleep_timer_elapsed()

        # 2000ms / 50ms tick = 40 steps. Each step drops volume by 2.5.
        assert controller._sleep_fade_steps_remaining == 40
        controller._on_sleep_fade_tick()
        # After two ticks the mpv volume should sit around 95.
        controller._on_sleep_fade_tick()
        assert 93 <= controller._mpv.options["volume"] <= 98
