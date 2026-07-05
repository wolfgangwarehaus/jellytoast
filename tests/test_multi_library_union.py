"""Phase 2 multi-library wiring: a 2+-folder fetch plan actually filters.

The plan resolution itself is covered in test_library_selection /
test_host_library_selection; these tests pin the SURFACE wiring — the
grid paginator, the Songs view, and the Suggestions rails fetching every
folder in the plan, merging client-side, and never running the
per-folder pagination cascades against a merged list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jellytoast import disk_cache as _disk_cache
from jellytoast import library_grid as lg
from jellytoast import library_paginator as lp
from jellytoast import songs_view as sv_mod
from jellytoast import suggestions_view as sug_mod
from jellytoast.songs_view import SongsView
from jellytoast.suggestions_view import RAIL_LIMIT, SuggestionsView
from tests.conftest import force_sync_render


def _inline_run_async(fn, *args, on_result=None, on_error=None, **kwargs):
    """Synchronous run_async stand-in: the worker fn runs inline and its
    result/exception is dispatched immediately."""
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — mirrors run_async's error path
        if on_error is not None:
            on_error(e)
        return
    if on_result is not None:
        on_result(result)


# Two folders whose album names interleave alphabetically, so a naive
# per-folder concatenation is visibly NOT globally sorted.
_FOLDER_ALBUMS = {
    "fa": [
        {"Id": "a0", "Name": "Apple", "SortName": "Apple", "AlbumArtist": "X"},
        {"Id": "a1", "Name": "Cherry", "SortName": "Cherry", "AlbumArtist": "X"},
    ],
    "fb": [
        {"Id": "b0", "Name": "Banana", "SortName": "Banana", "AlbumArtist": "Y"},
        {"Id": "b1", "Name": "Date", "SortName": "Date", "AlbumArtist": "Y"},
    ],
}


def _folder_api(data):
    """A provider stub whose get_items pages ``data[pid]`` honouring
    offset/count, recording each call."""
    api = MagicMock()
    calls = []

    def _get_items(pid, item_type, count, offset, *a, **k):
        calls.append((pid, offset, count))
        rows = data.get(pid, [])
        return {"Items": [dict(r) for r in rows[offset : offset + count]]}

    api.get_items.side_effect = _get_items
    api.calls = calls
    return api


# ── Grid paginator ──────────────────────────────────────────────────────────


@pytest.fixture
def grid(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(lp, "run_async", _inline_run_async)
    g = lg.LibraryGrid("album")
    g.api = _folder_api(_FOLDER_ALBUMS)
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)
    force_sync_render(g)
    return g


def _grid_ids(g):
    return [it["Id"] for it in g._model.items()]


def test_grid_multi_plan_renders_sorted_union(grid, monkeypatch):
    saved = []
    monkeypatch.setattr(_disk_cache, "save", lambda name, scope, payload: saved.append((scope, payload)))
    grid.load_items(["fa", "fb"], "")
    # Global alphabetical interleave — not folder concatenation.
    assert _grid_ids(grid) == ["a0", "b0", "a1", "b1"]
    # Union renders complete: the pagination cascade must not arm.
    assert grid._has_more is False
    # Both folders drained (short first page each → one call per folder).
    assert {c[0] for c in grid.api.calls} == {"fa", "fb"}
    # Cache persisted complete under an order-independent plan key.
    assert saved and saved[-1][0]["parent_id"] == "fa|fb"
    assert saved[-1][1]["complete"] is True


def test_grid_plan_key_is_order_independent(grid, monkeypatch):
    scopes = []
    monkeypatch.setattr(_disk_cache, "save", lambda name, scope, payload: scopes.append(scope))
    grid.load_items(["fb", "fa"], "")
    assert scopes[-1]["parent_id"] == "fa|fb"


def test_grid_single_entry_plan_behaves_like_string(grid, monkeypatch):
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    grid.load_items(["fa"], "")
    assert _grid_ids(grid) == ["a0", "a1"]
    # Classic single path: the provider saw the plain folder id.
    assert grid.api.calls[0][0] == "fa"


def test_grid_multi_cache_hit_renders_then_refreshes_silently(qapp, monkeypatch):
    cached_payload = {
        "items": [dict(r) for r in _FOLDER_ALBUMS["fa"]],
        "complete": True,
    }
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: cached_payload)
    saved = []
    monkeypatch.setattr(_disk_cache, "save", lambda name, scope, payload: saved.append(payload))
    monkeypatch.setattr(lp, "run_async", _inline_run_async)
    g = lg.LibraryGrid("album")
    g.api = _folder_api(_FOLDER_ALBUMS)
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)
    force_sync_render(g)

    g.load_items(["fa", "fb"], "")
    # The (stale, fa-only) cache painted instantly…
    # …and the background union re-fetch detected the diff and saved the
    # fresh union for next launch WITHOUT re-rendering (keep-the-view).
    assert _grid_ids(g) == ["a0", "a1"]
    assert saved and [it["Id"] for it in saved[-1]["items"]] == [
        "a0", "b0", "a1", "b1",
    ]
    assert saved[-1]["complete"] is True


def test_grid_pagination_cascades_refuse_multi_scope(grid, monkeypatch):
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    grid.load_items(["fa", "fb"], "")
    grid.api.calls.clear()
    # Force the preconditions a cascade would normally need, then poke
    # each one — none may fetch against a merged list.
    grid._has_more = True
    grid._loading_more = False
    grid._load_next_page()
    grid._silent_buffered_fill()
    grid._probe_tail_growth()
    grid._silent_rebuild_tick()
    assert grid.api.calls == []


# ── Songs view ──────────────────────────────────────────────────────────────

_FOLDER_SONGS = {
    "fa": [
        {"Id": "s-a0", "Name": "Alpha", "SortName": "Alpha"},
        {"Id": "s-a1", "Name": "Gamma", "SortName": "Gamma"},
    ],
    "fb": [
        {"Id": "s-b0", "Name": "Beta", "SortName": "Beta"},
        {"Id": "s-b1", "Name": "The Delta", "SortName": "The Delta"},
    ],
}


@pytest.fixture
def songs(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(sv_mod, "run_async", _inline_run_async)
    view = SongsView()
    view.api = _folder_api(_FOLDER_SONGS)
    view.api.sorts_songs_server_side = True  # Jellyfin-like
    view._sort_by = "SortName"  # fixture items carry name keys only
    monkeypatch.setattr(view, "_load_visible_covers", lambda *a, **k: None)
    return view


def test_songs_multi_plan_renders_client_sorted_union(songs, monkeypatch):
    saved = []
    monkeypatch.setattr(
        songs, "_save_cache_async", lambda items, complete: saved.append((list(items), complete))
    )
    songs.load_songs(["fa", "fb"])
    # Even on a server-sorting provider, a merged union must be fully
    # client-sorted — article-stripped ("The Delta" under D).
    assert [it["Id"] for it in songs._model.items()] == [
        "s-a0", "s-b0", "s-b1", "s-a1",
    ]
    # Union renders complete: the page cascade stays off.
    assert songs._tail_reached is True
    assert saved and saved[-1][1] is True


def test_songs_multi_refresh_ignores_order_only_diff(songs, monkeypatch):
    saved = []
    monkeypatch.setattr(
        songs, "_save_cache_async", lambda items, complete: saved.append(items)
    )
    songs.load_songs(["fa", "fb"])
    saved.clear()
    # Same ID set, different order → NOT a mutation; must not save or
    # re-render (set-compare kills the endless-reload class structurally).
    reordered = list(reversed([dict(it) for it in songs._model.items()]))
    songs._on_union_refresh(reordered, songs._load_gen)
    assert saved == []


def test_songs_multi_refresh_rerenders_on_real_diff(songs, monkeypatch):
    monkeypatch.setattr(songs, "_save_cache_async", lambda *a, **k: None)
    songs.load_songs(["fa", "fb"])
    fresh = [dict(it) for it in songs._model.items()][:-1]  # one song removed
    songs._on_union_refresh(fresh, songs._load_gen)
    assert len(songs._model.items()) == 3


def test_songs_load_next_page_refuses_multi_scope(songs, monkeypatch):
    songs.load_songs(["fa", "fb"])
    songs.api.calls.clear()
    songs._tail_reached = False  # force the precondition
    songs._page_fetch_in_flight = False
    songs._model.set_items([{"Id": "x"}])  # offset > 0
    songs._load_next_page()
    assert songs.api.calls == []


# ── Suggestions rails ───────────────────────────────────────────────────────


def _rail_api():
    """Provider stub for the rails: per-folder latest + item queries."""
    api = MagicMock()
    api.get_latest_media.side_effect = lambda pid, limit: [
        {"Id": f"{pid}-latest-{i}", "Name": f"L{i}", "DateCreated": f"2026-0{i + 1}-01"}
        for i in range(2)
    ]

    def _get_items(pid, item_type, count, offset, sort_by, *a, **k):
        return {
            "Items": [
                {
                    "Id": f"{pid}-{sort_by.split(',')[0]}-{i}",
                    "Name": f"{pid}{i}",
                    "SortName": f"{pid}{i}",
                    "UserData": {"PlayCount": i},
                }
                for i in range(2)
            ]
        }

    api.get_items.side_effect = _get_items
    return api


@pytest.fixture
def suggestions(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    monkeypatch.setattr(sug_mod, "run_async", _inline_run_async)
    view = SuggestionsView()
    view.api = _rail_api()
    return view


def test_rails_multi_plan_merge_across_folders(suggestions):
    landed = {}
    suggestions._latest_loaded.connect(lambda items: landed.setdefault("latest", items))
    suggestions._favorites_loaded.connect(lambda items: landed.setdefault("favorites", items))
    suggestions.load(["fa", "fb"])
    # Both folders contributed to each rail (2 per folder, rail cap 12).
    assert {it["Id"][:2] for it in landed["latest"]} == {"fa", "fb"}
    assert {it["Id"][:2] for it in landed["favorites"]} == {"fa", "fb"}
    assert len(landed["latest"]) == 4


def test_rails_multi_plan_trims_to_rail_limit(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    monkeypatch.setattr(sug_mod, "run_async", _inline_run_async)
    view = SuggestionsView()
    api = MagicMock()
    api.get_latest_media.side_effect = lambda pid, limit: [
        {"Id": f"{pid}-{i}", "DateCreated": "2026-01-01"} for i in range(RAIL_LIMIT)
    ]
    api.get_items.side_effect = lambda *a, **k: {"Items": []}
    view.api = api
    landed = {}
    view._latest_loaded.connect(lambda items: landed.setdefault("latest", items))
    view.load(["fa", "fb"])
    # 2 × RAIL_LIMIT fetched, exactly RAIL_LIMIT shown.
    assert len(landed["latest"]) == RAIL_LIMIT


def test_rails_single_folder_keeps_server_order(suggestions):
    landed = {}
    suggestions._latest_loaded.connect(lambda items: landed.setdefault("latest", items))
    suggestions.load("fa")
    # Single scope: the server's rail order passes through verbatim.
    assert [it["Id"] for it in landed["latest"]] == ["fa-latest-0", "fa-latest-1"]


def test_rails_dedupe_shared_album_across_folders(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    monkeypatch.setattr(sug_mod, "run_async", _inline_run_async)
    view = SuggestionsView()
    api = MagicMock()
    api.get_latest_media.side_effect = lambda pid, limit: [
        {"Id": "shared", "DateCreated": "2026-01-01"}
    ]
    api.get_items.side_effect = lambda *a, **k: {"Items": []}
    view.api = api
    landed = {}
    view._latest_loaded.connect(lambda items: landed.setdefault("latest", items))
    view.load(["fa", "fb"])
    assert [it["Id"] for it in landed["latest"]] == ["shared"]
