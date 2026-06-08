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


# ── Failed/abandoned cast must resume LOCAL playback (review P0-2) ───────────


class _Emit:
    """Minimal stand-in for a Qt Signal — records emit() args."""

    def __init__(self):
        self.calls = []

    def emit(self, *a):
        self.calls.append(a)


def _dispatch_stub():
    bus = SimpleNamespace(
        stop_requested=_Emit(),
        play_requested=_Emit(),
        playback_started=_Emit(),
        cast_started=_Emit(),
    )
    return SimpleNamespace(bus=bus, cast_manager=None)


def test_failed_cast_resumes_local_playback(qapp, monkeypatch):
    # A track is playing; the user casts to a DLNA renderer and the push
    # fails. _cast_to_device stops local mpv up front, so on failure the
    # track must be RE-ARMED locally (play_requested, which restarts mpv at
    # np.position) — not left on "Nothing playing", and not a UI-only
    # playback_started (which wouldn't resume audio).
    import modules.cast_dispatcher as cd

    np = SimpleNamespace(item_id="trk1", stream_url="stream://trk1", position=42000)
    monkeypatch.setattr(cd, "get_now_playing", lambda: np)
    monkeypatch.setattr(cd, "get_provider", lambda: None)
    monkeypatch.setattr("modules.frosted_dialog.frosted_warning", lambda *a, **k: None)

    class _CM:
        active_cast = None

        def start_track(self, dev, np_, *, provider, resume_seconds, on_done):
            on_done(False)  # simulate cast failure

    stub = _dispatch_stub()
    stub.cast_manager = _CM()
    dev = SimpleNamespace(device_type=CastType.DLNA, name="Living Room TV")

    _CastDispatcherMixin._cast_to_device(stub, dev)

    assert len(stub.bus.stop_requested.calls) == 1, "local mpv stopped up front"
    assert len(stub.bus.play_requested.calls) == 1, "failed cast must resume local"
    assert stub.bus.play_requested.calls[0][0] is np
    assert stub.bus.cast_started.calls == []


def test_successful_cast_does_not_resume_local(qapp, monkeypatch):
    # The success path is UI-only on purpose: mpv stays stopped (the cast is
    # playing) and playback_started just re-renders the bar. It must NOT emit
    # play_requested, which would start local audio on top of the cast.
    import modules.cast_dispatcher as cd

    np = SimpleNamespace(item_id="trk1", stream_url="stream://trk1", position=0)
    monkeypatch.setattr(cd, "get_now_playing", lambda: np)
    monkeypatch.setattr(cd, "get_provider", lambda: None)

    class _CM:
        active_cast = None

        def start_track(self, dev, np_, *, provider, resume_seconds, on_done):
            on_done(True)  # simulate cast success

    stub = _dispatch_stub()
    stub.cast_manager = _CM()
    dev = SimpleNamespace(device_type=CastType.DLNA, name="Living Room TV")

    _CastDispatcherMixin._cast_to_device(stub, dev)

    assert len(stub.bus.cast_started.calls) == 1
    assert stub.bus.play_requested.calls == [], "success must not start local audio"
    assert len(stub.bus.playback_started.calls) == 1, "success re-renders the bar"
