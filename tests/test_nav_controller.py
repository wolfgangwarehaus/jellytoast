"""Contract test for the Nav cluster extracted into
``modules.nav_controller._NavMixin`` — the final cut of the JellytoastWindow
decomposition.

Nav is exercised end-to-end by the GUI (no isolated host unit tests; the
verbatim byte-diff review is the safety net). This pins the extraction shape and
the deliberate stay-on-window decisions: keyPressEvent (Qt override) and the
_NAV_HISTORY_CAP constant (host state) must NOT have moved to the mixin.
"""

from __future__ import annotations

from jellytoast import JellytoastWindow
from modules.nav_controller import _NavMixin

# Representative slice of the ~37 moved methods (one per sub-area).
MOVED = [
    "_on_nav_requested",
    "_on_tab_requested",
    "_show_downloads_library_view",
    "_show_native_music_grid",
    "_show_self",
    "_browse_album",
    "_show_now_playing",
    "_show_library_grid",
    "_show_songs_view",
    "_show_genres_view",
    "_show_suggestions_view",
    "_tab_anchors",
    "_current_section_index",
    "_push_nav",
    "_go_back",
    "_go_forward",
    "_route_home",
    "_show_search_view",
    "_show_artist_page",
    "_grid_play_collection",
    "_open_currently_playing_album",
]

# Window-core / Qt-override that the Nav slices were interleaved with.
STAYED = [
    "keyPressEvent",
    "_apply_blur",
    "_cascade_global_style",
    "_open_settings",
    "_kick_load_when_ready",
    "closeEvent",
]


def test_window_mixes_in_nav_with_single_qt_base():
    assert issubclass(JellytoastWindow, _NavMixin)
    assert _NavMixin.__bases__ == (object,)
    mro = [c.__name__ for c in JellytoastWindow.__mro__]
    assert mro.index("_NavMixin") < mro.index("QMainWindow")
    # All five controller mixins precede the Qt base, in declared order.
    for m in ("_NavMixin", "_SessionMixin", "_CastDispatcherMixin",
              "_ShufflePrimerMixin", "_LibrarySelectionMixin"):
        assert mro.index(m) < mro.index("QMainWindow")


def test_mixin_owns_no_state_or_initializer():
    assert "__init__" not in _NavMixin.__dict__


def test_moved_methods_live_on_the_mixin():
    for name in MOVED:
        assert name in _NavMixin.__dict__, f"{name} should be on the mixin"
        assert callable(getattr(JellytoastWindow, name))


def test_core_and_qt_overrides_did_not_move():
    for name in STAYED:
        assert name not in _NavMixin.__dict__, f"{name} should NOT be on the Nav mixin"
        assert name in JellytoastWindow.__dict__, f"{name} should stay on the window"
    # The nav-history cap is host state, kept on the window (the mixin's
    # _push_nav reads it via self, resolving on the combined instance).
    assert "_NAV_HISTORY_CAP" in JellytoastWindow.__dict__
    assert "_NAV_HISTORY_CAP" not in _NavMixin.__dict__
