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


def test_on_libraries_listed_populates_dropdown(isolated_settings):
    # The async result handler (boot + login paths both route through it)
    # must push the music-filtered libraries + the persisted selection into
    # the top bar. This is the piece that was missing on the relaunch path.
    ls.reset_after_server_change()
    top_bar = MagicMock()
    stub = SimpleNamespace(
        top_bar=top_bar,
        _sync_library_title=lambda: None,
    )
    raw = [
        {"Id": "m", "Name": "Music", "CollectionType": "music"},
        {"Id": "d", "Name": "Discover", "CollectionType": "music"},
        {"Id": "mov", "Name": "Movies", "CollectionType": "movies"},  # filtered out
    ]
    jellytoast.JellytoastWindow._on_libraries_listed(stub, raw)

    # Selection state learned the two music libraries (Movies dropped).
    assert [x["Id"] for x in ls.available_libraries()] == ["m", "d"]
    assert ls.has_multiple_libraries()
    # Top bar got the music-only list so the dropdown can appear.
    passed = top_bar.set_available_libraries.call_args[0][0]
    assert [x["Id"] for x in passed] == ["m", "d"]
    # ...and the FILTERED/normalized selection — [] after a reset, NOT the
    # raw stored ids or the available-library ids (a regression that pushed
    # those would render every library checked on boot).
    top_bar.set_selected_libraries.assert_called_once_with([])


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
    # Bind the real resolver + the reload helper so the parent id is the
    # live selection (no top_bar attr on the stub → the normalized push-back
    # is skipped, which is fine for the reload assertion).
    stub._music_parent_id = lambda: jellytoast.JellytoastWindow._music_parent_id(stub)
    stub._reload_music_surfaces = lambda: jellytoast.JellytoastWindow._reload_music_surfaces(
        stub
    )

    jellytoast.JellytoastWindow._on_libraries_changed(stub)

    album.load_items.assert_called_once_with("disc", "")
    artist.load_items.assert_called_once_with("disc", "")
    songs.load_songs.assert_called_once_with("disc")
    suggestions.load.assert_called_once_with("disc")


def test_on_libraries_selected_flushes_on_change(isolated_settings, monkeypatch):
    # A new selection must hit disk immediately — a hard tray-Quit right
    # after a change bypasses the QSettings destructor flush on KDE, so the
    # write would be lost otherwise (known_issue_qsettings_flush). Flush only
    # fires on an effective change, matching the emit.
    ls.reset_after_server_change()
    _seed_two()
    settings = jellytoast.get_settings()
    flushed = []
    monkeypatch.setattr(settings, "flush", lambda: flushed.append(True))
    stub = SimpleNamespace()

    jellytoast.JellytoastWindow._on_libraries_selected(stub, ["disc"])
    assert flushed == [True]  # changed → persisted to disk

    jellytoast.JellytoastWindow._on_libraries_selected(stub, ["disc"])
    assert flushed == [True]  # no effective change → no extra flush


def test_relaunch_heals_grid_when_stale_id_filtered(isolated_settings):
    # Saved-session relaunch: a stored library id that went stale server-side
    # is trusted verbatim during the boot window (before the async list
    # lands), scoping the home grid to a ghost parent → empty. Once the real
    # list arrives and filters the stale id out (→ 'all'), the surfaces must
    # reload so the user isn't stranded on a blank grid.
    ls.reset_after_server_change()
    # Persist a stale id directly (simulating a prior session's write — the
    # setter would filter it against the now-empty available list).
    jellytoast.get_settings().selected_library_ids = ["ghost"]
    assert ls.selected_ids() == ["ghost"]  # boot window trusts it

    reloaded = []
    stub = SimpleNamespace(
        top_bar=MagicMock(),
        _sync_library_title=lambda: None,
        _reload_music_surfaces=lambda: reloaded.append(True),
    )
    raw = [
        {"Id": "music-view", "Name": "Music", "CollectionType": "music"},
        {"Id": "disc", "Name": "Discover", "CollectionType": "music"},
    ]
    jellytoast.JellytoastWindow._on_libraries_listed(stub, raw)

    assert ls.selected_ids() == []  # ghost dropped → all
    assert reloaded == [True]  # ...and the grid reloaded so it heals


def test_relaunch_no_reload_when_selection_stable(isolated_settings):
    # The common relaunch: the stored selection is still valid, so the
    # effective selection doesn't change when the list lands → no spurious
    # reload (which would also risk re-introducing the doubled-albums race).
    ls.reset_after_server_change()
    jellytoast.get_settings().selected_library_ids = ["disc"]
    assert ls.selected_ids() == ["disc"]

    reloaded = []
    stub = SimpleNamespace(
        top_bar=MagicMock(),
        _sync_library_title=lambda: None,
        _reload_music_surfaces=lambda: reloaded.append(True),
    )
    raw = [
        {"Id": "music-view", "Name": "Music", "CollectionType": "music"},
        {"Id": "disc", "Name": "Discover", "CollectionType": "music"},
    ]
    jellytoast.JellytoastWindow._on_libraries_listed(stub, raw)

    assert ls.selected_ids() == ["disc"]
    assert reloaded == []


def test_refresh_library_selection_is_async_with_correct_wiring(isolated_settings, monkeypatch):
    # The boot/relaunch dropdown population MUST go through run_async (a
    # network get_libraries on the GUI thread would hang boot), wired to the
    # listed/failed handlers. Capture the run_async args without running it.
    captured = {}

    def fake_run_async(fn, on_result=None, on_error=None, **kw):
        captured["fn"] = fn
        captured["on_result"] = on_result
        captured["on_error"] = on_error

    monkeypatch.setattr(jellytoast, "run_async", fake_run_async)
    provider = _Provider(scopes_by_library=False)
    stub = SimpleNamespace(
        provider=provider,
        _on_libraries_listed=lambda libs: None,
        _on_libraries_list_failed=lambda e: None,
    )

    jellytoast.JellytoastWindow._refresh_library_selection(stub)

    assert captured["fn"] == provider.get_libraries  # the network call, off-thread
    assert captured["on_result"] == stub._on_libraries_listed
    assert captured["on_error"] == stub._on_libraries_list_failed
