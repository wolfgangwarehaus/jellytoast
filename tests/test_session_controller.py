"""Contract test for the Session lifecycle cluster extracted into
``modules.session_controller._SessionMixin``.

The session/auth methods need a full window + live provider to exercise, so
they have no host-level unit coverage (the verbatim byte-diff review is the
safety net). This pins the extraction shape: the 11 methods moved off the
window onto the mixin, the mixin stays state-free, and the window-core
lifecycle methods did not get swept in.
"""

from __future__ import annotations

from jellytoast import JellytoastWindow
from modules.session_controller import _SessionMixin

MOVED = [
    "_do_boot_auth_check",
    "_retry_empty_native_views",
    "_heartbeat",
    "_refresh_provider_refs",
    "_on_sign_out_requested",
    "_on_server_change_requested",
    "_on_auth_failed",
    "_reinstall_hotkeys",
    "_on_host_switched",
    "_on_verify_session_done",
    "_on_native_signed_in",
]

# Window-core / other-cluster methods the Session slices were interleaved with.
STAYED = ["_reveal_window", "_kick_load_when_ready", "_route_home", "_apply_blur"]


def test_window_mixes_in_session_with_single_qt_base():
    assert issubclass(JellytoastWindow, _SessionMixin)
    assert _SessionMixin.__bases__ == (object,)
    mro = [c.__name__ for c in JellytoastWindow.__mro__]
    assert mro.index("_SessionMixin") < mro.index("QMainWindow")


def test_mixin_owns_no_state_or_initializer():
    assert "__init__" not in _SessionMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _SessionMixin.__dict__, f"{name} should be on the mixin"
        assert callable(getattr(JellytoastWindow, name))


def test_core_methods_did_not_move():
    for name in STAYED:
        assert name not in _SessionMixin.__dict__, f"{name} should NOT be on the Session mixin"
        assert callable(getattr(JellytoastWindow, name))
