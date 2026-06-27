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

import jellytoast.async_io as aio
from jellytoast.cast_manager._manager import CastManager


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()
    # Run the DLNA/Sonos off-thread dispatch inline so routing is observable,
    # forwarding the result to on_result so result-driven flag flips are testable.
    def _run_inline(fn, *a, on_result=None, on_error=None, **k):
        res = fn()
        if on_result is not None:
            on_result(res)
        return None

    monkeypatch.setattr(aio, "run_async", _run_inline)
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
        monkeypatch.setattr(mgr, "_dlna_pause", lambda: (calls.append("pause"), True)[1])
        monkeypatch.setattr(mgr, "_dlna_resume", lambda: (calls.append("resume"), True)[1])
        mgr.active_cast = _dev("dlna")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()  # playing → pause
        mgr.cast_toggle_pause()  # paused → resume
        assert calls == ["pause", "resume"]
        assert mgr._cast_paused is False  # back to playing after two toggles

    def test_toggle_pause_routes_sonos(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "_sonos_pause", lambda: (calls.append("pause"), True)[1])
        monkeypatch.setattr(mgr, "_sonos_resume", lambda: (calls.append("resume"), True)[1])
        mgr.active_cast = _dev("sonos")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()
        assert calls == ["pause"]
        assert mgr._cast_paused is True

    def test_toggle_pause_failure_does_not_flip_flag(self, mgr, monkeypatch):
        # A failed transport (backend returns False) must NOT flip the tracked
        # pause flag — otherwise the next toggle sends the wrong command.
        monkeypatch.setattr(mgr, "_dlna_pause", lambda: False)
        mgr.active_cast = _dev("dlna")
        mgr._cast_paused = False
        mgr.cast_toggle_pause()
        assert mgr._cast_paused is False

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


class TestSeekFollowups:
    """Skip (±) wired for DLNA + Sonos, and DLNA absolute-seek goes to the
    real position (UPnP REL_TIME = position-in-track) instead of a clamped
    delta — so backward seek works."""

    def test_seek_relative_routes_by_type(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "_dlna_seek_relative", lambda d: calls.append(("dlna", d)))
        monkeypatch.setattr(mgr, "_sonos_seek_relative", lambda d: calls.append(("sonos", d)))
        monkeypatch.setattr(mgr, "chromecast_seek", lambda s: calls.append(("cc", s)))

        for kind in ("dlna", "sonos"):
            mgr.active_cast = _dev(kind)
            mgr.cast_seek_relative(10.0)
        # Chromecast reads the receiver position then absolute-seeks pos+delta.
        cc = SimpleNamespace(
            media_controller=SimpleNamespace(status=SimpleNamespace(current_time=30.0))
        )
        mgr.active_cast = SimpleNamespace(device_type="chromecast", cast_object=cc)
        mgr.cast_seek_relative(10.0)   # 30 + 10
        mgr.cast_seek_relative(-25.0)  # 30 - 25 = 5
        assert calls == [("dlna", 10.0), ("sonos", 10.0), ("cc", 40.0), ("cc", 5.0)]

    def test_dlna_seek_abs_is_absolute_not_delta(self, mgr, monkeypatch):
        import jellytoast.cast.dlna as _dlna

        seeks = []

        class _C:
            def last_state(self):
                return {"position_sec": 50.0}

            def seek(self, s):
                seeks.append(s)

        monkeypatch.setattr(_dlna, "get_dlna_controller", lambda: _C())
        mgr._dlna_seek_abs(20.0)  # seek to 20s (backward from 50) — absolute
        assert seeks == [20.0]  # NOT 20-50 clamped to 0

    def test_dlna_seek_relative_adds_to_polled_position(self, mgr, monkeypatch):
        import jellytoast.cast.dlna as _dlna

        seeks = []

        class _C:
            def last_state(self):
                return {"position_sec": 50.0}

            def seek(self, s):
                seeks.append(s)

        monkeypatch.setattr(_dlna, "get_dlna_controller", lambda: _C())
        mgr._dlna_seek_relative(-10.0)  # 50 - 10 = 40 (backward skip works)
        assert seeks == [40.0]


class TestInitialVolume:
    """``cast_set_initial_volume`` pushes a defined level on connect for
    EVERY protocol — DLNA/Sonos previously got nothing because
    ``_on_cast_started`` called the chromecast-only setter, which
    early-returns off-Chromecast (the LG-TV "plays at the renderer's last
    volume" bug). Routes by ``device_type`` like ``cast_set_volume`` and
    returns the value actually applied so the slider can track it.
    """

    def _patch_setters(self, mgr, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: calls.append(("cc", v)))
        monkeypatch.setattr(mgr, "_dlna_set_volume", lambda v: calls.append(("dlna", v)))
        monkeypatch.setattr(mgr, "_sonos_set_volume", lambda v: calls.append(("sonos", v)))
        return calls

    def test_routes_by_type_and_returns_applied(self, mgr, monkeypatch):
        calls = self._patch_setters(mgr, monkeypatch)
        # Floor below the initial value → no Sonos clamp.
        monkeypatch.setattr(mgr, "_sonos_initial_volume", lambda p: p)
        for kind in ("chromecast", "dlna", "sonos"):
            mgr.active_cast = _dev(kind)
            assert mgr.cast_set_initial_volume(30) == 30
        assert calls == [("cc", 30), ("dlna", 30), ("sonos", 30)]

    def test_sonos_clamps_up_to_floor(self, mgr, monkeypatch):
        # A floor above the initial value bumps the push up and the
        # returned (slider-bound) value with it.
        import jellytoast.settings as _settings

        calls = self._patch_setters(mgr, monkeypatch)
        monkeypatch.setattr(_settings, "get_settings", lambda: SimpleNamespace(sonos_volume_floor=45))
        mgr.active_cast = _dev("sonos")
        assert mgr.cast_set_initial_volume(30) == 45
        assert calls == [("sonos", 45)]

    def test_sonos_floor_below_initial_keeps_initial(self, mgr, monkeypatch):
        import jellytoast.settings as _settings

        calls = self._patch_setters(mgr, monkeypatch)
        monkeypatch.setattr(_settings, "get_settings", lambda: SimpleNamespace(sonos_volume_floor=15))
        mgr.active_cast = _dev("sonos")
        assert mgr.cast_set_initial_volume(30) == 30
        assert calls == [("sonos", 30)]

    def test_sonos_initial_volume_falls_back_on_settings_error(self, mgr, monkeypatch):
        import jellytoast.settings as _settings

        def _boom():
            raise RuntimeError("no settings")

        monkeypatch.setattr(_settings, "get_settings", _boom)
        # Bad settings → just use the requested percent, never crash.
        assert mgr._sonos_initial_volume(30) == 30

    def test_no_active_cast_returns_percent_unchanged(self, mgr, monkeypatch):
        calls = self._patch_setters(mgr, monkeypatch)
        mgr.active_cast = None
        assert mgr.cast_set_initial_volume(30) == 30
        assert calls == []

    def test_airplay_has_no_volume_push(self, mgr, monkeypatch):
        calls = self._patch_setters(mgr, monkeypatch)
        mgr.active_cast = _dev("airplay")
        assert mgr.cast_set_initial_volume(30) == 30
        assert calls == []
