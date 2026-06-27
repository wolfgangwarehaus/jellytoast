"""AT-18: ``CastType`` / ``DownloadState`` enums + the unified cast
track-start dispatch.

This is a behaviour-preserving refactor, so the load-bearing guarantee is
*value identity*: both enums subclass ``str`` and their members must be
byte-identical to the lowercase strings that were used inline before the
enums existed — those values hit persisted QSettings (cast device favourites,
section keys), SQLite (``nodes.state``), and the ``download_progress`` signal
payload. If a value drifts, old persisted state silently mis-reads.

The dispatch half pins that ``CastManager.start_track`` routes each
``CastType`` to the same transport call the two former inline ladders used,
and that both dispatch call sites delegate to it.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import jellytoast.async_io as aio
from jellytoast.cast_manager import CastType
from jellytoast.cast_manager._manager import CastManager
from jellytoast.offline.index import DownloadState
from jellytoast.player_state import NowPlaying

# ── CastType value identity ──────────────────────────────────────────


def test_casttype_values_byte_identical():
    # The exact lowercase strings every pre-enum site compared against.
    assert CastType.CHROMECAST == "chromecast"
    assert CastType.AIRPLAY == "airplay"
    assert CastType.DLNA == "dlna"
    assert CastType.SONOS == "sonos"
    # str-backed: a member *is* its string value.
    assert isinstance(CastType.CHROMECAST, str)
    assert {m.value for m in CastType} == {
        "chromecast",
        "airplay",
        "dlna",
        "sonos",
    }


def test_casttype_dict_lookup_by_bare_string():
    # The cast-dialog section labels are a dict keyed by the bare lowercase
    # string; a CastType member must hash/equal-resolve the same so a lookup
    # keyed by the enum hits the string entry (and vice-versa).
    labels = {"chromecast": "Chromecast", "airplay": "AirPlay"}
    assert labels[CastType.CHROMECAST] == "Chromecast"
    assert labels.get(CastType.AIRPLAY) == "AirPlay"


def test_casttype_json_roundtrip_is_plain_string():
    # Favourite-cast-device persistence serialises {type: ...} as JSON; an
    # enum member must serialise to the bare value, not "CastType.X".
    assert json.dumps({"type": CastType.SONOS}) == '{"type": "sonos"}'
    assert json.loads('{"type": "sonos"}')["type"] == CastType.SONOS


def test_casttype_favorite_settings_roundtrip(isolated_settings):
    # Storing a CastType through the favourite-cast-devices setter must
    # persist the bare lowercase value (str(CastType.X) is "CastType.X" on
    # 3.11+, which would corrupt it — the setter guards against that).
    s = isolated_settings
    s.favorite_cast_devices = [
        {"uuid": "u1", "name": "Living Room", "type": CastType.SONOS},
    ]
    raw = s._s.value("playback/favorite_cast_devices", "", type=str)
    assert json.loads(raw)[0]["type"] == "sonos"
    # And it reads back as a plain string equal to the enum.
    back = s.favorite_cast_devices[0]
    assert back["type"] == "sonos"
    assert back["type"] == CastType.SONOS


# ── DownloadState value identity ─────────────────────────────────────


def test_downloadstate_values_byte_identical():
    assert DownloadState.PENDING == "pending"
    assert DownloadState.DOWNLOADING == "downloading"
    assert DownloadState.COMPLETE == "complete"
    assert DownloadState.FAILED == "failed"
    assert DownloadState.STALE == "stale"
    assert DownloadState.REMOVED == "removed"
    assert isinstance(DownloadState.COMPLETE, str)


def test_downloadstate_sqlite_stores_plain_text():
    # nodes.state is TEXT; a str-backed enum must bind as the bare value so
    # an existing DB row written as "complete" still equates.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(s TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (DownloadState.COMPLETE,))
    row = con.execute("SELECT s, typeof(s) FROM t").fetchone()
    assert row[0] == "complete"
    assert row[1] == "text"
    assert row[0] == DownloadState.COMPLETE


def test_downloadstate_signal_payload_is_plain_string(qapp):
    # The download_progress Signal(str, str, float) marshals a DownloadState
    # member to a plain str on the receiver side; consumers' == "complete"
    # comparisons keep working and never see "DownloadState.X".
    from jellytoast.player_state import PlayerBus

    received = []
    bus = PlayerBus.get()
    bus.download_progress.connect(
        lambda i, st, fr: received.append((i, st, type(st).__name__))
    )
    try:
        bus.download_progress.emit("item-1", DownloadState.COMPLETE, 1.0)
    finally:
        bus.download_progress.disconnect()
    assert received == [("item-1", "complete", "str")]


# ── Unified track-start dispatch ─────────────────────────────────────


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()

    # Run the DLNA/Sonos off-thread dispatch inline so routing is observable,
    # faithfully marshalling the result/exception to on_result/on_error the
    # way the real run_async does (start_track's _run_off_thread_result relies
    # on on_result firing to deliver the bool back to on_done).
    def _inline(fn, *_a, on_result=None, on_error=None, **_k):
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if on_error is not None:
                on_error(e)
            return
        if on_result is not None:
            on_result(res)

    monkeypatch.setattr(aio, "run_async", _inline)
    return m


def _np(**kw):
    base = dict(
        item_id="abc",
        title="Song",
        subtitle="Artist",
        album="Album",
        duration=215000,
        thumb_url="http://s/art.jpg",
        stream_url="http://s/stream",
        item_type="Audio",
    )
    base.update(kw)
    return NowPlaying(**base)


class _Prov:
    def get_audio_transcode_url(self, item_id, max_bitrate_kbps, codec):
        return f"http://s/{item_id}?br={max_bitrate_kbps}&c={codec}"


def _dev(kind):
    return SimpleNamespace(name="Dev", device_type=kind, cast_object=object())


def test_start_track_routes_chromecast(mgr, monkeypatch):
    calls = {}

    def _cc(dev, url, title="", thumb="", **kw):
        calls.update(dev=dev, url=url, title=title, kw=kw)
        if kw.get("on_done"):
            kw["on_done"](True)

    monkeypatch.setattr(mgr, "cast_to_chromecast_async", _cc)
    # FLAC direct-plays → no transcode, native MIME picked.
    monkeypatch.setattr(mgr, "chromecast_audio_mime_for", lambda c: "audio/flac")
    dev = _dev(CastType.CHROMECAST)
    done = []
    mgr.start_track(
        dev, _np(raw={"Container": "flac"}), provider=_Prov(),
        resume_seconds=12.0, on_done=lambda ok: done.append(ok),
    )
    assert calls["dev"] is dev
    assert calls["url"] == "http://s/stream"  # native, not transcoded
    assert calls["kw"]["content_type"] == "audio/flac"
    assert calls["kw"]["current_time"] == 12.0  # resume offset threaded through
    assert done == [True]


def test_start_track_chromecast_transcode_fallback(mgr, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        mgr, "cast_to_chromecast_async",
        lambda dev, url, title="", thumb="", **kw: calls.update(url=url, kw=kw),
    )
    # Unknown codec → None → 320 kbps MP3 transcode via the provider.
    monkeypatch.setattr(mgr, "chromecast_audio_mime_for", lambda c: None)
    mgr.start_track(
        _dev(CastType.CHROMECAST), _np(raw={"Container": "alac"}), provider=_Prov(),
    )
    assert calls["url"] == "http://s/abc?br=320&c=mp3"
    assert calls["kw"]["content_type"] == "audio/mpeg"


def test_start_track_radio_bypasses_resume_and_transcode(mgr, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        mgr, "cast_to_chromecast_async",
        lambda dev, url, title="", thumb="", **kw: calls.update(url=url, kw=kw),
    )
    # Internet radio: live stream, unseekable → current_time 0.0 regardless
    # of resume_seconds, and a generic audio MIME (no transcode 404).
    mgr.start_track(
        _dev(CastType.CHROMECAST),
        _np(raw={"streamUrl": "http://radio/live"}),
        provider=_Prov(), resume_seconds=99.0,
    )
    assert calls["url"] == "http://s/stream"
    assert calls["kw"]["content_type"] == "audio/mpeg"
    assert calls["kw"]["current_time"] == 0.0
    assert calls["kw"]["is_live"] is True


def test_start_track_routes_dlna(mgr, monkeypatch):
    calls = {}

    def _dlna(dev, url, meta, *, transcode_url_fn=None, start_sec=0.0, **kw):
        calls.update(dev=dev, url=url, start_sec=start_sec, tfn=transcode_url_fn)
        return True

    monkeypatch.setattr(mgr, "cast_to_dlna", _dlna)
    monkeypatch.setattr("jellytoast.cast_payload.dlna_meta_from_np", lambda np: "META")
    done = []
    mgr.start_track(
        _dev(CastType.DLNA), _np(raw={"Container": "flac"}), provider=_Prov(),
        resume_seconds=7.5, on_done=lambda ok: done.append(ok),
    )
    assert calls["url"] == "http://s/stream"
    assert calls["start_sec"] == 7.5
    assert callable(calls["tfn"])  # provider transcode fallback wired
    assert done == [True]


def test_start_track_routes_sonos(mgr, monkeypatch):
    calls = {}

    def _sonos(dev, url, *, title="", artist="", album="", art_url="", is_live=False):
        calls.update(dev=dev, url=url, title=title, artist=artist, album=album, is_live=is_live)
        return True

    monkeypatch.setattr(mgr, "cast_to_sonos", _sonos)
    done = []
    mgr.start_track(
        _dev(CastType.SONOS), _np(), provider=_Prov(),
        on_done=lambda ok: done.append(ok),
    )
    assert calls["url"] == "http://s/stream"
    assert calls["title"] == "Song"
    assert calls["artist"] == "Artist"
    assert done == [True]


def test_start_track_routes_airplay_synchronously(mgr, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        mgr, "cast_to_airplay",
        lambda dev, url, title="": calls.update(url=url, title=title) or True,
    )
    done = []
    mgr.start_track(
        _dev(CastType.AIRPLAY), _np(), provider=_Prov(),
        on_done=lambda ok: done.append(ok),
    )
    assert calls["url"] == "http://s/stream"
    assert done == [True]  # AirPlay fires on_done inline (no async hop)




def test_start_track_dlna_failure_reports_false(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "cast_to_dlna", lambda *a, **k: False)
    monkeypatch.setattr("jellytoast.cast_payload.dlna_meta_from_np", lambda np: "META")
    done = []
    mgr.start_track(
        _dev(CastType.DLNA), _np(), provider=_Prov(),
        on_done=lambda ok: done.append(ok),
    )
    assert done == [False]


def test_start_track_dlna_exception_degrades_to_false(mgr, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("SOAP timeout")

    monkeypatch.setattr(mgr, "cast_to_dlna", _boom)
    monkeypatch.setattr("jellytoast.cast_payload.dlna_meta_from_np", lambda np: "META")
    done = []
    mgr.start_track(
        _dev(CastType.DLNA), _np(), provider=_Prov(),
        on_done=lambda ok: done.append(ok),
    )
    assert done == [False]  # backend exception → on_done(False), not a crash


# ── Both call sites delegate to start_track ──────────────────────────


def test_player_backend_play_delegates_to_start_track(qapp, monkeypatch):
    # The auto-advance cast branch must call CastManager.start_track rather
    # than re-implementing the per-type ladder inline.
    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    captured = {}

    cm = SimpleNamespace(
        active_cast=_dev(CastType.CHROMECAST),
        start_track=lambda dev, np, **kw: captured.update(dev=dev, np=np, kw=kw),
    )
    ctrl._cast_manager = cm
    ctrl._cast_active = lambda: True
    ctrl.api = _Prov()
    ctrl._monotonic = lambda: 0.0
    ctrl._cast_attempt = 0
    # play() touches these poll-anchor attrs before dispatch.
    for attr in (
        "_cast_last_player_state", "_cast_last_duration_ms",
        "_cast_last_position_ms", "_cast_anchor_pos_ms", "_cast_anchor_wall",
        "_last_streaming_emit_t", "_last_radio_title",
    ):
        setattr(ctrl, attr, None)

    ctrl.play(_np())

    assert captured["dev"] is cm.active_cast
    assert captured["kw"]["provider"] is ctrl.api
    assert callable(captured["kw"]["on_done"])


def test_cast_auto_advance_failure_recovers_to_local(qapp, monkeypatch, caplog):
    # A failed auto-advance cast push (on_done(False)) must not silently
    # stall in a casting-but-dead state. It logs, tears the cast session
    # down (stop_cast + cast_stopped → _on_cast_stopped hands playback back
    # to local mpv), and surfaces a toast.
    import logging

    from jellytoast.player_backend import MpvController

    events = {"stop_cast": 0, "cast_stopped": 0, "notify": 0}
    monkeypatch.setattr(
        "jellytoast.notifications.notify",
        lambda *a, **k: events.__setitem__("notify", events["notify"] + 1),
    )

    ctrl = MpvController.__new__(MpvController)
    captured = {}
    cm = SimpleNamespace(
        active_cast=_dev(CastType.CHROMECAST),
        start_track=lambda dev, np, **kw: captured.update(on_done=kw.get("on_done")),
        stop_cast=lambda: events.__setitem__("stop_cast", events["stop_cast"] + 1),
    )
    ctrl._cast_manager = cm
    ctrl._cast_active = lambda: True
    ctrl.api = _Prov()
    ctrl._monotonic = lambda: 0.0
    ctrl._cast_attempt = 0
    ctrl.bus = SimpleNamespace(
        cast_stopped=SimpleNamespace(
            emit=lambda: events.__setitem__("cast_stopped", events["cast_stopped"] + 1)
        )
    )
    for attr in (
        "_cast_last_player_state", "_cast_last_duration_ms",
        "_cast_last_position_ms", "_cast_anchor_pos_ms", "_cast_anchor_wall",
        "_last_streaming_emit_t", "_last_radio_title",
    ):
        setattr(ctrl, attr, None)

    ctrl.play(_np(title="Doomed Track"))
    on_done = captured["on_done"]
    assert callable(on_done)

    with caplog.at_level(logging.WARNING, logger="jellytoast.player_backend"):
        on_done(False)  # receiver rejected the media / device dropped off

    assert any(
        "cast auto-advance failed" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), "a failed auto-advance cast push was not logged"
    assert "Doomed Track" in caplog.text
    # Recovery: session torn down + handed back to local + user told.
    assert events == {"stop_cast": 1, "cast_stopped": 1, "notify": 1}


def test_cast_auto_advance_success_does_not_warn(qapp, monkeypatch, caplog):
    # The happy path (on_done(True)) must NOT emit the failure warning.
    import logging

    from jellytoast.player_backend import MpvController

    ctrl = MpvController.__new__(MpvController)
    captured = {}
    cm = SimpleNamespace(
        active_cast=_dev(CastType.CHROMECAST),
        start_track=lambda dev, np, **kw: captured.update(on_done=kw.get("on_done")),
    )
    ctrl._cast_manager = cm
    ctrl._cast_active = lambda: True
    ctrl.api = _Prov()
    ctrl._monotonic = lambda: 0.0
    ctrl._cast_attempt = 0
    ctrl.bus = SimpleNamespace(playback_started=SimpleNamespace(emit=lambda _np: None))
    ctrl._begin_play_session = lambda _np: None
    ctrl._report_session_start = lambda _np: None
    for attr in (
        "_cast_last_player_state", "_cast_last_duration_ms",
        "_cast_last_position_ms", "_cast_anchor_pos_ms", "_cast_anchor_wall",
        "_last_streaming_emit_t", "_last_radio_title",
    ):
        setattr(ctrl, attr, None)

    ctrl.play(_np())
    with caplog.at_level(logging.WARNING, logger="jellytoast.player_backend"):
        captured["on_done"](True)

    assert not any("cast auto-advance failed" in r.getMessage() for r in caplog.records)
