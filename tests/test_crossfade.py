"""Tests for the Crossfader state machine.

Mirrors the test-double pattern in ``test_player_backend.py``: mpv is a
``FakeMpv`` so tests run headless without audio. The Crossfader is built
directly (not via ``MpvController``) so its state machine, ramp
arithmetic, and signal-firing contract are exercised in isolation;
end-to-end integration (env flag gating, controller wiring) is covered
by a separate fixture below that uses the real ``MpvController`` with
``MPV_AVAILABLE`` flipped off.
"""

from typing import Any, Dict, List, Optional

import pytest

from modules.player_state import NowPlaying, PlayerBus

# ── Test doubles ────────────────────────────────────────────────────────────


class FakeMpv:
    """Stand-in for ``mpv.MPV``. Same shape as the FakeMpv in
    ``test_player_backend.py`` — ``[]`` for options, attribute access
    for properties, ``play()`` updates path + idle state."""

    def __init__(self):
        self.path: Any = None
        self.idle_active: bool = True
        self.core_idle: bool = True
        self.pause: bool = False
        self.options: Dict[str, Any] = {"volume": 0}
        self.commands: List[tuple] = []
        self.play_calls: List[str] = []
        self.stopped: bool = False
        self.terminated: bool = False

    def __getitem__(self, key):
        # python-mpv exposes properties via both attribute and ``[]``;
        # mirror the few that the crossfade path writes through so a
        # bracket assignment surfaces on the corresponding attribute.
        if key == "pause":
            return self.pause
        return self.options.get(key)

    def __setitem__(self, key, value):
        self.options[key] = value
        if key == "pause":
            self.pause = bool(value)

    def play(self, url):
        self.play_calls.append(url)
        self.path = url
        self.idle_active = False
        self.core_idle = False
        self.stopped = False

    def command(self, *args):
        self.commands.append(args)

    def stop(self):
        self.path = None
        self.idle_active = True
        self.core_idle = True
        self.stopped = True

    def terminate(self):
        self.terminated = True


class FakeSettings:
    """Minimal settings stub for Crossfader. ``MpvController`` reads more
    fields but the Crossfader only needs these four."""

    def __init__(
        self,
        *,
        crossfade_enabled: bool = True,
        crossfade_duration_ms: int = 2000,
        crossfade_smart_album_continuity: bool = True,
        volume: int = 80,
    ):
        self.crossfade_enabled = crossfade_enabled
        self.crossfade_duration_ms = crossfade_duration_ms
        self.crossfade_smart_album_continuity = crossfade_smart_album_continuity
        self.volume = volume


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def factory(qapp, fresh_bus):
    """Build a Crossfader with injected fakes. Returns a callable that
    accepts overrides so each test can tune the setup without a fixture
    explosion."""

    handles_built: List[FakeMpv] = []
    current_holder: Dict[str, Any] = {"handle": FakeMpv(), "item": None, "casting": False}
    swap_log: List[Any] = []

    def _build(**overrides):
        from modules.playback.crossfade import Crossfader

        settings = overrides.pop("settings", FakeSettings())
        # Seed the active handle with a target volume reading.
        current_holder["handle"].options["volume"] = settings.volume
        current_holder["item"] = overrides.pop("current_item", None)
        current_holder["casting"] = overrides.pop("casting", False)

        def _make_handle(volume: int = 0):
            h = FakeMpv()
            h.options["volume"] = volume
            handles_built.append(h)
            return h

        def _swap(new_handle):
            swap_log.append(new_handle)
            current_holder["handle"] = new_handle

        cf = Crossfader(
            bus=PlayerBus.get(),
            settings=settings,
            make_handle=_make_handle,
            get_current_handle=lambda: current_holder["handle"],
            get_current_item=lambda: current_holder["item"],
            is_casting=lambda: current_holder["casting"],
            swap_handles=_swap,
        )
        return cf, current_holder, handles_built, swap_log

    return _build


def _np(item_id: str = "next", url: Optional[str] = None, album_id: str = "") -> NowPlaying:
    raw: Dict[str, Any] = {}
    if album_id:
        raw["AlbumId"] = album_id
    return NowPlaying(
        item_id=item_id,
        title=f"Track {item_id}",
        stream_url=url if url is not None else f"stream://{item_id}",
        item_type="Audio",
        raw=raw,
    )


# ── Setting gating ──────────────────────────────────────────────────────────


class TestControllerGating:
    """Controller-level gate: ``_ensure_crossfader`` returns None unless
    the ``crossfade_enabled`` setting is True, and builds the Crossfader
    otherwise. (The JT_CROSSFADE env gate was dropped when the Settings
    → Playback crossfade controls landed — the setting is now the only
    opt-in.)"""

    def _controller(self, qapp, monkeypatch, tmp_path):
        import modules.player_backend as backend_mod
        import modules.providers as providers_mod
        import modules.settings as settings_mod

        class _FakeProvider:
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

        monkeypatch.setattr(providers_mod, "_PROVIDER", _FakeProvider())
        s = settings_mod.Settings()
        monkeypatch.setattr(s, "_config_dir", tmp_path)
        monkeypatch.setattr(settings_mod, "_settings", s)
        s._s.clear()
        s._s.sync()
        monkeypatch.setattr(backend_mod, "MPV_AVAILABLE", False)
        monkeypatch.setattr(backend_mod, "_MPV_ERROR", "test mode", raising=False)
        PlayerBus._instance = None
        c = backend_mod.MpvController()
        c._mpv = FakeMpv()
        return c

    def test_setting_off_no_crossfader(self, qapp, monkeypatch, tmp_path):
        c = self._controller(qapp, monkeypatch, tmp_path)
        c.settings.crossfade_enabled = False
        assert c._ensure_crossfader() is None
        PlayerBus._instance = None

    def test_setting_on_builds_crossfader(self, qapp, monkeypatch, tmp_path):
        c = self._controller(qapp, monkeypatch, tmp_path)
        c.settings.crossfade_enabled = True
        cf = c._ensure_crossfader()
        assert cf is not None
        # Cached on second call.
        assert c._ensure_crossfader() is cf
        PlayerBus._instance = None


# ── State machine ──────────────────────────────────────────────────────────


class TestStateMachineTransitions:
    def test_idle_then_arms_then_swaps(self, factory):
        from modules.playback.crossfade import CrossfadeState

        cf, holder, handles, swaps = factory()
        cf._on_prefetch_request(_np("next", "stream://next"))
        assert cf.state == CrossfadeState.IDLE

        # Track is 30s long, fade is 2s — trigger threshold is 28s.
        # Tick at 27s: still IDLE.
        cf.on_position(27_000, 30_000)
        assert cf.state == CrossfadeState.IDLE

        # Cross the threshold: arm → crossfade in one call (no readiness
        # gate for v1).
        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.CROSSFADING

        # Force the ramp to completion.
        for _ in range(cf._ticks_total):
            cf._on_tick()

        assert cf.state == CrossfadeState.IDLE
        assert swaps  # swap_handles was called
        # Sibling that was built is now the new "current" via the swap.
        assert swaps[-1] is handles[0]

    def test_smart_album_continuity_skips(self, factory):
        from modules.playback.crossfade import CrossfadeState

        same_album = {"AlbumId": "album-A"}
        cf, holder, handles, swaps = factory(current_item=same_album)
        cf._on_prefetch_request(_np("next", "stream://next", album_id="album-A"))

        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.IDLE
        assert handles == []  # never built a sibling

    def test_smart_album_continuity_off_still_fades(self, factory):
        from modules.playback.crossfade import CrossfadeState

        s = FakeSettings(crossfade_smart_album_continuity=False)
        same_album = {"AlbumId": "album-A"}
        cf, holder, handles, swaps = factory(settings=s, current_item=same_album)
        cf._on_prefetch_request(_np("next", "stream://next", album_id="album-A"))

        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.CROSSFADING

    def test_subsonic_lowercase_album_id_detected(self, factory):
        """Subsonic uses ``albumId`` (camelCase). Same-album short-circuit
        must trigger on it too."""
        from modules.playback.crossfade import CrossfadeState

        cur = {"albumId": "album-S"}
        next_np = NowPlaying(
            item_id="n", stream_url="stream://n", raw={"albumId": "album-S"}
        )
        cf, holder, handles, swaps = factory(current_item=cur)
        cf._on_prefetch_request(next_np)
        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.IDLE

    def test_cast_active_skips(self, factory):
        from modules.playback.crossfade import CrossfadeState

        cf, holder, handles, swaps = factory(casting=True)
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.IDLE
        assert handles == []

    def test_disabled_setting_skips(self, factory):
        from modules.playback.crossfade import CrossfadeState

        s = FakeSettings(crossfade_enabled=False)
        cf, holder, handles, swaps = factory(settings=s)
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.IDLE

    def test_short_track_skips(self, factory):
        """Track shorter than 2× fade window: let normal EOF handle it."""
        from modules.playback.crossfade import CrossfadeState

        # 2s fade, 3s track: not enough room.
        cf, holder, handles, swaps = factory()
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(2_500, 3_000)
        assert cf.state == CrossfadeState.IDLE

    def test_no_next_track_skips(self, factory):
        from modules.playback.crossfade import CrossfadeState

        cf, holder, handles, swaps = factory()
        cf._on_prefetch_request(None)
        cf.on_position(28_000, 30_000)
        assert cf.state == CrossfadeState.IDLE


# ── Ramp arithmetic ─────────────────────────────────────────────────────────


class TestRampArithmetic:
    def test_midpoint_volumes(self, factory):
        """At 1000ms into a 2000ms fade with target=80, the equal-power
        curve puts both handles at ``80 × cos(π/4) = 80 × √2/2 ≈ 57``.
        This is the load-bearing audible difference from the original
        linear ramp: the summed power stays constant across the fade
        instead of dipping ~3 dB at the midpoint, which is what
        prevents the mid-fade dropout on uncorrelated cross-album
        transitions."""
        cf, holder, handles, swaps = factory(settings=FakeSettings(volume=80))
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)

        # Fire half the ticks (1000ms = 20 ticks at 50ms cadence).
        half_ticks = cf._ticks_total // 2
        for _ in range(half_ticks):
            cf._on_tick()

        out_vol = holder["handle"]["volume"]
        in_vol = handles[0]["volume"]
        # ±2 tolerance for rounding/step alignment.
        assert abs(out_vol - 57) <= 2, f"outgoing was {out_vol}, expected ~57"
        assert abs(in_vol - 57) <= 2, f"incoming was {in_vol}, expected ~57"

    def test_full_ramp_lands_at_target(self, factory):
        cf, holder, handles, swaps = factory(settings=FakeSettings(volume=80))
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)

        for _ in range(cf._ticks_total):
            cf._on_tick()

        # After swap, the previously-incoming handle is now "current"
        # and should sit at the target volume.
        new_current = swaps[-1]
        assert new_current["volume"] == 80


class TestEqualPowerCurve:
    """The shape of the curve, independent of the state machine — pinned
    here so a future tweak to ``_equal_power_gains`` (different angle,
    extra easing layer) doesn't silently change what callers hear."""

    def test_endpoints_are_exact(self):
        from modules.playback.crossfade import _equal_power_gains

        # No rounding wobble at the boundaries — the post-swap clamp
        # in `_enter_swap` relies on this being exact.
        assert _equal_power_gains(0.0) == (1.0, 0.0)
        assert _equal_power_gains(1.0) == (0.0, 1.0)

    def test_constant_summed_power(self):
        from modules.playback.crossfade import _equal_power_gains

        # The defining property: ``out² + in² == 1`` everywhere across
        # the fade. This is what keeps perceived loudness flat on
        # uncorrelated content.
        for k in range(0, 21):
            out_g, in_g = _equal_power_gains(k / 20.0)
            assert abs(out_g * out_g + in_g * in_g - 1.0) < 1e-9

    def test_midpoint_is_sqrt_half(self):
        from modules.playback.crossfade import _equal_power_gains

        out_g, in_g = _equal_power_gains(0.5)
        # cos(π/4) == sin(π/4) == √2/2 ≈ 0.7071. Both equal at the
        # midpoint is what makes the crossover symmetric.
        assert abs(out_g - 0.7071067811865) < 1e-9
        assert abs(in_g - 0.7071067811865) < 1e-9


# ── Edge actions ────────────────────────────────────────────────────────────


class TestSkipDuringFade:
    def test_request_skip_hard_cuts(self, factory):
        from modules.playback.crossfade import CrossfadeState

        cf, holder, handles, swaps = factory(settings=FakeSettings(volume=80))
        original = holder["handle"]
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)
        # Tick once so we're partway through; volumes are mid-ramp.
        cf._on_tick()
        assert cf.state == CrossfadeState.CROSSFADING

        cf.request_skip()

        # Outgoing silenced + stopped; incoming at full target; state IDLE.
        assert original.stopped is True
        assert handles[0]["volume"] == 80
        assert cf.state == CrossfadeState.IDLE


class TestPauseResume:
    def test_pause_freezes_ramp(self, factory):
        from modules.playback.crossfade import CrossfadeState

        cf, holder, handles, swaps = factory()
        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)
        # Run a few ticks then pause.
        for _ in range(3):
            cf._on_tick()
        ticks_at_pause = cf._ticks_done
        cf.pause()

        assert holder["handle"].pause is True
        assert handles[0].pause is True
        assert cf._tick_timer.isActive() is False

        # Pretending wall time elapsed; resume picks up right where we left.
        cf.resume()
        assert cf._ticks_done == ticks_at_pause
        assert holder["handle"].pause is False
        assert handles[0].pause is False
        assert cf.state == CrossfadeState.CROSSFADING


# ── Signal firing ───────────────────────────────────────────────────────────


class TestSignals:
    def test_started_on_crossfading_entry(self, factory):
        cf, holder, handles, swaps = factory()
        fired_started: List[bool] = []
        fired_ended: List[bool] = []
        cf._bus.crossfade_started.connect(lambda: fired_started.append(True))
        cf._bus.crossfade_ended.connect(lambda: fired_ended.append(True))

        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)

        assert fired_started == [True]
        assert fired_ended == []  # not yet

        for _ in range(cf._ticks_total):
            cf._on_tick()

        assert fired_ended == [True]

    def test_abort_does_not_fire_ended(self, factory):
        cf, holder, handles, swaps = factory()
        fired_ended: List[bool] = []
        cf._bus.crossfade_ended.connect(lambda: fired_ended.append(True))

        cf._on_prefetch_request(_np("next", "stream://next"))
        cf.on_position(28_000, 30_000)
        cf._on_tick()
        cf.abort()

        assert fired_ended == []
