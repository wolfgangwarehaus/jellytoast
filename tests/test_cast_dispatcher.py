"""Tests for the Cast dispatch cluster extracted into
``modules.cast_dispatcher._CastDispatcherMixin``.

The cast backends (CastManager / cast_dialog / cast_proxy / the per-protocol
controllers) are covered by the test_cast_* suite; the host-level dispatch
(`_cast_to_device`) was untested. This pins the extraction shape and adds a
host-level routing test for the cleanest, most consequential dispatch decision:
Snapcast must NOT enter the stop/cast flow — it hands off to the control surface.
"""

from __future__ import annotations

from types import SimpleNamespace

from jellytoast import JellytoastWindow
from modules.cast_dispatcher import _CastDispatcherMixin
from modules.cast_manager import CastType

MOVED = [
    "_open_cast_dialog",
    "_show_cast_context_menu",
    "_disconnect_cast",
    "_find_cast_device",
    "_cast_to_favorite",
    "_cast_to_device",
    "_open_snapcast_control",
]


def test_window_mixes_in_cast_dispatcher_with_single_qt_base():
    assert issubclass(JellytoastWindow, _CastDispatcherMixin)
    assert _CastDispatcherMixin.__bases__ == (object,)
    mro = [c.__name__ for c in JellytoastWindow.__mro__]
    assert mro.index("_CastDispatcherMixin") < mro.index("QMainWindow")


def test_mixin_owns_no_state_or_initializer():
    assert "__init__" not in _CastDispatcherMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _CastDispatcherMixin.__dict__, f"{name} should be on the mixin"
        assert callable(getattr(JellytoastWindow, name))


def test_cast_to_device_routes_snapcast_to_control_surface(qapp):
    # Snapcast is a routing matrix, not a play target: _cast_to_device must
    # hand off to _open_snapcast_control and return BEFORE the stop/cast flow
    # (no stop_requested, no active_cast). Drive it unbound with a minimal stub.
    captured = []
    stub = SimpleNamespace(_open_snapcast_control=lambda dev: captured.append(dev))
    dev = SimpleNamespace(device_type=CastType.SNAPCAST, name="Living Room")
    _CastDispatcherMixin._cast_to_device(stub, dev)
    assert captured == [dev]
