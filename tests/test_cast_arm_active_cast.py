"""``CastManager._arm_active_cast`` — the GUI-thread marshaling seam for the
cross-thread ``active_cast`` write fix.

The Chromecast/DLNA/Sonos ``cast_to_*`` workers run on the async pool, so a
bare ``self.active_cast = dev`` there is an unsynchronized cross-thread write
(GIL-benign but unordered against the GUI thread's transport/poll reads). The
helper routes the assignment through ``async_io.call_on_gui``, which runs the
setter inline on the GUI thread (and in these main-thread tests) but queues it
from a worker — so the "arms ``active_cast`` on success" contract is preserved
while the write becomes thread-safe.
"""

from types import SimpleNamespace

from jellytoast import async_io as _aio
from jellytoast.cast_manager._manager import CastManager


def _dev(name="TV"):
    return SimpleNamespace(device_type="dlna", name=name)


# ── On the GUI thread: call_on_gui runs inline → the write is synchronous ────


def test_arm_sets_active_cast_synchronously_on_gui_thread(qapp):
    m = CastManager()
    d = _dev()
    m._arm_active_cast(d)
    assert m.active_cast is d


def test_arm_reset_paused_clears_cast_paused(qapp):
    m = CastManager()
    d = _dev()
    m._cast_paused = True  # left paused by a prior session
    m._arm_active_cast(d, reset_paused=True)
    assert m.active_cast is d
    assert m._cast_paused is False  # fresh cast starts playing


def test_arm_default_leaves_cast_paused_untouched(qapp):
    # Chromecast/connect arm active_cast WITHOUT resetting _cast_paused
    # (only DLNA/Sonos pass reset_paused=True) — preserve that exactly.
    m = CastManager()
    d = _dev()
    m._cast_paused = True
    m._arm_active_cast(d)  # reset_paused defaults False
    assert m.active_cast is d
    assert m._cast_paused is True


# ── Off the GUI thread: the write must be DEFERRED, not done inline ──────────


def test_arm_marshals_the_write_through_call_on_gui(monkeypatch):
    """Simulate a worker-thread call by making call_on_gui DEFER the setter
    (capture instead of run). Until the GUI thread drains it, active_cast must
    stay unset — proving the assignment is marshaled, never done inline on the
    pool worker. The lazy ``from jellytoast.async_io import call_on_gui`` inside
    the helper re-reads the patched module attribute, so this lands."""
    captured = []
    monkeypatch.setattr(_aio, "call_on_gui", lambda fn: captured.append(fn))

    m = CastManager()
    d = _dev()
    m._arm_active_cast(d, reset_paused=True)

    # Worker thread returned, but the GUI-thread setter hasn't run yet:
    assert m.active_cast is None, "write leaked onto the worker thread"
    assert getattr(m, "_cast_paused", None) is None
    assert len(captured) == 1

    # The GUI event loop now runs the marshaled setter:
    captured[0]()
    assert m.active_cast is d
    assert m._cast_paused is False
