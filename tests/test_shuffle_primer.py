"""Tests for the ShufflePrimer cluster extracted into
``modules.shuffle_primer._ShufflePrimerMixin``.

Pins the extraction shape and adds the host-level shuffle coverage that didn't
exist before (the underlying smart_shuffle/QueueManager are tested elsewhere,
but the window's queue-install path was untested).
"""

from __future__ import annotations

from jellytoast import JellytoastWindow
from modules.player_state import PlayerBus, QueueKind
from modules.shuffle_primer import _ShufflePrimerMixin

MOVED = [
    "_library_shuffle",
    "_on_library_shuffle_loaded",
    "_on_library_shuffle_error",
    "_install_shuffle_queue",
    "_prime_random_queue_async",
    "_on_prime_random_queue_loaded",
]


def test_window_mixes_in_shuffle_primer_with_single_qt_base():
    assert issubclass(JellytoastWindow, _ShufflePrimerMixin)
    assert _ShufflePrimerMixin.__bases__ == (object,)
    mro = [c.__name__ for c in JellytoastWindow.__mro__]
    assert mro.index("_ShufflePrimerMixin") < mro.index("QMainWindow")


def test_mixin_owns_no_state_or_initializer():
    assert "__init__" not in _ShufflePrimerMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _ShufflePrimerMixin.__dict__, f"{name} should be on the mixin"
        assert callable(getattr(JellytoastWindow, name))


def test_install_shuffle_queue_emits_shuffle_context(qapp):
    # _install_shuffle_queue touches no self state — drive it unbound with a
    # bare stub and capture the bus emission.
    captured = []
    PlayerBus.get().queue_play_now.connect(lambda items, idx, ctx: captured.append((items, idx, ctx)))
    items = [{"Id": "1", "AlbumId": "a"}, {"Id": "2", "AlbumId": "b"}]
    _ShufflePrimerMixin._install_shuffle_queue(object(), items, "test shuffle")
    assert len(captured) == 1
    sent_items, start_idx, ctx = captured[0]
    assert sent_items == items
    assert start_idx == 0
    assert ctx.kind == QueueKind.SHUFFLE
