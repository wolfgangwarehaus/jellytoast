"""LibraryGrid double-load guard (the doubled / truncated albums bug).

A same-session DOUBLE ``load_items()`` on one grid — the sign-in path
fires it via both ``_route_home`` and ``_retry_empty_native_views`` —
used to run TWO concurrent auto-pagination cascades sharing one model and
one ``_loaded_count`` offset. They double-appended some pages (the
``[A,A,B,B]`` doubling, made adjacent by the whole-list resort on the
next cache render) and over-advanced the offset past others (the grid
truncating mid-library, e.g. stopping at "Joy'All"). Both faces are the
same race.

The fix is a per-grid load-generation token: every ``load_items`` bumps
it, and every async continuation captures the value live and bails when a
newer load has superseded it, so only one cascade survives. There is also
a hard re-entrancy guard on ``_load_next_page``.

NB: the production suite was green WITH this bug because the test
``run_async`` stub runs inline, which serializes the two loads and hides
the race. These tests drive the async seam by CAPTURING callbacks and
replaying them interleaved — the only way to surface a concurrency bug
deterministically.
"""

from unittest.mock import MagicMock

import pytest

from modules import disk_cache as _disk_cache
from modules import library_grid as lg


def _albums(n, start=0):
    """n albums with distinct, stably-sortable artist/name keys — the
    shape that makes the resort place duplicate twins adjacent."""
    return [
        {
            "Id": f"id{i:04d}",
            "Name": f"Album {i:04d}",
            "SortName": f"Album {i:04d}",
            "AlbumArtist": f"Artist {i:04d}",
        }
        for i in range(start, start + n)
    ]


@pytest.fixture
def grid(qapp, monkeypatch):
    """A cold-loading album grid: disk cache forced to miss, covers
    stubbed so no real image work runs, and a MagicMock provider."""
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    g = lg.LibraryGrid("album")
    g.api = MagicMock()
    # Keep cover machinery inert — we only care about the model contents.
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)
    return g


def _ids(g):
    return [it["Id"] for it in g._model.items()]


def test_superseded_cold_fetch_is_dropped(grid, monkeypatch):
    """Two cold loads in one turn: the FIRST load's result, landing after
    the second load bumped the generation, must be ignored. On the buggy
    code both results render and the model doubles."""
    captured = []

    def _capture(fn, *args, on_result=None, on_error=None, **kwargs):
        captured.append(on_result)

    monkeypatch.setattr(lg, "run_async", _capture)

    grid.load_items("p", "")  # generation 1
    grid.load_items("p", "")  # generation 2 — supersedes 1
    assert len(captured) == 2

    page = _albums(3)
    captured[0]({"Items": list(page)})  # stale (gen 1) → must be dropped
    assert _ids(grid) == [], "a superseded load still rendered"

    captured[1]({"Items": list(page)})  # current (gen 2) → renders
    assert _ids(grid) == [it["Id"] for it in page]


def test_load_next_page_reentrancy_guard(grid, monkeypatch):
    """A second ``_load_next_page`` while one is already in flight must
    not fire another fetch — otherwise both read the same offset and the
    pagination over-advances (skipping pages → truncation)."""
    calls = []
    monkeypatch.setattr(lg, "run_async", lambda *a, **k: calls.append(a))

    grid._loaded_count = 100
    grid._loading_more = True
    grid._load_next_page()
    assert calls == [], "fetched a page while one was already in flight"

    grid._loading_more = False
    grid._load_next_page()
    assert len(calls) == 1


def test_interleaved_double_cold_load_does_not_double_or_truncate(grid, monkeypatch):
    """End-to-end: two cold loads whose async pages + cascade ticks are
    replayed INTERLEAVED (the real race ordering). The model must end up
    with the full library, each album exactly once — no doubling, no
    truncation. Reproduces both symptom faces; fails on the pre-fix code.
    """
    TOTAL = 350  # 3 full pages + a short tail at PAGE_SIZE=100
    server = _albums(TOTAL)

    run_q = []  # queued (fn, args, kwargs, on_result, on_error)
    timer_q = []  # queued cascade callbacks (QTimer.singleShot)

    def _fake_run_async(fn, *args, on_result=None, on_error=None, **kwargs):
        run_q.append((fn, args, kwargs, on_result, on_error))

    def _fake_single_shot(_delay, cb):
        timer_q.append(cb)

    def _fake_get_items(parent, item_type, limit, offset, *a, **k):
        page = server[offset : offset + limit]
        return {"Items": list(page)}

    monkeypatch.setattr(lg, "run_async", _fake_run_async)
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(_fake_single_shot))
    grid.api.get_items.side_effect = _fake_get_items

    grid.load_items("p", "")  # gen 1
    grid.load_items("p", "")  # gen 2

    # Drain both queues to exhaustion, alternating so the two cascades
    # genuinely interleave. Bounded so a regression that loops forever
    # fails loudly instead of hanging.
    for _ in range(2000):
        if run_q:
            fn, args, kwargs, on_result, on_error = run_q.pop(0)
            try:
                r = fn(*args, **kwargs)
            except Exception as e:  # pragma: no cover
                if on_error:
                    on_error(e)
                continue
            if on_result:
                on_result(r)
        elif timer_q:
            timer_q.pop(0)()
        else:
            break
    else:  # pragma: no cover
        pytest.fail("pagination did not terminate — possible runaway cascade")

    ids = _ids(grid)
    assert len(ids) == len(set(ids)), (
        f"model doubled: {len(ids)} rows, {len(set(ids))} unique"
    )
    assert len(ids) == TOTAL, f"model truncated: {len(ids)} of {TOTAL} albums"
    assert set(ids) == {it["Id"] for it in server}, "albums missing from grid"


# ── Silent backfill/rebuild cascades honour the load generation too ─────────
# The original dup-albums fix threaded _load_gen through the cold + auto-
# paginate cascades but NOT the three SILENT background cascades
# (_silent_buffered_fill, _silent_rebuild_tick, _on_refresh_loaded). A scope
# change (sort / library switch) mid-backfill could let a stale-scope page
# land in the buffer and get persisted under the NEW scope's cache key with
# complete=True — a cross-scope-poisoned cache. These pin the generation
# guard on the silent handlers directly (deterministic, no async needed).

def _scope(parent="", sort="SortName"):
    return {
        "kind": "album",
        "parent_id": parent,
        "genre_id": "",
        "year": "",
        "sort_by": sort,
        "sort_order": "Ascending",
        "_item_schema": 3,
    }


def test_silent_buffer_page_drops_superseded_generation(grid):
    grid._load_gen = 5
    grid._refresh_scope = _scope(sort="B")
    grid._partial_cache_buffer = []
    grid._silent_fetch_in_flight = True
    saved = []
    grid._save_cache_async = lambda items, complete: saved.append((list(items), complete))

    # A page from an OLD generation (the sort=A cascade) lands after the
    # sort=B load bumped the gen: it must not touch buffer / gate / cache.
    grid._on_silent_buffer_page({"Items": _albums(3)}, gen=4)
    assert grid._partial_cache_buffer == []
    assert grid._silent_fetch_in_flight is True  # untouched
    assert saved == []

    # A current-generation FULL page accumulates as normal (a short page is
    # treated as the tail and saved+cleared — covered separately).
    grid._on_silent_buffer_page({"Items": _albums(grid.PAGE_SIZE)}, gen=5)
    assert len(grid._partial_cache_buffer) == grid.PAGE_SIZE


def test_stale_silent_tail_does_not_persist_cross_scope(grid):
    # The corruption path: a stale cascade's TAIL (empty page) used to call
    # _save_cache_async(model+buffer, complete=True) under the live (newer)
    # scope, poisoning the new scope's on-disk cache. The gen guard prevents
    # the write entirely.
    grid._load_gen = 9
    grid._refresh_scope = _scope(sort="B")  # the NEW scope
    grid._partial_cache_buffer = _albums(5)  # old-scope residue
    saved = []
    grid._save_cache_async = lambda items, complete: saved.append((list(items), complete))

    grid._on_silent_buffer_page({"Items": []}, gen=8)  # old cascade tail
    assert saved == []  # no cross-scope-poisoned write


def test_silent_rebuild_page_drops_superseded_generation(grid):
    grid._load_gen = 7
    grid._refresh_scope = _scope()
    grid._partial_cache_buffer = []
    saved = []
    grid._save_cache_async = lambda items, complete: saved.append((list(items), complete))

    grid._on_silent_rebuild_page({"Items": _albums(2)}, gen=6)  # stale
    assert grid._partial_cache_buffer == []
    assert saved == []


def test_silent_fill_bails_when_superseded(grid):
    # The scheduler-side guard: a stale-gen fill must not even dispatch a
    # fetch (which would set the in-flight gate against the wrong scope).
    grid._load_gen = 3
    grid._refresh_scope = _scope()
    grid._silent_fetch_in_flight = False

    grid._silent_buffered_fill(gen=2)  # superseded
    assert grid._silent_fetch_in_flight is False  # never armed → no dispatch


def test_load_items_resets_silent_gate(grid, monkeypatch):
    # A superseding load must clear a stale in-flight gate + buffer so the new
    # scope's silent fill isn't wedged behind the prior cascade's flag.
    monkeypatch.setattr(lg, "run_async", lambda *a, **k: None)
    grid._silent_fetch_in_flight = True
    grid._partial_cache_buffer = _albums(3)

    grid.load_items("")  # cold (fixture forces a cache miss)

    assert grid._silent_fetch_in_flight is False
    assert grid._partial_cache_buffer == []
