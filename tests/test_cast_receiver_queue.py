"""Chromecast receiver queue: the app's next track waits queued on the
receiver, which moves on by itself; the app follows its status, keeps the
queued item in step with queue edits, and falls back to pushing the next
track whenever the receiver's queue can't be trusted."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import jellytoast.cast_manager as _cm_pkg
from jellytoast.cast_manager import CastDevice, CastManager, CastType
from jellytoast.cast_manager import _chromecast as _cc_mod
from jellytoast.cast_receiver_queue import CastReceiverQueue
from jellytoast.player_cast_transport import _CastTransportMixin
from jellytoast.player_state import NowPlaying, get_now_playing, set_now_playing


def _np(item_id, *, container="flac", radio=False, url=None):
    raw = {"Container": container}
    if radio:
        raw = {"streamUrl": "http://radio/stream"}
    return NowPlaying(
        item_id=item_id,
        title=f"Track {item_id}",
        stream_url=url or f"http://srv/{item_id}",
        thumb_url="",
        item_type="Audio",
        raw=raw,
    )


@pytest.fixture(autouse=True)
def now_playing():
    prev = get_now_playing()
    set_now_playing(_np("cur"))
    yield
    set_now_playing(prev)


# ── The wire: CastManager.chromecast_queue_next ─────────────────────────────


class _WireMC:
    """Media controller that answers queue requests the way a receiver
    would (or refuses / stays silent, per ``answer``)."""

    def __init__(self, session=7, answer="ok", items=None):
        self.status = SimpleNamespace(media_session_id=session, content_id="http://srv/cur")
        self.answer = answer
        self.items = items
        self.sent = []
        self.inserts = []

    def _reply(self, cb, tag=None, url=None):
        if self.answer == "silent":
            return
        if self.answer == "refuse":
            cb(True, {"type": "INVALID_REQUEST", "reason": "INVALID_MEDIA_SESSION_ID"})
            return
        items = self.items
        if items is None:
            items = [
                {"itemId": 1, "media": {"contentId": "http://srv/cur"}},
                {"itemId": 2, "media": {"contentId": url, "customData": {"jellytoast": tag}}},
            ]
        cb(True, {"type": "MEDIA_STATUS", "status": [{"items": items}]})

    def send_message(self, msg, inc_session_id=False, callback_function=None):
        self.sent.append(msg)
        if msg["type"] == "QUEUE_REMOVE" and self.answer == "refuse_remove":
            callback_function(True, {"type": "INVALID_REQUEST"})
            return
        if msg["type"] == "QUEUE_REMOVE" and self.answer != "silent":
            callback_function(True, {"type": "MEDIA_STATUS", "status": [{}]})

    def play_media(self, url, content_type, **kw):
        self.inserts.append((url, content_type, kw))
        tag = kw["media_info"]["customData"]["jellytoast"]
        self._reply(kw["callback_function"], tag, url)


def _wire_dev(mc):
    return CastDevice(
        name="Group", host="10.0.0.2", port=8009, device_type=CastType.CHROMECAST,
        uuid="g", cast_object=SimpleNamespace(media_controller=mc),
    )


@pytest.fixture
def proxied(monkeypatch):
    import jellytoast.cast_proxy as _proxy

    monkeypatch.setattr(_proxy, "resolve_cast_url", lambda u: f"http://relay/{u.rsplit('/', 1)[-1]}")


class _Prov:
    def get_audio_transcode_url(self, item_id, max_bitrate_kbps=320, codec="mp3"):
        return f"http://srv/{item_id}.mp3?b={max_bitrate_kbps}"


class TestQueueNextWire:
    def test_inserts_behind_the_current_track(self, proxied):
        mc = _WireMC()
        out = CastManager().chromecast_queue_next(_wire_dev(mc), _np("n1"), provider=_Prov())
        (url, mime, kw), = mc.inserts
        assert url == "http://relay/n1"  # through the cast proxy
        assert mime == "audio/flac"  # direct play, like the LOAD
        assert kw["enqueue"] is True and kw["stream_type"] == "BUFFERED"
        q = out["queued"]
        assert q["item_id"] == "n1" and q["receiver_id"] == 2
        assert q["tag"] == kw["media_info"]["customData"]["jellytoast"]
        assert q["playing_url"] == "http://srv/cur"
        assert out["removed"] is True

    def test_untranscodable_container_queues_the_transcode(self, proxied):
        mc = _WireMC()
        CastManager().chromecast_queue_next(
            _wire_dev(mc), _np("n1", container="alac"), provider=_Prov()
        )
        url, mime, _ = mc.inserts[0]
        assert url == "http://relay/n1.mp3?b=320" and mime == "audio/mpeg"

    def test_removes_the_old_item_first(self, proxied):
        mc = _WireMC()
        out = CastManager().chromecast_queue_next(
            _wire_dev(mc), _np("n2"), provider=_Prov(), remove_item_id=5
        )
        assert mc.sent[0] == {"type": "QUEUE_REMOVE", "mediaSessionId": 7, "itemIds": [5]}
        assert out["removed"] is True and out["queued"]["item_id"] == "n2"

    def test_failed_removal_queues_nothing(self, proxied):
        mc = _WireMC(answer="refuse_remove")
        out = CastManager().chromecast_queue_next(
            _wire_dev(mc), _np("n2"), provider=_Prov(), remove_item_id=5
        )
        assert out == {"removed": False, "queued": None}
        assert mc.inserts == []

    def test_refused_insert(self, proxied):
        out = CastManager().chromecast_queue_next(
            _wire_dev(_WireMC(answer="refuse")), _np("n1"), provider=_Prov()
        )
        assert out["queued"] is None

    def test_no_reply_keeps_the_item_without_an_id(self, proxied, monkeypatch):
        monkeypatch.setattr(_cc_mod, "_QUEUE_REPLY_TIMEOUT_S", 0.01)
        out = CastManager().chromecast_queue_next(
            _wire_dev(_WireMC(answer="silent")), _np("n1"), provider=_Prov()
        )
        assert out["queued"]["receiver_id"] is None

    def test_id_from_a_listing_without_media(self, proxied):
        mc = _WireMC(items=[{"itemId": 1}, {"itemId": 9}])
        out = CastManager().chromecast_queue_next(_wire_dev(mc), _np("n1"), provider=_Prov())
        assert out["queued"]["receiver_id"] == 9

    def test_nothing_loaded_means_no_queue(self, proxied):
        mc = _WireMC(session=None)
        out = CastManager().chromecast_queue_next(
            _wire_dev(mc), _np("n1"), provider=_Prov(), remove_item_id=5
        )
        assert out == {"removed": True, "queued": None}
        assert mc.sent == [] and mc.inserts == []

    def test_async_wrapper_reports_on_done(self, proxied, monkeypatch):
        def _run(fn, on_result=None, on_error=None):
            on_result(fn())

        monkeypatch.setattr(_cm_pkg, "run_async", _run)
        got = []
        CastManager().chromecast_queue_next_async(
            _wire_dev(_WireMC()), _np("n1"), provider=_Prov(), on_done=got.append
        )
        assert got[0]["queued"]["item_id"] == "n1"


def test_load_tags_the_item(monkeypatch):
    """A LOAD carries a fresh tag too, so pychromecast (which keeps the last
    customData it saw) can't show an old queued item's tag for it."""
    import jellytoast.cast_proxy as _proxy

    monkeypatch.setattr(_proxy, "resolve_cast_url", lambda u: u)
    mc = MagicMock()
    mc.status = SimpleNamespace(player_state="PLAYING", idle_reason=None)
    cc = SimpleNamespace(media_controller=mc, wait=lambda timeout=None: None, app_id="x")
    dev = CastDevice(
        name="S", host="h", port=1, device_type=CastType.CHROMECAST, uuid="u", cast_object=cc
    )
    cm = CastManager()
    monkeypatch.setattr(cm, "_arm_active_cast", lambda *a, **k: None)
    assert cm.cast_to_chromecast(dev, "http://srv/a", is_audio=True)
    kw = mc.play_media.call_args.kwargs
    assert kw["media_info"]["customData"]["jellytoast"].startswith("jt-")


# ── The logic: CastReceiverQueue ────────────────────────────────────────────


class _FakeCM:
    def __init__(self, device_type=CastType.CHROMECAST):
        self.active_cast = SimpleNamespace(device_type=device_type)
        self.requests = []
        self.sync_cast_paused = MagicMock()

    def chromecast_queue_next_async(self, dev, np, *, provider, remove_item_id=None, on_done):
        self.requests.append(SimpleNamespace(np=np, remove=remove_item_id, on_done=on_done))

    def answer(self, *, queued_tag=None, receiver_id=None, removed=True, url=None):
        req = self.requests[-1]
        queued = None
        if queued_tag is not None:
            queued = {
                "tag": queued_tag,
                "item_id": req.np.item_id,
                "url": url or f"http://relay/{req.np.item_id}",
                "receiver_id": receiver_id,
                "playing_url": "http://relay/cur",
            }
        req.on_done({"removed": removed, "queued": queued})


def _host(cm=None, now=1000.0):
    clock = {"t": now}
    h = SimpleNamespace(
        _cast_manager=cm or _FakeCM(),
        api=object(),
        _sleep_pending_eot=False,
        _cast_last_player_state="PLAYING",
        _monotonic=lambda: clock["t"],
        clock=clock,
    )
    h._on_ended = MagicMock()
    return h


def _status(state="PLAYING", tag=None, content_id=None, idle_reason=None):
    return SimpleNamespace(
        player_state=state,
        idle_reason=idle_reason,
        content_id=content_id,
        media_custom_data={"jellytoast": tag} if tag else {},
    )


class TestSync:
    def test_queues_the_next_track(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        (req,) = h._cast_manager.requests
        assert req.np.item_id == "n1" and req.remove is None
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        assert rq.queued["item_id"] == "n1"

    def test_same_next_track_sends_nothing(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        rq.set_wanted(_np("n1"))
        assert len(h._cast_manager.requests) == 1

    def test_queue_edit_swaps_the_queued_track(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        rq.set_wanted(_np("n2"))  # reorder / shuffle / repeat / add next
        req = h._cast_manager.requests[-1]
        assert req.remove == 2 and req.np.item_id == "n2"
        h._cast_manager.answer(queued_tag="t2", receiver_id=3)
        assert rq.queued["item_id"] == "n2"

    def test_end_of_queue_takes_it_off(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        rq.set_wanted(None)
        req = h._cast_manager.requests[-1]
        assert req.remove == 2 and req.np is None
        h._cast_manager.answer()
        assert rq.queued is None

    def test_one_request_at_a_time_then_catches_up(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        rq.set_wanted(_np("n2"))  # while the first is in flight
        assert len(h._cast_manager.requests) == 1
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        req = h._cast_manager.requests[-1]
        assert req.remove == 2 and req.np.item_id == "n2"

    @pytest.mark.parametrize(
        "case",
        ["radio_next", "radio_now", "sleep_eot", "loading", "not_chromecast", "video"],
    )
    def test_cases_left_to_the_push_path(self, case):
        cm = _FakeCM(CastType.DLNA if case == "not_chromecast" else CastType.CHROMECAST)
        h = _host(cm)
        rq = CastReceiverQueue(h)
        want = _np("n1", radio=case == "radio_next")
        if case == "radio_now":
            set_now_playing(_np("cur", radio=True))
        if case == "sleep_eot":
            h._sleep_pending_eot = True
        if case == "loading":
            rq.begin_load()
        if case == "video":
            want.item_type = "Movie"
        rq.set_wanted(want)
        assert cm.requests == []

    def test_load_replaces_the_queue_then_requeues(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        rq.begin_load()  # user pressed next: LOAD in flight
        assert rq.queued is None
        rq.set_wanted(_np("n2"))
        assert len(h._cast_manager.requests) == 1
        rq.load_done(True)
        req = h._cast_manager.requests[-1]
        assert req.remove is None and req.np.item_id == "n2"

    def test_reply_from_before_a_load_is_dropped(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        rq.begin_load()
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        assert rq.queued is None

    def test_refused_track_is_not_retried(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer()  # refused
        rq.set_wanted(_np("n1"))
        assert len(h._cast_manager.requests) == 1
        rq.set_wanted(_np("n2"))
        assert len(h._cast_manager.requests) == 2

    def test_unnamed_item_goes_stale_instead_of_stacking(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=None)  # no reply id
        rq.set_wanted(_np("n2"))
        assert len(h._cast_manager.requests) == 1  # nothing queued behind it
        assert rq.stale_tags == {"t1"} and rq.queued is None

    def test_failed_removal_goes_stale(self):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        rq.set_wanted(_np("n2"))
        h._cast_manager.answer(removed=False)
        assert rq.stale_tags == {"t1"} and rq.queued is None
        assert len(h._cast_manager.requests) == 2


class TestFollow:
    def _queued(self, receiver_id=2, url=None):
        h = _host()
        rq = CastReceiverQueue(h)
        rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=receiver_id, url=url)
        return h, rq

    def test_receiver_moving_on_advances_the_app(self):
        h, rq = self._queued()
        seen = {}
        h._on_ended.side_effect = lambda: seen.update(handoff=rq.take_followed(_np("n1")))
        assert rq.on_status(_status("PLAYING", tag="t1")) is False
        h._on_ended.assert_called_once()
        assert seen["handoff"] is True  # play() of n1 inside _on_ended: no push
        assert rq.queued is None

    def test_current_track_status_is_not_a_move(self):
        h, rq = self._queued()
        assert rq.on_status(_status("PLAYING", tag="t0")) is False
        h._on_ended.assert_not_called()

    def test_followed_flag_does_not_outlive_the_advance(self):
        h, rq = self._queued()
        rq.on_status(_status("PLAYING", tag="t1"))
        assert rq.take_followed(_np("n1")) is False

    def test_by_url_when_the_receiver_drops_custom_data(self):
        h, rq = self._queued()
        rq.on_status(_status("BUFFERING", content_id="http://relay/n1"))
        h._on_ended.assert_called_once()

    def test_queued_repeat_of_the_same_url_is_not_a_move(self):
        h, rq = self._queued(url="http://relay/cur")
        rq.on_status(_status("PLAYING", content_id="http://relay/cur"))
        h._on_ended.assert_not_called()

    def test_finished_holds_then_follows(self):
        h, rq = self._queued()
        assert rq.on_status(_status("IDLE", idle_reason="FINISHED")) is True
        h._on_ended.assert_not_called()
        h.clock["t"] += 1.0
        rq.tick()
        h._on_ended.assert_not_called()
        rq.on_status(_status("PLAYING", tag="t1"))
        h._on_ended.assert_called_once()
        assert not rq.holding

    def test_finished_without_a_move_pushes_the_next_track(self):
        h, rq = self._queued()
        rq.on_status(_status("IDLE", idle_reason="FINISHED"))
        h.clock["t"] += CastReceiverQueue.ADVANCE_WAIT_S + 0.1
        rq.tick()
        h._on_ended.assert_called_once()
        assert h._cast_last_player_state == "IDLE"
        assert rq.take_followed(_np("n1")) is False  # play() pushes it

    def test_receiver_rejecting_the_queued_track_pushes_it(self):
        h, rq = self._queued()
        assert rq.on_status(_status("IDLE", tag="t1", idle_reason="ERROR")) is True
        h._on_ended.assert_called_once()
        assert rq.take_followed(_np("n1")) is False

    def test_stale_track_starting_pushes_the_real_next(self):
        h, rq = self._queued(receiver_id=None)
        rq.set_wanted(_np("n2"))  # can't take n1 back off
        rq.on_status(_status("PLAYING", tag="t1"))
        h._on_ended.assert_called_once()
        assert rq.take_followed(_np("n2")) is False
        assert rq.stale_tags == set()

    def test_stale_tag_ignored_while_a_load_is_in_flight(self):
        h, rq = self._queued(receiver_id=None)
        rq.set_wanted(_np("n2"))
        rq.loading = True
        rq.on_status(_status("PLAYING", tag="t1"))
        h._on_ended.assert_not_called()


# ── Wired into the transport mixin ──────────────────────────────────────────


class TestTransportWiring:
    def _stub(self):
        h = _host()
        h._cast_last_duration_ms = 180_000
        h._cast_last_position_ms = 179_000
        h._cast_anchor_pos_ms = 179_000
        h._cast_anchor_wall = 999.0
        h._cast_end_poke_wall = 0.0
        h._on_position = MagicMock()
        h._on_duration = MagicMock()
        h._on_paused = MagicMock()
        for k in ("_CAST_END_GRACE_S", "_CAST_END_POKE_S", "_CAST_END_GIVE_UP_S"):
            setattr(h, k, getattr(_CastTransportMixin, k))
        h._cast_rq = CastReceiverQueue(h)
        h._cast_rq.set_wanted(_np("n1"))
        h._cast_manager.answer(queued_tag="t1", receiver_id=2)
        return h

    def test_finished_with_a_queued_track_does_not_advance_twice(self):
        h = self._stub()
        _CastTransportMixin._apply_cast_status(h, _status("IDLE", idle_reason="FINISHED"))
        h._on_ended.assert_not_called()
        status = _status("PLAYING", tag="t1")
        status.current_time, status.duration = 0.5, 200.0
        _CastTransportMixin._apply_cast_status(h, status)
        h._on_ended.assert_called_once()

    def test_new_track_status_applies_after_the_move(self):
        h = self._stub()

        def _ended():  # what play()'s handoff does to the poll state
            h._cast_last_player_state = None
            h._cast_last_duration_ms = -1
            h._cast_last_position_ms = -1

        h._on_ended.side_effect = _ended
        status = _status("PLAYING", tag="t1")
        status.current_time, status.duration = 0.5, 200.0
        _CastTransportMixin._apply_cast_status(h, status)
        h._on_duration.assert_called_once_with(200_000)
        h._on_position.assert_called_once_with(500)

    def test_watchdog_waits_while_holding(self):
        h = self._stub()
        h._cast_rq.end_wait_wall = 990.0
        h._cast_anchor_wall = 900.0  # far past the end
        cc = SimpleNamespace(media_controller=MagicMock())
        _CastTransportMixin._check_cast_track_overdue(h, cc, _status("IDLE"))
        h._on_ended.assert_not_called()


# ── MpvController.play(): handoff vs push ───────────────────────────────────


def _ctrl(monkeypatch, cm):
    from jellytoast import player_backend
    from jellytoast.player_backend import MpvController

    monkeypatch.setattr(player_backend.QTimer, "singleShot", lambda _ms, fn: fn())
    ctrl = MpvController.__new__(MpvController)
    ctrl._cast_manager = cm
    ctrl._cast_active = lambda: True
    ctrl.api = object()
    ctrl._monotonic = lambda: 0.0
    ctrl._cast_attempt = 0
    ctrl._sleep_pending_eot = False
    ctrl._mpv = None
    ctrl._cast_rq = CastReceiverQueue(ctrl)
    ctrl.started = []
    ctrl.bus = SimpleNamespace(
        cast_stopped=SimpleNamespace(emit=lambda: None),
        playback_started=SimpleNamespace(emit=ctrl.started.append),
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


class TestPlay:
    def _cm(self, ok=True):
        cm = _FakeCM()
        cm.active_cast = CastDevice(
            name="G", host="h", port=1, device_type=CastType.CHROMECAST, uuid="g"
        )
        cm.loads = []

        def start_track(dev, np, **kw):
            cm.loads.append(np)
            cm.pending = kw["on_done"]

        cm.start_track = start_track
        return cm

    def test_track_the_receiver_moved_to_is_not_pushed(self, monkeypatch):
        cm = self._cm()
        ctrl = _ctrl(monkeypatch, cm)
        ctrl._cast_rq.followed_item_id = "n1"
        ctrl.play(_np("n1"))
        assert cm.loads == []
        assert [n.item_id for n in ctrl.started] == ["n1"]
        assert ctrl._cast_last_duration_ms == -1  # poll state reset for the new track

    def test_push_holds_the_queue_until_the_load_lands(self, monkeypatch):
        cm = self._cm()
        ctrl = _ctrl(monkeypatch, cm)
        ctrl.play(_np("cur"))
        assert [n.item_id for n in cm.loads] == ["cur"]
        ctrl._on_prefetch_request(_np("n1"))
        assert cm.requests == []  # LOAD still in flight
        cm.pending(True)
        assert [r.np.item_id for r in cm.requests] == ["n1"]

    def test_failed_load_queues_nothing(self, monkeypatch):
        monkeypatch.setattr("jellytoast.notifications.notify", lambda *a, **k: None)
        cm = self._cm()
        cm.stop_cast = lambda: None
        cm.last_cast_failure = None
        ctrl = _ctrl(monkeypatch, cm)
        ctrl.play(_np("cur"))
        ctrl._on_prefetch_request(_np("n1"))
        cm.pending(False)
        assert cm.requests == []
