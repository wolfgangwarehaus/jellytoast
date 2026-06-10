"""Restore a cast device's pre-cast volume on disconnect (feature 2026-06-02).

On connect we force a defined initial volume (30) so a renderer doesn't
play at a stale level. Until now, disconnecting left the device at that
cast level — annoying when the device doubles as a TV speaker (too loud
/ quiet for TV afterwards). Now we snapshot the device's volume at
connect (before the override) and hand it back in ``stop_cast`` while the
connection is still live; if the level couldn't be read we fall back to a
uniform level. Design preference: previous level when possible.

Routing/logic only — the live Chromecast behaviour is hardware-gated.
Harness mirrors test_cast_transport_dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import jellytoast.async_io as aio
from jellytoast.cast_manager._manager import _CAST_VOLUME_RESTORE_FALLBACK, CastManager


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()
    monkeypatch.setattr(aio, "run_async", lambda fn, *a, **k: fn())
    return m


def _dev(kind, cast_object=None):
    return SimpleNamespace(device_type=kind, cast_object=cast_object or object())


def _cc_dev(volume_level):
    """A chromecast device whose receiver status reports volume_level."""
    return _dev(
        "chromecast",
        cast_object=SimpleNamespace(status=SimpleNamespace(volume_level=volume_level)),
    )


# ── chromecast_get_volume ──────────────────────────────────────────────


class TestChromecastGetVolume:
    def test_reads_volume_level_as_percent(self, mgr):
        mgr.active_cast = _cc_dev(0.5)
        assert mgr.chromecast_get_volume() == 50

    def test_rounds(self, mgr):
        mgr.active_cast = _cc_dev(0.37)
        assert mgr.chromecast_get_volume() == 37

    def test_none_when_volume_level_missing(self, mgr):
        mgr.active_cast = _dev("chromecast", cast_object=SimpleNamespace(status=SimpleNamespace()))
        assert mgr.chromecast_get_volume() is None

    def test_none_off_chromecast(self, mgr):
        mgr.active_cast = _dev("dlna")
        assert mgr.chromecast_get_volume() is None


# ── snapshot on connect ────────────────────────────────────────────────


class TestSnapshotOnConnect:
    def test_initial_volume_snapshots_then_overrides(self, mgr, monkeypatch):
        sets = []
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: sets.append(v))
        mgr.active_cast = _cc_dev(0.80)  # device was at 80
        mgr.cast_set_initial_volume(30)
        assert mgr._pre_cast_device_volume == 80  # captured before override
        assert sets == [30]  # then forced to the cast initial

    def test_snapshot_none_when_unreadable(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: None)
        mgr.active_cast = _dev("chromecast", cast_object=SimpleNamespace(status=SimpleNamespace()))
        mgr.cast_set_initial_volume(30)
        assert mgr._pre_cast_device_volume is None


# ── restore on stop ────────────────────────────────────────────────────


class TestRestoreOnStop:
    def test_restores_snapshot_before_teardown(self, mgr, monkeypatch):
        order = []
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: order.append(("vol", v)))
        monkeypatch.setattr(mgr, "chromecast_stop", lambda: order.append(("stop", None)))
        mgr.active_cast = _cc_dev(0.0)
        mgr._pre_cast_device_volume = 80
        mgr.stop_cast()
        # Volume handed back BEFORE the quit_app teardown.
        assert order == [("vol", 80), ("stop", None)]

    def test_fallback_when_snapshot_unread_on_chromecast(self, mgr, monkeypatch):
        vols = []
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: vols.append(v))
        monkeypatch.setattr(mgr, "chromecast_stop", lambda: None)
        mgr.active_cast = _cc_dev(0.0)
        mgr._pre_cast_device_volume = None  # couldn't read at connect
        mgr.stop_cast()
        assert vols == [_CAST_VOLUME_RESTORE_FALLBACK]

    def test_no_fallback_imposed_on_non_chromecast(self, mgr, monkeypatch):
        """DLNA/Sonos aren't snapshotted yet, so stop must NOT force the
        fallback on them — they keep today's behaviour (no restore)."""
        vols = []
        monkeypatch.setattr(mgr, "_dlna_set_volume", lambda v: vols.append(v))
        monkeypatch.setattr(mgr, "dlna_stop", lambda: None)
        mgr.active_cast = _dev("dlna")
        mgr._pre_cast_device_volume = None
        mgr.stop_cast()
        assert vols == []  # no restore call

    def test_snapshot_cleared_after_restore(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: None)
        monkeypatch.setattr(mgr, "chromecast_stop", lambda: None)
        mgr.active_cast = _cc_dev(0.0)
        mgr._pre_cast_device_volume = 65
        mgr.stop_cast()
        assert mgr._pre_cast_device_volume is None  # consumed


# ── round trip ─────────────────────────────────────────────────────────


def test_connect_then_disconnect_round_trip(mgr, monkeypatch):
    """End-to-end: device at 72 → cast forces 30 → disconnect hands 72
    back before teardown."""
    events = []
    monkeypatch.setattr(mgr, "chromecast_set_volume", lambda v: events.append(("vol", v)))
    monkeypatch.setattr(mgr, "chromecast_stop", lambda: events.append(("stop", None)))
    mgr.active_cast = _cc_dev(0.72)

    mgr.cast_set_initial_volume(30)
    mgr.stop_cast()

    assert events == [("vol", 30), ("vol", 72), ("stop", None)]
