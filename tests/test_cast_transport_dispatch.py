"""Mid-cast transport dispatch (audit #14).

The player's play/pause, volume, and seek used to reach chromecast-only
methods that early-return off-Chromecast, so they silently no-op'd on a DLNA
or Sonos cast. The cast_* dispatchers route by ``active_cast.device_type``
(mirroring ``stop_cast``), running DLNA/Sonos off the GUI thread. These pin
the ROUTING; the DLNA/Sonos device behaviour itself is hardware-gated (Sonos
is unverified).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import modules.async_io as aio
from modules.cast_manager._manager import CastManager


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()
    # Run the DLNA/Sonos off-thread dispatch inline so routing is observable.
    monkeypatch.setattr(aio, "run_async", lambda fn, *a, **k: fn())
    return m


def _dev(kind):
    return SimpleNamespace(device_type=kind, cast_object=object())


class TestTransportDispatch:
    def test_toggle_pause_routes_chromecast(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "chromecast_pause", lambda: calls.append("cc"))
        mgr.active_cast = _dev("chromecast")
        mgr.cast_toggle_pause()
        assert calls == ["cc"]

    def test_toggle_pause_dlna_flips_pause_resume(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "_dlna_pause", lambda: calls.append("pause"))
        monkeypatch.setattr(mgr, "_dlna_resume", lambda: calls.append("resume"))
        mgr.active_cast = _dev("dlna")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()  # playing → pause
        mgr.cast_toggle_pause()  # paused → resume
        assert calls == ["pause", "resume"]
        assert mgr._cast_paused is False  # back to playing after two toggles

    def test_toggle_pause_routes_sonos(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "_sonos_pause", lambda: calls.append("pause"))
        monkeypatch.setattr(mgr, "_sonos_resume", lambda: calls.append("resume"))
        mgr.active_cast = _dev("sonos")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()
        assert calls == ["pause"]
        assert mgr._cast_paused is True

    def test_set_volume_routes_by_type(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: calls.append(("cc", v)))
        monkeypatch.setattr(mgr, "_dlna_set_volume", lambda v: calls.append(("dlna", v)))
        monkeypatch.setattr(mgr, "_sonos_set_volume", lambda v: calls.append(("sonos", v)))
        for kind in ("chromecast", "dlna", "sonos"):
            mgr.active_cast = _dev(kind)
            mgr.cast_set_volume(42)
        assert calls == [("cc", 42), ("dlna", 42), ("sonos", 42)]

    def test_seek_routes_by_type(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "chromecast_seek", lambda s: calls.append(("cc", s)))
        monkeypatch.setattr(mgr, "_dlna_seek_abs", lambda s: calls.append(("dlna", s)))
        monkeypatch.setattr(mgr, "_sonos_seek_abs", lambda s: calls.append(("sonos", s)))
        for kind in ("chromecast", "dlna", "sonos"):
            mgr.active_cast = _dev(kind)
            mgr.cast_seek(12.0)
        assert calls == [("cc", 12.0), ("dlna", 12.0), ("sonos", 12.0)]

    def test_airplay_has_no_transport(self, mgr, monkeypatch):
        # AirPlay v1 is push-only — the dispatchers must not crash or flip
        # the pause flag for it.
        mgr.active_cast = _dev("airplay")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()
        mgr.cast_seek(5.0)
        mgr.cast_set_volume(50)
        assert mgr._cast_paused is False

    def test_no_active_cast_is_noop(self, mgr):
        mgr.active_cast = None
        mgr.cast_toggle_pause()
        mgr.cast_seek(5.0)
        mgr.cast_set_volume(50)  # no crash
