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

from jellytoast.cast_manager import CastType
from jellytoast.cast_manager._manager import CastManager


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
        "jellytoast.settings.get_settings",
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
        "jellytoast.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"tv": 55}}),
    )

    mgr.prepare_group_volume_before_media(_group("g1"))

    assert mgr._pre_cast_member_volumes == [{"uuid": "tv", "volume": 80}]
    assert sets == []


def test_prepare_noop_without_a_saved_balance(mgr, monkeypatch):
    sets = []
    mgr.chromecast_devices = [_member("tv", 0.80, sets)]
    monkeypatch.setattr(
        "jellytoast.settings.get_settings",
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


def test_prepare_sets_members_in_parallel(mgr, monkeypatch):
    # One slow / offline speaker mustn't hold the others (and the first
    # track) back by its connect timeout, one after another.
    import threading
    import time

    barrier = threading.Barrier(3, timeout=2)
    sets = []

    def _slow(uuid, level01):
        dev = _member(uuid, level01, sets)
        dev.cast_object.wait = lambda timeout=3: barrier.wait()
        return dev

    mgr.chromecast_devices = [_slow("a", 0.5), _slow("b", 0.5), _slow("c", 0.5)]
    monkeypatch.setattr(
        "jellytoast.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"a": 20, "b": 30, "c": 40}}),
    )
    t0 = time.monotonic()
    mgr.prepare_group_volume_before_media(_group("g1"))  # serial would hit the barrier timeout
    assert time.monotonic() - t0 < 1.5
    assert sorted(sets) == [("a", 0.2), ("b", 0.3), ("c", 0.4)]
    assert [m["uuid"] for m in mgr._pre_cast_member_volumes] == ["a", "b", "c"]


def test_prepare_skips_unreachable_member(mgr, monkeypatch):
    sets = []
    bad = _member("bad", 0.5, sets)

    def _timeout(timeout=3):
        raise TimeoutError("offline")

    bad.cast_object.wait = _timeout
    mgr.chromecast_devices = [_member("ok", 0.5, sets), bad]
    monkeypatch.setattr(
        "jellytoast.settings.get_settings",
        lambda: SimpleNamespace(cast_member_volumes={"g1": {"ok": 20, "bad": 30}}),
    )
    mgr.prepare_group_volume_before_media(_group("g1"))
    assert sets == [("ok", 0.2)]
    assert mgr._pre_cast_member_volumes == [{"uuid": "ok", "volume": 50}]


def test_group_members_reuses_one_multizone_controller(mgr, monkeypatch):
    # Every volume-popup open registered another controller on the group's
    # connection; they piled up for the whole session.
    import sys
    import types

    registered = []

    class _MZ:
        def __init__(self, uuid):
            self.members = ["m1"]
            self._members = {"m1": "Kitchen"}

        def update_members(self):
            pass

    fake_mod = types.ModuleType("pychromecast.controllers.multizone")
    fake_mod.MultizoneController = _MZ
    monkeypatch.setitem(sys.modules, "pychromecast.controllers.multizone", fake_mod)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "jellytoast.cast_manager.run_async",
        lambda fn, on_result=None, on_error=None: on_result(fn()),
    )
    group_cc = SimpleNamespace(uuid="g1", wait=lambda timeout=6: None,
                               register_handler=registered.append)
    group = SimpleNamespace(name="Group", uuid="g1", cast_object=group_cc)
    out = []
    mgr.group_members_async(group, out.append)
    mgr.group_members_async(group, out.append)
    assert len(registered) == 1
    assert out[0] == out[1] and out[0][0]["name"] == "Kitchen"
