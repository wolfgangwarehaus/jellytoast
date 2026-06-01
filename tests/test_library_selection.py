"""Multi-library selection state, resolve-to-fetch-plan, title, cache key,
and the partial-subset merge helper.

These are pure-logic tests (no Qt, no network). ``isolated_settings`` pins
the ``get_settings()`` singleton to a tmp-dir Settings so the persisted
``selected_library_ids`` round-trips in isolation.
"""

from modules import library_selection as ls
from modules.settings import get_settings


def _seed(*names):
    """Install N available libraries named A, B, C… with ids la, lb, lc…"""
    libs = [{"Id": f"l{n.lower()}", "Name": n} for n in names]
    ls.set_available_libraries(libs)
    return [lib["Id"] for lib in libs]


def _reset():
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
    assert ls.fetch_plan(prov) == ["lmusic"]


def test_fetch_plan_single_selection_ignores_provider(isolated_settings):
    _reset()
    ids = _seed("Music", "Discover")
    ls.set_selected_ids([ids[1]])
    # A concrete selection is the parent_id regardless of provider.
    assert ls.fetch_plan(_FakeProvider(True)) == ["ldiscover"]
    assert ls.fetch_plan(_FakeProvider(False)) == ["ldiscover"]


# ── Merge helper (the 3+-library partial-subset path) ──────────────────────


def _library_rows():
    """Two folders whose albums interleave alphabetically by name, so a
    naive concatenation would NOT be globally sorted."""
    folder_a = [{"Id": f"a{i}", "Name": n} for i, n in enumerate(["Apple", "Cherry", "Elder"])]
    folder_b = [{"Id": f"b{i}", "Name": n} for i, n in enumerate(["Banana", "Date", "Fig"])]
    return {"la": folder_a, "lb": folder_b}


def _make_fetch(data):
    def fetch(parent_id, offset, count):
        rows = data.get(parent_id, [])
        return rows[offset : offset + count]

    return fetch


def test_merge_paged_globally_sorted_window():
    data = _library_rows()
    fetch = _make_fetch(data)
    key = lambda it: it["Name"]  # noqa: E731
    page = ls.merge_paged(
        fetch, ["la", "lb"], start_index=0, limit=4, sort_key=key
    )
    assert [it["Name"] for it in page] == ["Apple", "Banana", "Cherry", "Date"]
    # Next window continues without gaps or repeats.
    page2 = ls.merge_paged(
        fetch, ["la", "lb"], start_index=4, limit=4, sort_key=key
    )
    assert [it["Name"] for it in page2] == ["Elder", "Fig"]


def test_merge_paged_full_union_no_dupes_no_gaps():
    data = _library_rows()
    fetch = _make_fetch(data)
    key = lambda it: it["Name"]  # noqa: E731
    collected = []
    offset = 0
    # Walk the whole union one page at a time, as the grid cascade would.
    while True:
        page = ls.merge_paged(
            fetch, ["la", "lb"], start_index=offset, limit=2, sort_key=key
        )
        if not page:
            break
        collected.extend(page)
        offset += 2
    names = [it["Name"] for it in collected]
    assert names == ["Apple", "Banana", "Cherry", "Date", "Elder", "Fig"]
    assert len(names) == len(set(names))  # no dupes


def test_merge_paged_dedupes_shared_ids():
    # The same album id present in two folders must appear once.
    data = {
        "la": [{"Id": "x", "Name": "Shared"}, {"Id": "a1", "Name": "Alpha"}],
        "lb": [{"Id": "x", "Name": "Shared"}, {"Id": "b1", "Name": "Beta"}],
    }
    fetch = _make_fetch(data)
    key = lambda it: it["Name"]  # noqa: E731
    page = ls.merge_paged(fetch, ["la", "lb"], start_index=0, limit=10, sort_key=key)
    ids = [it["Id"] for it in page]
    assert ids.count("x") == 1
    assert [it["Name"] for it in page] == ["Alpha", "Beta", "Shared"]


def test_merge_paged_reverse():
    data = _library_rows()
    fetch = _make_fetch(data)
    key = lambda it: it["Name"]  # noqa: E731
    page = ls.merge_paged(
        fetch, ["la", "lb"], start_index=0, limit=3, sort_key=key, reverse=True
    )
    assert [it["Name"] for it in page] == ["Fig", "Elder", "Date"]


def test_merge_paged_single_folder_passthrough():
    data = _library_rows()
    fetch = _make_fetch(data)
    key = lambda it: it["Name"]  # noqa: E731
    # One folder → direct paging, no merge.
    page = ls.merge_paged(fetch, ["la"], start_index=1, limit=2, sort_key=key)
    assert [it["Name"] for it in page] == ["Cherry", "Elder"]
