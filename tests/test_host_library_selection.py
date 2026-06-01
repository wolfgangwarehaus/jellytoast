"""Host glue for multi-library selection: ``_music_parent_id`` resolution
and ``_on_libraries_selected`` persist+emit.

These bind the unbound ``JellytoastWindow`` methods to a tiny stub ``self``
so we exercise the real logic without constructing a full window (which
needs a QApplication + the whole widget tree). The stub carries only the
attributes the methods touch.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import jellytoast
from modules import library_selection as ls
from modules.player_state import PlayerBus


class _Provider:
    def __init__(self, scopes_by_library, music_id="music-view"):
        self.scopes_music_by_library = scopes_by_library
        self._music_id = music_id

    def get_libraries(self):
        return [
            {"Id": self._music_id, "Name": "Music", "CollectionType": "music"},
            {"Id": "disc", "Name": "Discover", "CollectionType": "music"},
        ]


def _stub(provider):
    """A stub standing in for the window for the two glue methods.
    ``_resolve_library_id`` is faked to return the provider's music id."""
    s = SimpleNamespace(
        provider=provider,
        _resolve_library_id=lambda ct: provider._music_id,
    )
    return s


def _seed_two():
    ls.set_available_libraries(
        [{"Id": "music-view", "Name": "Music"}, {"Id": "disc", "Name": "Discover"}]
    )


def test_music_parent_id_all_subsonic_is_empty(isolated_settings):
    ls.reset_after_server_change()
    _seed_two()
    stub = _stub(_Provider(scopes_by_library=False))
    # 'all' on a music-only server → empty parent (union of folders).
    assert jellytoast.JellytoastWindow._music_parent_id(stub) == ""


def test_music_parent_id_all_jellyfin_uses_music_view(isolated_settings):
    ls.reset_after_server_change()
    _seed_two()
    stub = _stub(_Provider(scopes_by_library=True))
    # 'all' on a mixed-content server → scope to the music view id.
    assert jellytoast.JellytoastWindow._music_parent_id(stub) == "music-view"


def test_music_parent_id_single_selection(isolated_settings):
    ls.reset_after_server_change()
    _seed_two()
    ls.set_selected_ids(["disc"])
    stub = _stub(_Provider(scopes_by_library=False))
    assert jellytoast.JellytoastWindow._music_parent_id(stub) == "disc"


def test_music_parent_id_partial_subset_degrades_to_all(isolated_settings):
    # A 3+-library partial subset (no merge wired yet) → loads all music,
    # not a wrong subset.
    ls.reset_after_server_change()
    ls.set_available_libraries(
        [
            {"Id": "music-view", "Name": "Music"},
            {"Id": "disc", "Name": "Discover"},
            {"Id": "live", "Name": "Live"},
        ]
    )
    ls.set_selected_ids(["disc", "live"])  # 2 of 3 → partial
    stub = _stub(_Provider(scopes_by_library=False))
    assert jellytoast.JellytoastWindow._music_parent_id(stub) == ""


def test_on_libraries_selected_emits_only_on_change(isolated_settings):
    ls.reset_after_server_change()
    _seed_two()
    fired = []
    PlayerBus.get().libraries_changed.connect(lambda: fired.append(True))
    stub = SimpleNamespace()

    # all → Discover: effective change → emits.
    jellytoast.JellytoastWindow._on_libraries_selected(stub, ["disc"])
    assert ls.selected_ids() == ["disc"]
    assert len(fired) == 1

    # Re-selecting the same thing: no effective change → no emit.
    jellytoast.JellytoastWindow._on_libraries_selected(stub, ["disc"])
    assert len(fired) == 1

    # Selecting both == 'all' → effective change → emits, normalizes to [].
    jellytoast.JellytoastWindow._on_libraries_selected(stub, ["disc", "music-view"])
    assert ls.selected_ids() == []
    assert len(fired) == 2


def test_on_libraries_changed_reloads_built_surfaces(isolated_settings):
    ls.reset_after_server_change()
    _seed_two()
    ls.set_selected_ids(["disc"])

    album = MagicMock()
    artist = MagicMock()
    songs = MagicMock()
    suggestions = MagicMock()
    stub = SimpleNamespace(
        provider=_Provider(scopes_by_library=False),
        _resolve_library_id=lambda ct: "music-view",
        album_grid=album,
        artist_grid=artist,
        songs_view=songs,
        suggestions_view=suggestions,
        _sync_library_title=lambda: None,
    )
    # Bind the real resolver so the parent id is the live selection.
    stub._music_parent_id = lambda: jellytoast.JellytoastWindow._music_parent_id(stub)

    jellytoast.JellytoastWindow._on_libraries_changed(stub)

    album.load_items.assert_called_once_with("disc", "")
    artist.load_items.assert_called_once_with("disc", "")
    songs.load_songs.assert_called_once_with("disc")
    suggestions.load.assert_called_once_with("disc")
