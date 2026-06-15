"""Mute must route through the cast device while casting — and unmute
must restore the LIVE cast level, not the local-playback baseline.

Bug-hunt regression: ``toggle_mute`` had no ``_cast_active()`` guard (the
lone transport method missing one), so muting while casting wrote 0 to the
idle local mpv handle — a silent no-op on the receiver — and still emitted
mute_state(True), desyncing the icon. The cast branch mirrors set_volume's.

Follow-up bug (this file's main assertions): the cast branch stashed
``settings.volume`` (the LOCAL baseline, often ~100%) and restored that on
unmute, while the cast device actually plays at ``_cast_volume`` (starts at
``_CAST_INITIAL_VOLUME`` = 30, tracks the slider independently). Unmuting a
speaker therefore slammed it from silence up to the local baseline —
deafening. The fix saves & restores ``_cast_volume``.

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


def _casting_controller(qapp, *, cast_volume=30, local_volume=70):
    bus = _Bus()
    states = []
    bus.mute_state.connect(states.append)
    cm = _FakeCastManager()
    obj = types.SimpleNamespace(
        _cast_active=lambda: True,
        _muted_volume=None,
        # The LIVE cast level — deliberately distinct from settings.volume
        # so a regression that re-uses the local baseline is caught.
        _cast_volume=cast_volume,
        _cast_volume_push_wall=0.0,
        _monotonic=lambda: 1000.0,
        _cast_manager=cm,
        bus=bus,
        settings=types.SimpleNamespace(volume=local_volume),
        _mpv=None,  # idle while casting
    )
    return obj, cm, states


def test_mute_while_casting_routes_to_device(qapp):
    obj, cm, states = _casting_controller(qapp)
    MpvController.toggle_mute(obj)
    assert cm.volume_calls == [0]
    assert states == [True]
    # Stashes the LIVE cast level (30), NOT settings.volume (70).
    assert obj._muted_volume == 30


def test_unmute_while_casting_restores_cast_level(qapp):
    obj, cm, states = _casting_controller(qapp)
    MpvController.toggle_mute(obj)  # mute
    MpvController.toggle_mute(obj)  # unmute
    # Comes back at the cast level (30), not the local baseline (70).
    assert cm.volume_calls == [0, 30]
    assert states == [True, False]
    assert obj._muted_volume is None


def test_unmute_never_restores_local_baseline(qapp):
    # The reported bug, made explicit: speaker at a quiet 20%, local
    # baseline at a loud 100%. Unmute must return to 20 — never 100.
    obj, cm, states = _casting_controller(qapp, cast_volume=20, local_volume=100)
    MpvController.toggle_mute(obj)  # mute
    MpvController.toggle_mute(obj)  # unmute
    assert cm.volume_calls == [0, 20]
    assert 100 not in cm.volume_calls
