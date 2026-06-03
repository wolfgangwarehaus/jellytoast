"""LibraryGrid stale-'complete'-cache tail-growth probe (bug 2026-06-02).

The Albums grid renders a *complete* disk cache instantly and validates
it with a background refresh of **page 1 only** — `_items_signature` is a
frozenset of the first-page IDs. An album added *past* the first page
(it sorts into the tail) doesn't change page 1, so the refresh saw "no
change" and kept serving the stale cache forever. Symptom: a newly-added
late-alphabet album ("Zara Larsson", "Neutral Milk Hotel") searched +
played but never appeared in the Albums view, while an earlier-added one
showed.

The fix: when page 1 matches AND the cache was complete, probe one page
at `offset = len(cached)`. A complete cache of N rows means the server
holds exactly N, so `offset=N` should be empty; ANY row there proves the
library grew (an insert *anywhere* bumps the total, so `offset=N` yields
a row) → kick the existing silent rebuild so the next launch is fresh.

Harness mirrors test_library_grid_double_load: a MagicMock provider,
inert covers, and the run_async / QTimer.singleShot seams driven by hand.
"""

from unittest.mock import MagicMock

import pytest

from modules import disk_cache as _disk_cache
from modules import library_grid as lg
from modules import library_paginator as lp
from tests.conftest import force_sync_render


def _albums(n, start=0):
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
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    g = lg.LibraryGrid("album")
    g.api = MagicMock()
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)
    # The background prefetch timer (started by _on_items_loaded) would
    # otherwise fire a cover load against the MagicMock provider between
    # tests → QUrl(MagicMock) crash. Keep it inert.
    monkeypatch.setattr(g, "_prefetch_tick", lambda *a, **k: None)
    force_sync_render(g)  # render synchronous by construction — see helper
    return g


def _ids(g):
    return [it["Id"] for it in g._model.items()]


# ── End-to-end: the reported bug ───────────────────────────────────────


def test_tail_addition_behind_complete_cache_triggers_rebuild(grid, monkeypatch):
    """Complete cache of 150 albums; the server now has 151 (a new album
    sorts at the tail, past page 1). On load the page-1 refresh matches,
    so the OLD code stopped there and the 151st album stayed invisible.
    The tail probe must detect the growth and rebuild a complete cache
    that includes it."""
    server = _albums(151)                 # current server state (+1 tail)
    stale = _albums(150)                  # cache written before the add

    monkeypatch.setattr(
        _disk_cache, "load", lambda *a, **k: {"items": list(stale), "complete": True}
    )

    run_q = []
    timer_q = []
    monkeypatch.setattr(
        lp, "run_async",
        lambda fn, *a, on_result=None, on_error=None, **k: run_q.append(
            (fn, a, k, on_result)
        ),
    )
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(lambda _d, cb: timer_q.append(cb)))
    grid.api.get_items.side_effect = lambda parent, item_type, limit, offset, *a, **k: {
        "Items": list(server[offset : offset + limit])
    }
    saved = []
    grid._save_cache_async = lambda items, complete: saved.append((list(items), complete))

    grid.load_items("p", "")

    # Cache rendered immediately; pagination parked (complete).
    assert len(_ids(grid)) == 150
    assert grid._cache_was_complete is True

    # Drain the async + timer seams to exhaustion.
    for _ in range(2000):
        if run_q:
            fn, a, k, on_result = run_q.pop(0)
            r = fn(*a, **k)
            if on_result:
                on_result(r)
        elif timer_q:
            timer_q.pop(0)()
        else:
            break
    else:  # pragma: no cover
        pytest.fail("did not terminate — runaway cascade")

    # A fresh COMPLETE cache was saved, and it includes the new tail album.
    assert saved, "tail growth was not detected — no rebuild fired"
    final_items, complete = saved[-1]
    assert complete is True
    final_ids = {it["Id"] for it in final_items}
    assert "id0150" in final_ids, "rebuilt cache still misses the new album"
    assert len(final_ids) == 151


def test_unchanged_complete_cache_does_not_rebuild(grid, monkeypatch):
    """No additions: the tail probe at offset=N returns empty, so the
    complete cache is left untouched (no needless rebuild churn)."""
    server = _albums(150)
    stale = _albums(150)  # identical — nothing changed

    monkeypatch.setattr(
        _disk_cache, "load", lambda *a, **k: {"items": list(stale), "complete": True}
    )
    run_q = []
    timer_q = []
    monkeypatch.setattr(
        lp, "run_async",
        lambda fn, *a, on_result=None, on_error=None, **k: run_q.append((fn, a, k, on_result)),
    )
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(lambda _d, cb: timer_q.append(cb)))
    grid.api.get_items.side_effect = lambda parent, item_type, limit, offset, *a, **k: {
        "Items": list(server[offset : offset + limit])
    }
    saved = []
    grid._save_cache_async = lambda items, complete: saved.append((list(items), complete))

    grid.load_items("p", "")
    for _ in range(2000):
        if run_q:
            fn, a, k, on_result = run_q.pop(0)
            r = fn(*a, **k)
            if on_result:
                on_result(r)
        elif timer_q:
            timer_q.pop(0)()
        else:
            break

    assert saved == [], "rebuilt the cache even though nothing changed"


# ── Unit: the probe + its result handler ───────────────────────────────


def test_probe_skips_when_library_fits_in_page_one(grid, monkeypatch):
    """A cache smaller than one page is fully covered by the page-1
    signature, so there's no tail to probe — no fetch."""
    calls = []
    monkeypatch.setattr(lp, "run_async", lambda *a, **k: calls.append(a))
    grid.PAGE_SIZE = 100  # the live per-instance value load_items sets
    # Stub the model count (avoids a real set_items → view paint → cover
    # fetch against the MagicMock provider).
    monkeypatch.setattr(grid._model, "items", lambda: _albums(40))  # < PAGE_SIZE
    grid._probe_tail_growth(gen=None)
    assert calls == []


def test_probe_fetches_at_cached_tail_offset(grid, monkeypatch):
    calls = []
    monkeypatch.setattr(lp, "run_async", lambda fn, *a, **k: calls.append(a))
    grid.PAGE_SIZE = 100
    monkeypatch.setattr(grid._model, "items", lambda: _albums(150))
    grid._probe_tail_growth(gen=None)
    assert len(calls) == 1
    # get_items(parent, item_type, limit, offset, ...) — offset is arg 3.
    assert calls[0][3] == 150


def test_probe_drops_superseded_generation(grid, monkeypatch):
    calls = []
    monkeypatch.setattr(lp, "run_async", lambda *a, **k: calls.append(a))
    grid._model.set_items(_albums(150))
    grid._load_gen = 5
    grid._probe_tail_growth(gen=4)  # stale
    assert calls == []


def test_on_tail_probe_empty_does_not_rebuild(grid, monkeypatch):
    shots = []
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(lambda _d, cb: shots.append(cb)))
    grid._load_gen = 1
    grid._on_tail_probe({"Items": []}, gen=1)
    assert shots == []


def test_on_tail_probe_nonempty_kicks_rebuild(grid, monkeypatch):
    shots = []
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(lambda _d, cb: shots.append(cb)))
    grid._load_gen = 1
    grid._on_tail_probe({"Items": _albums(1, start=150)}, gen=1)
    assert len(shots) == 1  # scheduled _silent_rebuild_tick


def test_on_tail_probe_drops_superseded_generation(grid, monkeypatch):
    shots = []
    monkeypatch.setattr(lg.QTimer, "singleShot", staticmethod(lambda _d, cb: shots.append(cb)))
    grid._load_gen = 9
    grid._on_tail_probe({"Items": _albums(1, start=150)}, gen=8)  # stale
    assert shots == []
