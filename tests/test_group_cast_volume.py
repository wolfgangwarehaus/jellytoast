"""Group-cast volume: snapshot each member's pre-cast level + restore it on
disconnect (so a TV speaker isn't left quiet), and apply the user's saved
per-speaker balance UP FRONT instead of forcing a master volume that then
audibly snaps to the saved levels.

Real Chromecast group timing is verified on hardware; these pin the
routing/wiring (which path a group takes, what gets snapshotted/applied/
restored) without a device.
"""

from types import SimpleNamespace

import pytest

from modules.cast_manager import CastType
from modules.cast_manager._manager import CastManager


def _group(uuid="g1"):
    return SimpleNamespace(
        name="Group", device_type=CastType.CHROMECAST, cast_type="group",
        uuid=uuid, cast_object=object(),
    )


def _single():
    return SimpleNamespace(
        name="Speaker", device_type=CastType.CHROMECAST, cast_type="audio",
        uuid="s1", cast_object=object(),
    )


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()
    monkeypatch.setattr(m, "chromecast_stop", lambda: None)  # no real teardown
    return m


def test_group_initial_volume_snapshots_and_applies_saved(mgr, monkeypatch):
    mgr.active_cast = _group()
    members = [
        {"uuid": "tv", "name": "TV", "volume": 80, "available": True},
        {"uuid": "kit", "name": "Kitchen", "volume": 20, "available": True},
        {"uuid": "off", "name": "Off", "volume": 50, "available": False},
    ]
    monkeypatch.setattr(mgr, "group_members_async", lambda dev, cb: cb(members))
    sets = []
    monkeypatch.setattr(mgr, "set_member_volume_async", lambda u, v, on_done=None: sets.append((u, v)))
    monkeypatch.setattr(mgr, "chromecast_get_volume", lambda: 62)
    forced = []
    monkeypatch.setattr(mgr, "chromecast_set_volume", lambda p: forced.append(p))
    monkeypatch.setattr(
        "modules.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"tv": 55, "kit": 20}}),
    )

    applied = mgr.cast_set_initial_volume(30)

    # The group master is NEVER force-set — forcing it is what caused the snap.
    assert forced == []
    # The master slider tracks the group's real aggregate, not the unused 30.
    assert applied == 62
    # Pre-cast levels snapshotted for the CONTROLLABLE members only.
    assert mgr._pre_cast_member_volumes == [
        {"uuid": "tv", "volume": 80},
        {"uuid": "kit", "volume": 20},
    ]
    # Saved balance applied up front: TV 80->55; Kitchen already at 20 (skipped);
    # the unavailable member is never touched.
    assert sets == [("tv", 55)]


def test_group_restore_hands_members_back_on_stop(mgr, monkeypatch):
    mgr.active_cast = _group()
    mgr._pre_cast_member_volumes = [{"uuid": "tv", "volume": 80}, {"uuid": "kit", "volume": 20}]
    sets = []
    monkeypatch.setattr(mgr, "set_member_volume_async", lambda u, v, on_done=None: sets.append((u, v)))

    mgr.stop_cast()

    # Each member handed back its pre-cast device volume.
    assert sets == [("tv", 80), ("kit", 20)]
    assert mgr._pre_cast_member_volumes is None


def test_single_chromecast_still_forces_and_snapshots(mgr, monkeypatch):
    # No regression for a normal (non-group) Chromecast: the master IS forced
    # to the cast initial volume and the single-device snapshot path runs.
    mgr.active_cast = _single()
    monkeypatch.setattr(mgr, "chromecast_get_volume", lambda: 70)
    forced = []
    monkeypatch.setattr(mgr, "chromecast_set_volume", lambda p: forced.append(p))

    applied = mgr.cast_set_initial_volume(30)

    assert forced == [30]
    assert applied == 30
    assert mgr._pre_cast_device_volume == 70
    assert mgr._pre_cast_member_volumes is None
