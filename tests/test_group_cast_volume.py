"""Group-cast volume:

1. Apply the user's saved per-speaker balance BEFORE play_media
   (``prepare_group_volume_before_media``), snapshotting each saved member's
   current level first — so audio starts AT the saved levels (no loud-then-
   quiet pop) and stop_cast can hand each speaker back its pre-cast level.
2. ``cast_set_initial_volume`` no longer forces a group master (the old force
   was the snap); it just reports the group's current aggregate.
3. stop_cast restores each member's pre-cast level (no quiet TV).

Real Chromecast group timing is verified on hardware; these pin the wiring.
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


def _member(uuid, level01, sets):
    """A discovered member device whose cast_object reports level01 (0-1) and
    records set_volume calls into ``sets``."""
    cc = SimpleNamespace(
        status=SimpleNamespace(volume_level=level01),
        wait=lambda timeout=3: None,
        set_volume=lambda v: sets.append((uuid, round(v, 4))),
    )
    return SimpleNamespace(uuid=uuid, cast_object=cc)


@pytest.fixture
def mgr(qapp, monkeypatch):
    m = CastManager()
    monkeypatch.setattr(m, "chromecast_stop", lambda: None)  # no real teardown
    return m


def test_prepare_applies_saved_before_media_and_snapshots(mgr, monkeypatch):
    sets = []
    mgr.chromecast_devices = [_member("tv", 0.80, sets), _member("kit", 0.20, sets)]
    monkeypatch.setattr(
        "modules.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"tv": 55, "kit": 20}}),
    )

    mgr.prepare_group_volume_before_media(_group("g1"))

    # Pre-cast levels snapshotted (as percent) for the on-stop restore.
    assert mgr._pre_cast_member_volumes == [
        {"uuid": "tv", "volume": 80},
        {"uuid": "kit", "volume": 20},
    ]
    # TV pushed to its saved 55% (0.55) before media; Kitchen already at its
    # saved 20% so it's left alone (no redundant set, no audible blip).
    assert sets == [("tv", 0.55)]


def test_prepare_is_idempotent_within_a_session(mgr, monkeypatch):
    # An auto-advance / re-cast must NOT re-snapshot the already-applied
    # levels as if they were the pre-cast ones.
    mgr._pre_cast_member_volumes = [{"uuid": "tv", "volume": 80}]
    sets = []
    mgr.chromecast_devices = [_member("tv", 0.10, sets)]
    monkeypatch.setattr(
        "modules.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"tv": 55}}),
    )

    mgr.prepare_group_volume_before_media(_group("g1"))

    assert mgr._pre_cast_member_volumes == [{"uuid": "tv", "volume": 80}]
    assert sets == []


def test_prepare_noop_without_a_saved_balance(mgr, monkeypatch):
    sets = []
    mgr.chromecast_devices = [_member("tv", 0.80, sets)]
    monkeypatch.setattr(
        "modules.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={}),
    )

    mgr.prepare_group_volume_before_media(_group("g1"))

    assert mgr._pre_cast_member_volumes is None
    assert sets == []


def test_group_initial_volume_no_force_returns_aggregate(mgr, monkeypatch):
    mgr.active_cast = _group()
    monkeypatch.setattr(mgr, "chromecast_get_volume", lambda: 62)
    forced = []
    monkeypatch.setattr(mgr, "chromecast_set_volume", lambda p: forced.append(p))

    applied = mgr.cast_set_initial_volume(30)

    assert forced == []      # no master force (that was the snap)
    assert applied == 62     # slider tracks the real aggregate


def test_group_restore_hands_members_back_on_stop(mgr, monkeypatch):
    mgr.active_cast = _group()
    mgr._pre_cast_member_volumes = [{"uuid": "tv", "volume": 80}, {"uuid": "kit", "volume": 20}]
    sets = []
    monkeypatch.setattr(mgr, "set_member_volume_async", lambda u, v, on_done=None: sets.append((u, v)))

    mgr.stop_cast()

    assert sets == [("tv", 80), ("kit", 20)]
    assert mgr._pre_cast_member_volumes is None


def test_single_chromecast_still_forces_and_snapshots(mgr, monkeypatch):
    # No regression for a normal (non-group) Chromecast.
    mgr.active_cast = _single()
    monkeypatch.setattr(mgr, "chromecast_get_volume", lambda: 70)
    forced = []
    monkeypatch.setattr(mgr, "chromecast_set_volume", lambda p: forced.append(p))

    applied = mgr.cast_set_initial_volume(30)

    assert forced == [30]
    assert applied == 30
    assert mgr._pre_cast_device_volume == 70
    assert mgr._pre_cast_member_volumes is None
