"""Boot-time seeding of shuffle / repeat state onto UI surfaces.

QueueManager loads ``settings.shuffle`` / ``settings.repeat_mode`` at
construction, but the surfaces that RENDER that state (now-playing bar
buttons, the MPRIS Shuffle/LoopStatus properties) historically booted
to hardcoded "off" and only synced on a later change signal. Result: a
restart with persisted shuffle=True silently scrambled every album
while the button showed off (live find 2026-06-12).

Each surface must self-seed from settings at construction — no bus
emission (an emit would bounce into QueueManager._on_shuffle_changed
and re-permute the restored queue).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fresh_bus():
    from jellytoast.player_state import PlayerBus

    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def _shuffle_all_settings(isolated_settings):
    isolated_settings.shuffle = True
    isolated_settings.repeat_mode = "all"
    return isolated_settings


def test_now_playing_bar_seeds_toggles(qapp, fresh_bus, _shuffle_all_settings):
    from jellytoast.now_playing_bar import NowPlayingBar

    bar = NowPlayingBar()
    try:
        assert bar.shuffle_btn.isChecked() is True
        assert bar._repeat_state == "all"
        assert bar.repeat_btn.isChecked() is True
    finally:
        bar.deleteLater()


def test_now_playing_bar_seed_does_not_emit(qapp, fresh_bus, _shuffle_all_settings):
    """Seeding must not bounce shuffle_changed back onto the bus —
    QueueManager would re-permute the restored queue."""
    from jellytoast.now_playing_bar import NowPlayingBar
    from jellytoast.player_state import PlayerBus

    bus = PlayerBus.get()
    fired = []
    bus.shuffle_changed.connect(lambda on: fired.append(on))
    bar = NowPlayingBar()
    try:
        assert fired == []
    finally:
        bar.deleteLater()


def test_now_playing_bar_defaults_stay_off(qapp, fresh_bus, isolated_settings):
    from jellytoast.now_playing_bar import NowPlayingBar

    isolated_settings.shuffle = False
    isolated_settings.repeat_mode = "off"
    bar = NowPlayingBar()
    try:
        assert bar.shuffle_btn.isChecked() is False
        assert bar._repeat_state == "off"
    finally:
        bar.deleteLater()


def test_mpris_seeds_shuffle_and_loop(qapp, fresh_bus, _shuffle_all_settings):
    dbus_next = pytest.importorskip("dbus_next")  # noqa: F841 — Linux-only dep
    from jellytoast.media_controls._mpris import MprisPlayer
    from jellytoast.player_state import PlayerBus

    player = MprisPlayer(PlayerBus.get())
    assert player._shuffle is True
    assert player._loop == "Playlist"


def test_mpris_defaults_stay_off(qapp, fresh_bus, isolated_settings):
    dbus_next = pytest.importorskip("dbus_next")  # noqa: F841
    from jellytoast.media_controls._mpris import MprisPlayer
    from jellytoast.player_state import PlayerBus

    isolated_settings.shuffle = False
    isolated_settings.repeat_mode = "off"
    player = MprisPlayer(PlayerBus.get())
    assert player._shuffle is False
    assert player._loop == "None"
