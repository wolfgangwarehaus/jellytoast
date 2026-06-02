"""Contract test for the LibrarySelection cluster extracted into
``modules.library_selection_controller._LibrarySelectionMixin``.

The behavioural coverage lives in ``test_host_library_selection.py`` (it
drives the real methods as unbound ``JellytoastWindow._method(stub)`` calls —
unchanged by this extraction since the mixin keeps them resolvable on the
class). This pins the *shape* so a future edit can't give the mixin state or
drop a moved method off the window.
"""

from __future__ import annotations

from jellytoast import JellytoastWindow
from modules.library_selection_controller import _LibrarySelectionMixin

MOVED = [
    "_resolve_library_id",
    "_music_parent_id",
    "_refresh_library_selection",
    "_on_libraries_listed",
    "_on_libraries_list_failed",
    "_sync_library_title",
    "_on_libraries_selected",
    "_on_libraries_changed",
    "_reload_music_surfaces",
]


def test_window_mixes_in_library_selection_with_single_qt_base():
    assert issubclass(JellytoastWindow, _LibrarySelectionMixin)
    assert _LibrarySelectionMixin.__bases__ == (object,)
    mro = [c.__name__ for c in JellytoastWindow.__mro__]
    assert mro.index("_LibrarySelectionMixin") < mro.index("QMainWindow")


def test_mixin_owns_no_state_or_initializer():
    assert "__init__" not in _LibrarySelectionMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _LibrarySelectionMixin.__dict__, f"{name} should be on the mixin"
        assert callable(getattr(JellytoastWindow, name))


def test_window_core_methods_stayed():
    # _open_settings was adjacent to the cluster but is window core; it must
    # NOT have been swept into the mixin.
    assert "_open_settings" in JellytoastWindow.__dict__
    assert "_open_settings" not in _LibrarySelectionMixin.__dict__
