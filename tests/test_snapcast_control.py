"""Snapcast control surface — the 2026-05-28 follow-up that turned the
'silent AirPlay misroute' guard into a real control dialog.

Snapcast is a routing matrix, not a URL push, so a pick opens
``SnapcastControlDialog`` (connect → route groups to streams + per-room
volume) rather than going through the active_cast play flow.

Covers:
- ``get_snapcast_controller`` singleton identity.
- ``snapshot_to_rows`` pure mapping: member resolution, stream/mute
  passthrough, unnamed-group name fallback, empty snapshot.
- The dialog's controller wiring (with a fake controller + the real
  PlayerBus): connects on open, renders on connect + on
  ``snapcast_state_changed``, routes a stream pick / volume change /
  mute back to the controller, and disconnects on close.

The visual layout is NOT asserted (no Snapcast hardware to verify
against — see module docstring); these guard the behaviour seam.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QSlider

from jellytoast.cast.snapcast import SnapcastServerInfo, get_snapcast_controller
from jellytoast.player_state import PlayerBus
from jellytoast.selector import Selector
from jellytoast.snapcast_control import SnapcastControlDialog, snapshot_to_rows

SNAP = {
    "connected": True,
    "server": {"host": "10.0.0.2", "port": 1705},
    "active_group_id": "",
    "streams": [{"id": "s1", "name": "Spotify"}, {"id": "s2", "name": "Default"}],
    "clients": [
        {"id": "c1", "name": "Kitchen", "volume": 40, "muted": False, "group_id": "g1"},
        {"id": "c2", "name": "Patio", "volume": 70, "muted": True, "group_id": "g1"},
    ],
    "groups": [
        {"id": "g1", "name": "", "stream_id": "s2", "muted": False, "client_ids": ["c1", "c2"]},
    ],
}


class FakeController:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.connected_to = None
        self.on_done = None
        self.calls = []
        self.disconnected = False

    def connect(self, host, port, on_done=None):
        self.connected_to = (host, port)
        self.on_done = on_done

    def snapshot(self):
        return self._snapshot

    def set_group_stream(self, gid, sid, on_done=None):
        self.calls.append(("set_group_stream", gid, sid))

    def set_group_mute(self, gid, muted, on_done=None):
        self.calls.append(("set_group_mute", gid, muted))

    def set_client_volume(self, cid, pct, on_done=None):
        self.calls.append(("set_client_volume", cid, pct))

    def set_client_mute(self, cid, muted, on_done=None):
        self.calls.append(("set_client_mute", cid, muted))

    def disconnect(self):
        self.disconnected = True


# ── singleton ─────────────────────────────────────────────────────


def test_get_snapcast_controller_is_singleton():
    a = get_snapcast_controller()
    b = get_snapcast_controller()
    assert a is b


# ── snapshot_to_rows (pure) ───────────────────────────────────────


def test_snapshot_to_rows_resolves_members_and_fields():
    model = snapshot_to_rows(SNAP)
    assert [s["id"] for s in model["streams"]] == ["s1", "s2"]
    assert len(model["groups"]) == 1
    g = model["groups"][0]
    assert g["group_id"] == "g1"
    assert g["stream_id"] == "s2"
    assert g["muted"] is False
    # Unnamed group → falls back to first member's name.
    assert g["group_name"] == "Kitchen"
    assert [c["id"] for c in g["clients"]] == ["c1", "c2"]
    assert g["clients"][1]["volume"] == 70


def test_snapshot_to_rows_named_group_keeps_name():
    snap = {"groups": [{"id": "g", "name": "Upstairs", "client_ids": []}], "clients": [], "streams": []}
    assert snapshot_to_rows(snap)["groups"][0]["group_name"] == "Upstairs"


def test_snapshot_to_rows_drops_unknown_client_ids():
    snap = {
        "groups": [{"id": "g", "name": "G", "client_ids": ["c1", "ghost"]}],
        "clients": [{"id": "c1", "name": "Real", "volume": 0}],
        "streams": [],
    }
    rows = snapshot_to_rows(snap)
    assert [c["id"] for c in rows["groups"][0]["clients"]] == ["c1"]


def test_snapshot_to_rows_empty():
    model = snapshot_to_rows({})
    assert model == {"streams": [], "groups": []}


# ── dialog wiring ─────────────────────────────────────────────────


@pytest.fixture
def opened_dialog(qapp):
    """A connected dialog backed by a fake controller. Closed on teardown
    so its PlayerBus subscriptions don't leak into other tests."""
    ctl = FakeController(SNAP)
    info = SnapcastServerInfo(host="10.0.0.2", port=1705, hostname="snap.local")
    d = SnapcastControlDialog(ctl, info, parent=None)
    yield d, ctl
    d.close()


def test_dialog_connects_on_open(opened_dialog):
    d, ctl = opened_dialog
    assert ctl.connected_to == ("10.0.0.2", 1705)
    assert callable(ctl.on_done)


def test_dialog_renders_on_connect(opened_dialog):
    d, ctl = opened_dialog
    ctl.on_done(True)  # simulate Server.GetStatus arriving
    selectors = d.findChildren(Selector)
    assert len(selectors) == 1  # one stream picker for the one group
    # The picker reflects the group's current stream (s2 / "Default").
    assert selectors[0].currentData() == "s2"
    # Two client volume sliders.
    assert len(d.findChildren(QSlider)) == 2


def test_state_changed_signal_rebuilds(opened_dialog):
    d, ctl = opened_dialog
    # No render yet (on_done not fired); a state push should still render.
    PlayerBus.get().snapcast_state_changed.emit(SNAP)
    assert len(d.findChildren(Selector)) == 1
    assert len(d.findChildren(QSlider)) == 2


def test_stream_pick_routes_to_controller(opened_dialog):
    d, ctl = opened_dialog
    ctl.on_done(True)
    sel = d.findChildren(Selector)[0]
    # Current is s2 (index 1); pick s1 (index 0).
    sel.setCurrentIndex(0)
    assert ("set_group_stream", "g1", "s1") in ctl.calls


def test_client_volume_release_routes_to_controller(opened_dialog):
    d, ctl = opened_dialog
    ctl.on_done(True)
    slider = d.findChildren(QSlider)[0]  # first client = c1
    slider.setValue(55)
    slider.sliderReleased.emit()
    assert ("set_client_volume", "c1", 55) in ctl.calls


def test_close_disconnects_controller(opened_dialog):
    d, ctl = opened_dialog
    d.close()
    assert ctl.disconnected is True
