"""Tests for the sleep-timer scaffold on MpvController.

Mirrors the mocking pattern from `test_player_backend.py`: MPV_AVAILABLE
is flipped off, a `FakeMpv` is injected, `_connect_bus` is called
manually. The QTimer itself is real (Qt-owned), so timing-dependent
fires are exercised by zero-second starts followed by manual signal
emission — no `qtbot.waitUntil` ceremony, no real wall-clock waits.
"""

from typing import Any, Dict, List

import pytest

from modules.player_state import (
    NowPlaying,
    PlayerBus,
    set_now_playing,
)


# ── Test doubles ────────────────────────────────────────────────────────────


class FakeMpv:
    def __init__(self):
        self.path: Any = None
        self.idle_active: bool = True
        self.core_idle: bool = True
        self.pause: bool = False
        self.playlist_count: int = 0
        self.playlist_pos: int = -1
        self.options: Dict[str, Any] = {}
        self.commands: List[tuple] = []
        self.play_calls: List[str] = []

    def __getitem__(self, key):
        return self.options.get(key)

    def __setitem__(self, key, value):
        self.options[key] = value

    def play(self, url):
        self.play_calls.append(url)
        self.path = url
        self.idle_active = False
        self.core_idle = False
        self.playlist_count = 1
        self.playlist_pos = 0

    def command(self, *args):
        self.commands.append(args)

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
    import modules.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(tmp_path, monkeypatch):
    import modules.settings as settings_mod

    s = settings_mod.Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    monkeypatch.setattr(settings_mod, "_settings", s)
    s._s.clear()
    s._s.sync()
    return s


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def controller(qapp, fake_provider, isolated_settings_singleton, fresh_bus, monkeypatch):
    import modules.player_backend as backend_mod

    monkeypatch.setattr(backend_mod, "MPV_AVAILABLE", False)
    monkeypatch.setattr(backend_mod, "_MPV_ERROR", "test mode", raising=False)
    from modules.player_backend import MpvController

    c = MpvController()
    c._mpv = FakeMpv()
    c._connect_bus()
    set_now_playing(NowPlaying())
    return c


def _capture(signal) -> List:
    out: List = []
    signal.connect(lambda *args: out.append(args))
    return out


# ── Start / cancel ──────────────────────────────────────────────────────────


class TestSleepTimerStart:
    def test_start_emits_started_with_seconds(self, controller):
        started = _capture(controller.bus.sleep_timer_started)
        controller.start_sleep_timer(1800, on_fire="pause")
        assert started == [(1800,)]
        assert controller._sleep_timer is not None

    def test_invalid_mode_falls_back_to_pause(self, controller):
        controller.start_sleep_timer(60, on_fire="bogus")
        assert controller._sleep_on_fire == "pause"


class TestSleepTimerCancel:
    def test_cancel_before_fire(self, controller):
        cancelled = _capture(controller.bus.sleep_timer_cancelled)
        fired = _capture(controller.bus.sleep_timer_fired)

        controller.start_sleep_timer(1800, on_fire="pause")
        controller.cancel_sleep_timer()

        assert len(cancelled) == 1
        assert fired == []
        assert controller._sleep_timer is None

    def test_cancel_when_inactive_is_noop(self, controller):
        cancelled = _capture(controller.bus.sleep_timer_cancelled)
        controller.cancel_sleep_timer()
        assert cancelled == []


# ── Fire actions ────────────────────────────────────────────────────────────


class TestSleepTimerFire:
    def test_pause_mode_fires_and_pauses(self, controller, monkeypatch):
        pause_calls = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        fired = _capture(controller.bus.sleep_timer_fired)

        controller.start_sleep_timer(30, on_fire="pause")
        controller._on_sleep_timer_elapsed()

        assert len(fired) == 1
        assert pause_calls == [True]
        assert controller._sleep_timer is None

    def test_fade_stop_mode_starts_fade_ramp(self, controller, monkeypatch):
        # In fade_stop mode, elapsed must start a volume ramp and NOT
        # call pause synchronously — pause comes after the ramp drains.
        pause_calls = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        fired = _capture(controller.bus.sleep_timer_fired)
        controller._mpv.options["volume"] = 60

        controller.start_sleep_timer(30, on_fire="fade_stop")
        controller._on_sleep_timer_elapsed()

        assert len(fired) == 1
        assert pause_calls == []
        assert controller._sleep_fade_timer is not None

    def test_fade_stop_mode_skips_fade_when_casting(self, controller, monkeypatch):
        # When a cast is active mpv's volume isn't what's audible, so
        # fade_stop degrades to an immediate pause.
        pause_calls = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        monkeypatch.setattr(controller, "_cast_active", lambda: True)
        controller._mpv.options["volume"] = 80

        controller.start_sleep_timer(30, on_fire="fade_stop")
        controller._on_sleep_timer_elapsed()

        assert pause_calls == [True]
        assert controller._sleep_fade_timer is None

    def test_end_of_track_defers_pause(self, controller, monkeypatch):
        # Timer elapsed → flag set, no pause yet. The next
        # playback_ended emit pauses.
        pause_calls = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))
        fired = _capture(controller.bus.sleep_timer_fired)

        controller.start_sleep_timer(10, on_fire="end_of_track")
        controller._on_sleep_timer_elapsed()

        assert len(fired) == 1
        assert pause_calls == []
        assert controller._sleep_pending_eot is True

        controller.bus.playback_ended.emit()
        assert pause_calls == [True]
        assert controller._sleep_pending_eot is False

    def test_end_of_track_no_pause_without_arm(self, controller, monkeypatch):
        # Without an active end_of_track arming, playback_ended must
        # not trigger our pause path.
        pause_calls = []
        monkeypatch.setattr(controller, "pause", lambda: pause_calls.append(True))

        controller.bus.playback_ended.emit()
        assert pause_calls == []


# ── Idempotent restart ──────────────────────────────────────────────────────


class TestSleepTimerRestart:
    def test_restart_cancels_previous_timer(self, controller):
        cancelled = _capture(controller.bus.sleep_timer_cancelled)
        started = _capture(controller.bus.sleep_timer_started)

        controller.start_sleep_timer(60, on_fire="pause")
        first_timer = controller._sleep_timer

        controller.start_sleep_timer(120, on_fire="end_of_track")
        second_timer = controller._sleep_timer

        assert len(cancelled) == 1
        assert started == [(60,), (120,)]
        assert second_timer is not first_timer
        assert controller._sleep_on_fire == "end_of_track"

    def test_restart_clears_pending_eot(self, controller):
        controller.start_sleep_timer(10, on_fire="end_of_track")
        controller._on_sleep_timer_elapsed()
        assert controller._sleep_pending_eot is True

        controller.start_sleep_timer(60, on_fire="pause")
        assert controller._sleep_pending_eot is False
