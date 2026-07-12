"""Multi-library selection state, resolve-to-fetch-plan, title, cache key,
and the partial-subset merge helper.

These are pure-logic tests (no Qt, no network). ``isolated_settings`` pins
the ``get_settings()`` singleton to a tmp-dir Settings so the persisted
``selected_library_ids`` round-trips in isolation.
"""

import pytest

from jellytoast import library_selection as ls
from jellytoast.settings import get_settings


def _seed(*names):
    """Install N available libraries named A, B, C… with ids la, lb, lc…"""
    libs = [{"Id": f"l{n.lower()}", "Name": n} for n in names]
    ls.set_available_libraries(libs)
    return [lib["Id"] for lib in libs]


def _reset():
    ls.reset_after_server_change()


@pytest.fixture(autouse=True)
def _clean_selection_state():
    """Reset library_selection's MODULE globals after every test here.

    ``_seed``/``set_selected_ids`` mutate module state (`_available` + the
    persisted selection); without teardown the LAST test's selection leaks
    into whatever file runs next. Concretely: the Jellyfin smart-playlist
    evaluator's ``query_items`` fans out one /Items pass per selected
    library (``_smart_folder_plan``), so a leaked 2-library selection
    doubled its expected call counts —
    tests/test_smart_playlist_evaluator.py's paged-fetch tests failed in
    full-suite order while passing alone."""
    yield
    ls.reset_after_server_change()


# ── Settings round-trip ────────────────────────────────────────────────────


def test_settings_selected_library_ids_round_trip(isolated_settings):
    s = isolated_settings
    assert s.selected_library_ids == []  # default = all
    s.selected_library_ids = ["lb", "la", "lb", "  ", "lc"]
    # de-duped, order-preserving, blanks dropped
    assert s.selected_library_ids == ["lb", "la", "lc"]


def test_settings_ignores_garbage(isolated_settings):
    isolated_settings._s.setValue("server/selected_library_ids", "not json")
    assert isolated_settings.selected_library_ids == []


# ── Availability + gating ──────────────────────────────────────────────────


def test_available_and_has_multiple(isolated_settings):
    _reset()
    assert ls.available_libraries() == []
    assert not ls.has_multiple_libraries()
    _seed("Music")
    assert not ls.has_multiple_libraries()  # single library → no dropdown
    _seed("Music", "Discover")
    assert ls.has_multiple_libraries()
    assert [x["Name"] for x in ls.available_libraries()] == ["Music", "Discover"]


def test_set_available_dedupes_and_cleans(isolated_settings):
    ls.set_available_libraries(
        [{"Id": "la", "Name": "A"}, {"Id": "", "Name": "blank"}, {"Id": "la", "Name": "dup"}]
    )
    assert ls.available_libraries() == [{"Id": "la", "Name": "A"}]


# ── Selection normalization ────────────────────────────────────────────────


def test_empty_selection_is_all(isolated_settings):
    _reset()
    _seed("Music", "Discover")
    assert ls.selected_ids() == []
    assert not ls.is_filtered()
    assert ls.fetch_plan() == [""]  # one unfiltered query
    assert ls.selection_cache_key() == ""
    assert ls.selection_title() == "Music"


def test_single_selection(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    changed = ls.set_selected_ids([ids[1]])  # Discover only
    assert changed
    assert ls.selected_ids() == ["ldiscover"]
    assert ls.is_filtered()
    assert ls.fetch_plan() == ["ldiscover"]
    assert ls.selection_cache_key() == "ldiscover"
    assert ls.selection_title() == "Discover"


def test_selecting_all_collapses_to_all(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    ls.set_selected_ids(ids)  # both → equivalent to 'all'
    assert ls.selected_ids() == []
    assert not ls.is_filtered()
    assert ls.fetch_plan() == [""]
    assert ls.selection_title() == "Music"


def test_set_selected_returns_change_flag(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    assert ls.set_selected_ids([ids[0]]) is True  # all → Music
    assert ls.set_selected_ids([ids[0]]) is False  # no-op
    assert ls.set_selected_ids(ids) is True  # Music → all (both)
    assert ls.set_selected_ids([]) is False  # all → all (no-op)


def test_unknown_ids_filtered_out(isolated_settings):
    _reset()
    _seed("Music", "Discover")
    ls.set_selected_ids(["lmusic", "ghost"])
    # 'ghost' isn't a known library → dropped; only Music remains.
    assert ls.selected_ids() == ["lmusic"]


def test_stale_selection_degrades_to_all_when_library_removed(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    ls.set_selected_ids([ids[1]])  # Discover only
    assert ls.selected_ids() == ["ldiscover"]
    # Server drops the Discover library; re-seed availability without it.
    ls.set_available_libraries([{"Id": "lmusic", "Name": "Music"}])
    # The stored id is now stale → degrades to 'all', not an empty grid.
    assert ls.selected_ids() == []
    assert ls.fetch_plan() == [""]


# ── Title formatting ───────────────────────────────────────────────────────


def test_title_two_and_many(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover", "Soundtracks")
    ls.set_selected_ids([ids[0], ids[1]])
    assert ls.selection_title() == "Music + Discover"
    ls.set_selected_ids([ids[0], ids[1], ids[2]])  # all 3 → collapses to all
    assert ls.selection_title() == "Music"
    # Re-seed a 4th so a 3-of-4 partial subset shows the +N form.
    ids = _seed("Music", "Discover", "Soundtracks", "Live")
    ls.set_selected_ids([ids[0], ids[1], ids[2]])
    assert ls.selection_title() == "Music +2"


def test_title_leads_with_primary_regardless_of_click_order(isolated_settings):
    # Selecting Discover THEN Music (a genuine 2-of-3 partial subset) should
    # still read "Music + Discover" — the title follows server order
    # (primary library leads), not the order the user toggled the rows.
    # (Uses 3 libraries so 2 selected is a real subset, not 'all'.)
    _reset()
    ids = _seed("Music", "Discover", "Soundtracks")
    ls.set_selected_ids([ids[1], ids[0]])  # Discover first, then Music
    assert ls.selection_title() == "Music + Discover"


def test_title_forms_ladder_two_libraries(isolated_settings):
    # The degradation ladder the top bar walks when the full title would
    # overrun the centred view dropdown: full → primary +N → count.
    _reset()
    ids = _seed("Music", "Discover", "Soundtracks")
    ls.set_selected_ids([ids[0], ids[1]])
    assert ls.selection_title_forms() == [
        "Music + Discover",
        "Music +1",
        "2 libraries",
    ]
    # selection_title() is exactly the most-informative form.
    assert ls.selection_title() == ls.selection_title_forms()[0]


def test_title_forms_ladder_many_libraries(isolated_settings):
    # 3-of-4 partial subset: no "A + B" form (only shown for exactly two),
    # so the ladder is the compact "+N" then the count.
    _reset()
    ids = _seed("Music", "Discover", "Soundtracks", "Live")
    ls.set_selected_ids([ids[0], ids[1], ids[2]])
    assert ls.selection_title_forms() == ["Music +2", "3 libraries"]


def test_title_forms_single_and_default(isolated_settings):
    # One library and 'all' collapse to a single, unshortenable form.
    _reset()
    ids = _seed("Music", "Discover")
    ls.set_selected_ids([ids[1]])
    assert ls.selection_title_forms() == ["Discover"]
    ls.set_selected_ids([])  # all → default
    assert ls.selection_title_forms("Music") == ["Music"]


# ── Reset on server change ─────────────────────────────────────────────────


def test_reset_clears_selection_and_availability(isolated_settings):
    _seed("Music", "Discover")
    ls.set_selected_ids(["ldiscover"])
    assert ls.selected_ids() == ["ldiscover"]
    ls.reset_after_server_change()
    assert ls.available_libraries() == []
    assert get_settings().selected_library_ids == []
    assert ls.fetch_plan() == [""]


# ── Provider-aware "all music" resolution ──────────────────────────────────


class _FakeProvider:
    def __init__(self, scopes_by_library):
        self.scopes_music_by_library = scopes_by_library


def test_music_libraries_filters_non_music():
    libs = [
        {"Id": "m", "Name": "Music", "CollectionType": "music"},
        {"Id": "v", "Name": "Movies", "CollectionType": "movies"},
        {"Id": "x", "Name": "NoType"},  # kept (defensive default-music)
    ]
    out = ls.music_libraries(libs)
    assert [x["Id"] for x in out] == ["m", "x"]


def test_all_libraries_parent_id_subsonic_is_empty(isolated_settings):
    _reset()
    _seed("Music", "Discover")
    prov = _FakeProvider(scopes_by_library=False)  # Subsonic-like
    assert ls.all_libraries_parent_id(prov) == ""
    assert ls.fetch_plan(prov) == [""]  # 'all' → one unfiltered query


def test_all_libraries_parent_id_jellyfin_uses_first_view(isolated_settings):
    _reset()
    _seed("Music", "Discover")  # ids: lmusic, ldiscover
    prov = _FakeProvider(scopes_by_library=True)  # Jellyfin-like
    assert ls.all_libraries_parent_id(prov) == "lmusic"


def test_fetch_plan_all_jellyfin_single_view_scopes_to_it(isolated_settings):
    _reset()
    _seed("Music")  # one music view
    prov = _FakeProvider(scopes_by_library=True)
    assert ls.fetch_plan(prov) == ["lmusic"]


def test_fetch_plan_all_jellyfin_multi_view_plans_every_view(isolated_settings):
    # A 2+-music-view Jellyfin server has no single union parent, so
    # 'all' must plan every view for a client-side merge — the Phase-1
    # gap where 'all' silently showed only the first view.
    _reset()
    _seed("Music", "Discover")
    prov = _FakeProvider(scopes_by_library=True)
    assert ls.fetch_plan(prov) == ["lmusic", "ldiscover"]


def test_fetch_plan_single_selection_ignores_provider(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    ls.set_selected_ids([ids[1]])
    # A concrete selection is the parent_id regardless of provider.
    assert ls.fetch_plan(_FakeProvider(True)) == ["ldiscover"]
    assert ls.fetch_plan(_FakeProvider(False)) == ["ldiscover"]


# ── Union fetch (the multi-folder plan path) ───────────────────────────────


def _library_rows():
    """Two folders whose albums interleave alphabetically by name, so a
    naive concatenation would NOT be globally sorted."""
    folder_a = [{"Id": f"a{i}", "Name": n} for i, n in enumerate(["Apple", "Cherry", "Elder"])]
    folder_b = [{"Id": f"b{i}", "Name": n} for i, n in enumerate(["Banana", "Date", "Fig"])]
    return {"la": folder_a, "lb": folder_b}


def _make_fetch(data, calls=None):
    def fetch(parent_id, offset, count):
        if calls is not None:
            calls.append((parent_id, offset, count))
        rows = data.get(parent_id, [])
        return rows[offset : offset + count]

    return fetch


def test_fetch_union_globally_sorted(isolated_settings):
    fetch = _make_fetch(_library_rows())
    out = ls.fetch_union(fetch, ["la", "lb"], sort_key=lambda it: it["Name"])
    assert [it["Name"] for it in out] == [
        "Apple", "Banana", "Cherry", "Date", "Elder", "Fig",
    ]


def test_fetch_union_reverse(isolated_settings):
    fetch = _make_fetch(_library_rows())
    out = ls.fetch_union(
        fetch, ["la", "lb"], sort_key=lambda it: it["Name"], reverse=True
    )
    assert [it["Name"] for it in out] == [
        "Fig", "Elder", "Date", "Cherry", "Banana", "Apple",
    ]


def test_fetch_union_dedupes_shared_ids(isolated_settings):
    data = {
        "la": [{"Id": "x", "Name": "Shared"}, {"Id": "a1", "Name": "Alpha"}],
        "lb": [{"Id": "x", "Name": "Shared"}, {"Id": "b1", "Name": "Beta"}],
    }
    out = ls.fetch_union(_make_fetch(data), ["la", "lb"], sort_key=lambda it: it["Name"])
    assert [it["Id"] for it in out].count("x") == 1
    assert [it["Name"] for it in out] == ["Alpha", "Beta", "Shared"]


def test_fetch_union_drains_multi_page_folders(isolated_settings):
    # A folder bigger than page_size must be drained page by page until
    # its short tail page — not truncated at one page.
    big = [{"Id": f"a{i}", "Name": f"N{i:04d}"} for i in range(7)]
    data = {"la": big, "lb": [{"Id": "b0", "Name": "N9999"}]}
    calls: list = []
    out = ls.fetch_union(
        _make_fetch(data, calls),
        ["la", "lb"],
        sort_key=lambda it: it["Name"],
        page_size=3,
    )
    assert len(out) == 8
    # la: offsets 0,3,6 (page 6 is short → stop); lb: offset 0 only.
    assert [(p, o) for p, o, _c in calls] == [
        ("la", 0), ("la", 3), ("la", 6), ("lb", 0),
    ]


def test_union_sort_key_matches_grid_collation(isolated_settings):
    # Article stripping — "The Weeknd" sorts under W, as the grids do.
    key = ls.union_sort_key("SortName")
    items = [
        {"Id": "1", "Name": "The Weeknd"},
        {"Id": "2", "Name": "Aphex Twin"},
    ]
    assert [it["Name"] for it in sorted(items, key=key)] == [
        "Aphex Twin", "The Weeknd",
    ]


def test_union_sort_key_composite_and_missing_fields(isolated_settings):
    # Composite "AlbumArtist,SortName" breaks ties by name, and a
    # missing AlbumArtist (None) must not raise against a str.
    key = ls.union_sort_key("AlbumArtist,SortName")
    items = [
        {"Id": "1", "AlbumArtist": "Zed", "Name": "A"},
        {"Id": "2", "AlbumArtist": None, "Name": "B"},
        {"Id": "3", "AlbumArtist": "Zed", "Name": "B"},
    ]
    out = [it["Id"] for it in sorted(items, key=key)]
    assert out == ["2", "1", "3"]


def test_union_sort_key_playcount_numeric(isolated_settings):
    key = ls.union_sort_key("PlayCount,SortName")
    items = [
        {"Id": "1", "Name": "A", "UserData": {"PlayCount": 2}},
        {"Id": "2", "Name": "B"},  # no UserData at all
        {"Id": "3", "Name": "C", "UserData": {"PlayCount": 10}},
    ]
    out = [it["Id"] for it in sorted(items, key=key, reverse=True)]
    assert out == ["3", "1", "2"]
