"""Regression tests for the #234 audit batch (findings 1–5).

1. ``stop_cast`` must never block the GUI thread on DLNA/Sonos SOAP —
   the session drops immediately, the network goodbye runs off-thread.
2. ``play_now`` invalidates an in-flight radio refill (same as ``clear``)
   so a stale batch can't append into a replacement queue.
3. ``toggle_mute`` mid-crossfade retargets the fade (else the 50 ms ramp
   tick undoes it) and stashes the user baseline, not a ramp transient.
4. A prefetch request arriving mid-crossfade is dropped — appending would
   re-arm the doubled-audio path ``_on_crossfade_started`` just cleared.
5. MPRIS connects its Qt signals even when the D-Bus ready-wait times
   out, so a slow session bus degrades to late instead of dead.
"""

from types import SimpleNamespace
from typing import List

import pytest

from jellytoast.player_state import NowPlaying, PlayerBus

# ── Finding 1: stop_cast off-thread ─────────────────────────────────────────


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def cast_mgr(qapp, fresh_bus):
    from jellytoast.cast_manager._manager import CastManager

    return CastManager()


def _device(kind, cast_object=None):
    from jellytoast.cast_manager import CastType
    from jellytoast.cast_manager._common import CastDevice

    return CastDevice(
        name="R",
        host="10.0.0.9",
        port=1400,
        device_type=CastType(kind),
        cast_object=cast_object,
    )


class TestStopCastNonBlocking:
    def test_dlna_stop_drops_session_before_network(self, cast_mgr):
        deferred: List = []
        cast_mgr._run_off_thread = deferred.append  # capture, don't run
        cast_mgr.active_cast = _device("dlna")

        cast_mgr.dlna_stop()

        # The GUI-thread contract: session gone NOW, network deferred.
        assert cast_mgr.active_cast is None
        assert len(deferred) == 1

    def test_sonos_stop_captures_zone_for_the_network_goodbye(self, cast_mgr, monkeypatch):
        zone = object()
        deferred: List = []
        cast_mgr._run_off_thread = deferred.append
        cast_mgr.active_cast = _device("sonos", cast_object=zone)

        cast_mgr.sonos_stop()

        assert cast_mgr.active_cast is None
        assert len(deferred) == 1
        # Running the deferred network call must still reach stop_sonos
        # with the zone captured before the session was dropped.
        stopped: List = []
        import jellytoast.cast.sonos as sonos_mod

        monkeypatch.setattr(sonos_mod, "stop_sonos", stopped.append)
        deferred[0]()
        assert stopped == [zone]

    def test_sonos_stop_without_session_is_a_noop(self, cast_mgr):
        deferred: List = []
        cast_mgr._run_off_thread = deferred.append
        cast_mgr.active_cast = None

        cast_mgr.sonos_stop()

        assert deferred == []


# ── Finding 2: play_now invalidates in-flight radio refill ──────────────────


# The queue fixture set mirrors tests/test_queue_manager.py.


class _FakeProvider:
    kind = "fake"

    def get_audio_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_video_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_image_url(self, item_id, image_type, width):
        return f"img://{item_id}"


@pytest.fixture
def fake_provider(monkeypatch):
    import jellytoast.providers as providers_mod

    fp = _FakeProvider()
    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def qm(qapp, fake_provider, isolated_settings, fresh_bus, monkeypatch):
    import jellytoast.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_settings", isolated_settings)
    from jellytoast.queue_manager import QueueManager

    return QueueManager()


def _items(n, base="id"):
    return [{"Id": f"{base}{i}", "Name": f"T{i}", "Type": "Audio"} for i in range(n)]


class TestRadioRefillRace:
    def test_play_now_bumps_generation(self, qm):
        qm.play_now(_items(3))
        gen = qm._refill_gen
        qm.play_now(_items(2, base="new"))
        assert qm._refill_gen == gen + 1

    def test_stale_batch_is_dropped_after_replacement(self, qm):
        # A refill dispatched against queue A must not append into queue B.
        qm.play_now(_items(3))
        stale_gen = qm._refill_gen
        qm.play_now(_items(2, base="new"))
        before = [it["Id"] for it in qm._q.original_items]
        qm._on_refill_result(_items(5, base="stale"), set(), set(), stale_gen)
        assert [it["Id"] for it in qm._q.original_items] == before


# ── Findings 3+4: player backend (fake mpv, fake crossfader) ────────────────


class _FakeMpv:
    def __init__(self):
        self.options = {"volume": 70}
        self.commands: List[tuple] = []
        self.path = "stream://current"
        self.idle_active = False
        self.playlist_count = 1
        self.playlist_pos = 0

    def __getitem__(self, key):
        return self.options.get(key)

    def __setitem__(self, key, value):
        self.options[key] = value

    def command(self, *args):
        self.commands.append(args)


@pytest.fixture
def controller(qapp, fake_provider, isolated_settings, fresh_bus, monkeypatch):
    import jellytoast.player_backend as backend_mod
    import jellytoast.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_settings", isolated_settings)
    monkeypatch.setattr(backend_mod, "MPV_AVAILABLE", False)
    monkeypatch.setattr(backend_mod, "_MPV_ERROR", "test mode", raising=False)
    from jellytoast.player_backend import MpvController

    c = MpvController()
    c._mpv = _FakeMpv()
    monkeypatch.setattr(c, "_cast_active", lambda: False)
    return c


def _fading(calls):
    from jellytoast.playback.crossfade import CrossfadeState

    return SimpleNamespace(
        state=CrossfadeState.CROSSFADING,
        set_target_volume=calls.append,
    )


class TestMuteDuringCrossfade:
    def test_mute_retargets_fade_and_stashes_baseline(self, controller, isolated_settings):
        isolated_settings.volume = 70
        controller._mpv["volume"] = 37  # mid-ramp transient
        retargets: List[int] = []
        controller._crossfader = _fading(retargets)

        controller.toggle_mute()

        # Stash is the user's baseline, not the 37 the ramp happened to
        # be passing through; the fade itself is retargeted to 0 so the
        # next 50 ms tick doesn't undo the mute.
        assert controller._muted_volume == 70
        assert controller._mpv["volume"] == 0
        assert retargets == [0]

        controller.toggle_mute()

        assert controller._muted_volume is None
        assert controller._mpv["volume"] == 70
        assert retargets == [0, 70]

    def test_mute_outside_fade_still_stashes_live_volume(self, controller):
        from jellytoast.playback.crossfade import CrossfadeState

        controller._mpv["volume"] = 55
        controller._crossfader = SimpleNamespace(
            state=CrossfadeState.IDLE,
            set_target_volume=lambda v: None,
        )

        controller.toggle_mute()

        assert controller._muted_volume == 55
        assert controller._mpv["volume"] == 0


class TestPrefetchDuringCrossfade:
    def _np(self, item_id="next"):
        return NowPlaying(
            item_id=item_id,
            title="Next",
            stream_url=f"stream://{item_id}",
            item_type="Audio",
        )

    def test_prefetch_dropped_while_crossfading(self, controller):
        controller._crossfader = _fading([])

        controller._on_prefetch_request(self._np())

        appends = [c for c in controller._mpv.commands if c[0] == "loadfile"]
        assert appends == []

    def test_prefetch_appends_when_not_fading(self, controller):
        from jellytoast.playback.crossfade import CrossfadeState

        controller._crossfader = SimpleNamespace(
            state=CrossfadeState.IDLE,
            set_target_volume=lambda v: None,
        )

        controller._on_prefetch_request(self._np())

        appends = [c for c in controller._mpv.commands if c[0] == "loadfile"]
        assert len(appends) == 1
        assert appends[0][1] == "stream://next"


# ── Finding 5: MPRIS wires signals even on ready-timeout ────────────────────


class TestMprisReadyTimeout:
    def test_signals_connect_when_ready_wait_times_out(self, qapp, fresh_bus, monkeypatch):
        from jellytoast.media_controls._mpris import MprisService

        svc = MprisService()
        # Loop thread that never becomes ready (slow session bus).
        monkeypatch.setattr(svc, "_run_loop", lambda: None)
        svc._ready = SimpleNamespace(wait=lambda timeout=None: False)
        connected: List[bool] = []
        monkeypatch.setattr(svc, "_connect_signals", lambda: connected.append(True))

        svc.start()

        assert connected == [True]

    def test_slots_noop_before_player_exists(self, qapp, fresh_bus):
        from jellytoast.media_controls._mpris import MprisService

        svc = MprisService()
        # The unconditional connect is only safe because every slot
        # guards on _player/_loop — pin that contract.
        svc._update_status("Playing")
        svc._on_position(1234)
        svc._on_volume(50)
