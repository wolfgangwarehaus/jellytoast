"""Group-cast reliability: surviving a dropped receiver connection (the
blank status pychromecast swaps in, a lost IDLE/FINISHED), not stacking
duplicate speaker connections across discovery sweeps, collapsing volume
slider floods, and one retry when the next track can't reach the group."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jellytoast.cast_manager import CastDevice, CastType
from jellytoast.cast_manager._common import _Throttle
from jellytoast.player_cast_transport import _CastTransportMixin

_apply = _CastTransportMixin._apply_cast_status
_overdue = _CastTransportMixin._check_cast_track_overdue


def _stub(now=1000.0, **extra):
    s = SimpleNamespace(
        _cast_last_player_state=None,
        _cast_last_duration_ms=-1,
        _cast_last_position_ms=-1,
        _cast_anchor_pos_ms=0,
        _cast_anchor_wall=0.0,
        _cast_end_poke_wall=0.0,
        _monotonic=lambda: now,
        _on_position=MagicMock(),
        _on_duration=MagicMock(),
        _on_paused=MagicMock(),
        _on_ended=MagicMock(),
        _cast_manager=None,
        _CAST_END_GRACE_S=_CastTransportMixin._CAST_END_GRACE_S,
        _CAST_END_POKE_S=_CastTransportMixin._CAST_END_POKE_S,
        _CAST_END_GIVE_UP_S=_CastTransportMixin._CAST_END_GIVE_UP_S,
    )
    for k, v in extra.items():
        setattr(s, k, v)
    return s


def _st(player_state, current_time=None, duration=None, idle_reason=None):
    return SimpleNamespace(
        player_state=player_state, current_time=current_time,
        duration=duration, idle_reason=idle_reason,
    )


class TestDisconnectPlaceholder:
    def test_blank_status_keeps_state_and_position(self):
        s = _stub(_cast_last_player_state="PLAYING", _cast_last_position_ms=120_000)
        _apply(s, _st("UNKNOWN", current_time=0.0))
        assert s._cast_last_player_state == "PLAYING"
        s._on_position.assert_not_called()
        s._on_paused.assert_not_called()

    def test_finish_after_a_reconnect_still_advances(self):
        # The bug: UNKNOWN replaced PLAYING, so the FINISHED that came after
        # the reconnect failed the "followed playback" gate.
        s = _stub(_cast_last_player_state="PLAYING")
        _apply(s, _st("UNKNOWN", current_time=0.0))
        _apply(s, _st("IDLE", idle_reason="FINISHED"))
        s._on_ended.assert_called_once()

    def test_reasonless_idle_leaves_it_to_the_watchdog(self):
        s = _stub(_cast_last_player_state="PLAYING", _cast_last_position_ms=170_000)
        _apply(s, _st("IDLE", current_time=0.0))
        assert s._cast_last_player_state == "PLAYING"
        s._on_ended.assert_not_called()
        s._on_position.assert_not_called()

    @pytest.mark.parametrize("reason", ["CANCELLED", "INTERRUPTED", "ERROR"])
    def test_explicit_stop_reasons_unchanged(self, reason):
        s = _stub(_cast_last_player_state="PLAYING")
        _apply(s, _st("IDLE", idle_reason=reason))
        assert s._cast_last_player_state == "IDLE"
        s._on_ended.assert_not_called()


class TestTrackEndWatchdog:
    def _playing(self, past_end_s, **extra):
        # 3:00 track, anchored at 2:50 ten seconds + past_end_s ago.
        now = 1000.0
        fields = dict(
            _cast_last_player_state="PLAYING",
            _cast_last_duration_ms=180_000,
            _cast_anchor_pos_ms=170_000,
            _cast_anchor_wall=now - 10.0 - past_end_s,
        )
        fields.update(extra)
        return _stub(now=now, **fields)

    def _cc(self):
        return SimpleNamespace(media_controller=MagicMock())

    def test_quiet_before_the_grace(self):
        s, cc = self._playing(2), self._cc()
        _overdue(s, cc, _st("PLAYING"))
        s._on_ended.assert_not_called()
        cc.media_controller.update_status.assert_not_called()

    def test_asks_the_receiver_when_overdue(self):
        s, cc = self._playing(6), self._cc()
        _overdue(s, cc, _st("UNKNOWN"))
        cc.media_controller.update_status.assert_called_once()
        s._on_ended.assert_not_called()
        _overdue(s, cc, _st("UNKNOWN"))  # throttled: same instant
        cc.media_controller.update_status.assert_called_once()

    def test_advances_once_the_receiver_says_idle(self):
        s, cc = self._playing(6), self._cc()
        _overdue(s, cc, _st("IDLE"))
        s._on_ended.assert_called_once()
        _overdue(s, cc, _st("IDLE"))  # state moved off PLAYING: once only
        s._on_ended.assert_called_once()

    def test_gives_up_waiting(self):
        s, cc = self._playing(25), self._cc()
        _overdue(s, cc, _st("PLAYING"))
        s._on_ended.assert_called_once()

    def test_live_stream_never_trips(self):
        s, cc = self._playing(60, _cast_last_duration_ms=-1), self._cc()
        _overdue(s, cc, _st("UNKNOWN"))
        s._on_ended.assert_not_called()

    def test_paused_never_trips(self):
        s, cc = self._playing(60, _cast_last_player_state="PAUSED"), self._cc()
        _overdue(s, cc, _st("PAUSED"))
        s._on_ended.assert_not_called()


def _cc_dev(uuid, host="10.0.0.2", port=8009, name="Speaker"):
    return CastDevice(
        name=name, host=host, port=port, device_type=CastType.CHROMECAST,
        uuid=uuid, cast_object=MagicMock(name=f"cc-{uuid}"),
    )


class TestDiscoveryKeepsHandles:
    @pytest.fixture
    def mgr(self):
        from jellytoast.cast_manager import CastManager

        return CastManager()

    def test_known_speaker_keeps_its_handle(self, mgr):
        old = _cc_dev("a")
        mgr.chromecast_devices = [old]
        fresh = _cc_dev("a", name="Kitchen")
        merged = mgr._merge_chromecast_handles([fresh])
        assert merged[0] is old
        assert old.name == "Kitchen"
        old.cast_object.disconnect.assert_not_called()

    def test_dropped_and_moved_handles_are_disconnected(self, mgr):
        gone, moved = _cc_dev("a"), _cc_dev("b")
        mgr.chromecast_devices = [gone, moved]
        new_b = _cc_dev("b", host="10.0.0.9")
        merged = mgr._merge_chromecast_handles([new_b])
        assert merged == [new_b]
        gone.cast_object.disconnect.assert_called_once()
        moved.cast_object.disconnect.assert_called_once()

    def test_active_cast_is_never_disconnected(self, mgr):
        group = _cc_dev("g")
        mgr.chromecast_devices = [group]
        mgr.active_cast = group
        mgr._merge_chromecast_handles([])
        group.cast_object.disconnect.assert_not_called()


class TestThrottle:
    def test_leading_then_latest_trailing(self):
        sent = []
        t = _Throttle(interval=0.05)
        for v in range(10):
            t.submit("k", v, sent.append)
        assert sent == [0]
        time.sleep(0.15)
        assert sent == [0, 9]

    def test_keys_are_independent(self):
        sent = []
        t = _Throttle(interval=10)
        t.submit("a", 1, sent.append)
        t.submit("b", 2, sent.append)
        assert sent == [1, 2]

    def test_send_errors_are_contained(self):
        t = _Throttle(interval=0.01)

        def boom(_v):
            raise OSError("socket closed")

        t.submit("k", 1, boom)  # no raise


def _np(title="Track"):
    return SimpleNamespace(
        item_id="i1", title=title, subtitle="", album="", thumb_url="",
        stream_url="http://x/s", is_audio=True, raw={"Container": "mp3"},
        is_paused=True,
    )


def _auto_advance_ctrl(monkeypatch, cm):
    from jellytoast import player_backend
    from jellytoast.player_backend import MpvController

    monkeypatch.setattr(player_backend.QTimer, "singleShot", lambda _ms, fn: fn())
    ctrl = MpvController.__new__(MpvController)
    ctrl._cast_manager = cm
    ctrl._cast_active = lambda: True
    ctrl.api = SimpleNamespace()
    ctrl._monotonic = lambda: 0.0
    ctrl._cast_attempt = 0
    ctrl.bus = SimpleNamespace(
        cast_stopped=SimpleNamespace(emit=lambda: None),
        playback_started=SimpleNamespace(emit=lambda _np: None),
    )
    ctrl._begin_play_session = lambda _np: None
    ctrl._report_session_start = lambda _np: None
    for attr in (
        "_cast_last_player_state", "_cast_last_duration_ms",
        "_cast_last_position_ms", "_cast_anchor_pos_ms", "_cast_anchor_wall",
        "_last_streaming_emit_t", "_last_radio_title",
    ):
        setattr(ctrl, attr, None)
    return ctrl


class TestAutoAdvanceRetry:
    def _cm(self, results, failure):
        calls = []
        cm = SimpleNamespace(
            active_cast=_cc_dev("g"), last_cast_failure=failure, stopped=0,
        )

        def start_track(dev, np, **kw):
            calls.append(dev)
            kw["on_done"](results[len(calls) - 1])

        cm.start_track = start_track
        cm.stop_cast = lambda: setattr(cm, "stopped", cm.stopped + 1)
        return cm, calls

    def test_unreachable_retries_once_then_plays(self, qapp, monkeypatch):
        cm, calls = self._cm([False, True], "unreachable")
        ctrl = _auto_advance_ctrl(monkeypatch, cm)
        ctrl.play(_np())
        assert len(calls) == 2
        assert cm.stopped == 0

    def test_unreachable_twice_falls_back(self, qapp, monkeypatch):
        monkeypatch.setattr("jellytoast.notifications.notify", lambda *a, **k: None)
        cm, calls = self._cm([False, False], "unreachable")
        ctrl = _auto_advance_ctrl(monkeypatch, cm)
        ctrl.play(_np())
        assert len(calls) == 2
        assert cm.stopped == 1

    def test_rejected_media_is_not_retried(self, qapp, monkeypatch):
        monkeypatch.setattr("jellytoast.notifications.notify", lambda *a, **k: None)
        cm, calls = self._cm([False], None)
        ctrl = _auto_advance_ctrl(monkeypatch, cm)
        ctrl.play(_np())
        assert len(calls) == 1
        assert cm.stopped == 1


class TestCommandWorker:
    """pause / play / seek / volume block on the receiver's reply (up to
    10 s); they run on one background worker, in order, and pause / play /
    seek retry across a reconnect."""

    @pytest.fixture
    def mgr(self):
        from jellytoast.cast_manager import CastManager

        m = CastManager()
        m._CC_RETRY_DELAY_S = 0.01
        yield m
        m._cc_queue.shutdown(wait=True)

    @pytest.mark.real_cast_queue
    def test_runs_off_the_calling_thread_in_order(self, mgr):
        import threading

        seen = []
        me = threading.get_ident()
        for i in range(5):
            mgr._cc_send(lambda i=i: seen.append((i, threading.get_ident() != me)), "x")
        mgr._cc_drain()
        assert seen == [(i, True) for i in range(5)]

    @pytest.mark.real_cast_queue
    def test_retries_a_press_lost_to_a_reconnect(self, mgr):
        from pychromecast.error import RequestFailed

        tries = []

        def pause():
            tries.append(1)
            if len(tries) < 2:
                raise RequestFailed("pause")

        mgr._cc_send(pause, "pause", retries=2)
        mgr._cc_drain()
        assert len(tries) == 2

    @pytest.mark.real_cast_queue
    def test_gives_up_quietly(self, mgr, caplog):
        import logging

        def boom():
            raise OSError("down")

        with caplog.at_level(logging.WARNING):
            mgr._cc_send(boom, "seek", retries=1)
            mgr._cc_drain()
        assert "Chromecast seek failed" in caplog.text

    @pytest.mark.real_cast_queue
    def test_stop_waits_for_queued_commands(self, mgr):
        import threading

        order = []
        gate = threading.Event()
        cc = MagicMock()
        cc.media_controller.stop.side_effect = lambda: order.append("stop")
        mgr.active_cast = SimpleNamespace(device_type=CastType.CHROMECAST, cast_object=cc)
        mgr._cc_send(lambda: (gate.wait(1), order.append("volume")), "volume")
        gate.set()
        mgr.chromecast_stop()
        assert order == ["volume", "stop"]

    def test_pause_does_not_call_the_receiver_inline(self, mgr, monkeypatch):
        # The press only queues; the blocking call happens on the worker.
        queued = []
        monkeypatch.setattr(mgr, "_cc_send", lambda fn, what, retries=0: queued.append(what))
        cc = MagicMock()
        cc.media_controller.status.player_state = "PLAYING"
        mgr.active_cast = SimpleNamespace(device_type=CastType.CHROMECAST, cast_object=cc)
        mgr.chromecast_pause()
        mgr.chromecast_seek(30)
        assert queued == ["pause", "seek"]
        cc.media_controller.pause.assert_not_called()
