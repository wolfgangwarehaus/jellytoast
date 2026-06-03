"""Contract tests for the cast-transport seam extracted into
``modules.player_cast_transport._CastTransportMixin``.

The local-vs-cast routing + cross-thread chromecast-status plumbing was
moved verbatim out of ``player_backend.py`` into a ``_CastTransportMixin``
mixed into ``MpvController`` (the last god-file decomposition). The
behavioural safety net is unchanged and lives in
``test_cast_enums_dispatch.py`` / ``test_cast_transport_dispatch.py`` /
``test_player_backend.py`` / ``test_sleep_timer*.py`` / ``test_bit_perfect_mode.py``.
These contract tests pin the *shape* so a future edit can't quietly give
the mixin state, drop a moved method, move a host helper onto it, or move
a Signal off the host (which would break the moved methods' ``self.bus`` /
``self._cast_*`` access and the race-sensitive ``active_cast`` reads).
"""

from __future__ import annotations

from PySide6.QtCore import QObject

from modules import player_backend
from modules.player_backend import MpvController
from modules.player_cast_transport import (
    _CastStatusForwarder,
    _CastStatusSignal,
    _CastTransportMixin,
)

# Methods that MUST live on the mixin (the cast routing + status machine).
MOVED = [
    "set_cast_manager",
    "_cast_active",
    "_ensure_cast_listener",
    "_on_cast_started",
    "_on_cast_stopped",
    "_on_cast_status_push",
    "_poll_cast_status",
    "_apply_cast_status",
    "_apply_dlna_status",
]

# Methods that MUST stay on the host: ``_reset_mpv_start`` is shared with
# ``play()`` (not cast-only); the bit-perfect cast hooks belong to the
# bit-perfect cluster; the cross-call targets are core mpv/session methods
# the mixin reaches back into via ``self``.
STAYED = [
    "_reset_mpv_start",
    "_on_cast_started_bit_perfect",
    "_on_cast_stopped_bit_perfect",
    "_on_position",
    "_on_duration",
    "_on_paused",
    "_on_ended",
    "_begin_play_session",
    "_report_session_start",
    "play",
    "toggle_pause",
    "_connect_bus",
]


def test_controller_mixes_in_cast_transport_with_single_qt_base():
    assert issubclass(MpvController, _CastTransportMixin)
    qt_bases = [b for b in MpvController.__bases__ if issubclass(b, QObject)]
    assert qt_bases == [QObject]
    mro = [c.__name__ for c in MpvController.__mro__]
    assert mro.index("_CastTransportMixin") < mro.index("QObject")


def test_mixin_is_plain_object_with_no_state_or_initializer():
    # A stateful mixin / its own __init__ would fight MpvController.__init__,
    # which owns every self._cast_* attribute (incl. the active_cast reads
    # the cast write-race fix depends on living on one instance).
    assert _CastTransportMixin.__bases__ == (object,)
    assert "__init__" not in _CastTransportMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _CastTransportMixin.__dict__, f"{name} should be on the mixin"
        assert name not in MpvController.__dict__, f"{name} leaked onto the host"
        assert callable(getattr(MpvController, name))


def test_host_only_helpers_did_not_move():
    for name in STAYED:
        assert name in MpvController.__dict__, f"{name} should stay on MpvController"
        assert name not in _CastTransportMixin.__dict__, f"{name} wrongly moved to the mixin"


def test_cast_initial_volume_constant_moved_with_the_cluster():
    # Read as self._CAST_INITIAL_VOLUME inside the moved _on_cast_started.
    assert _CastTransportMixin._CAST_INITIAL_VOLUME == 30


def test_status_carrier_classes_reexported_for_host_init():
    # MpvController.__init__ instantiates these as bare names, so the host
    # module must re-import them from the new home (and they must be the
    # same objects — not a divergent copy).
    assert player_backend._CastStatusSignal is _CastStatusSignal
    assert player_backend._CastStatusForwarder is _CastStatusForwarder
    assert issubclass(_CastStatusSignal, QObject)
