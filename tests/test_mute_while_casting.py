"""Mute must route through the cast device while casting.

Bug-hunt regression: ``toggle_mute`` had no ``_cast_active()`` guard (the
lone transport method missing one), so muting while casting wrote 0 to the
idle local mpv handle — a silent no-op on the receiver — and still emitted
mute_state(True), desyncing the icon. The cast branch mirrors set_volume's.

The cast branch is self-contained (it returns before touching mpv), so it
is exercised here with a minimal stand-in self rather than a full
MpvController + real mpv.
"""

import types

from PySide6.QtCore import QObject, Signal

from jellytoast.player_backend import MpvController


class _Bus(QObject):
    mute_state = Signal(bool)


class _FakeCastManager:
    def __init__(self):
        self.volume_calls = []

    def cast_set_volume(self, v):
        self.volume_calls.append(v)


def _casting_controller(qapp):
    bus = _Bus()
    states = []
    bus.mute_state.connect(states.append)
    cm = _FakeCastManager()
    obj = types.SimpleNamespace(
        _cast_active=lambda: True,
        _muted_volume=None,
        _cast_manager=cm,
        bus=bus,
        settings=types.SimpleNamespace(volume=70),
        _mpv=None,  # idle while casting
    )
    return obj, cm, states


def test_mute_while_casting_routes_to_device(qapp):
    obj, cm, states = _casting_controller(qapp)
    MpvController.toggle_mute(obj)
    assert cm.volume_calls == [0]
    assert states == [True]
    assert obj._muted_volume == 70


def test_unmute_while_casting_restores_baseline(qapp):
    obj, cm, states = _casting_controller(qapp)
    MpvController.toggle_mute(obj)  # mute
    MpvController.toggle_mute(obj)  # unmute
    assert cm.volume_calls == [0, 70]
    assert states == [True, False]
    assert obj._muted_volume is None
