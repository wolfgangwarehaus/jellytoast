"""Letter-navigation (A-Z rail) keys off the EFFECTIVE (kind-adjusted) sort.

The shared "Album artist" sort option is ``AlbumArtist,SortName``. Artists and
playlists can't sort by AlbumArtist, so ``_sort_for_kind`` remaps their FETCH
to ``SortName`` — but the alphabet rail used to build its letter->row map off
the RAW sort's field (``AlbumArtist``). Subsonic artist items carry only
``Name`` (no ``AlbumArtist``/``SortName``), so the map came out empty and
clicking a letter on the artists page silently did nothing.

The fix keys the rail (and the client-side article resort) off the effective
sort via ``_effective_sort()``, so artists map on ``Name``. Albums are
unaffected — ``_sort_for_kind`` leaves album sort untouched, so they keep
mapping on ``AlbumArtist``.
"""

from unittest.mock import MagicMock

from modules import disk_cache as _disk_cache
from modules import library_grid as lg
from modules import library_paginator as lp


def _make_grid(kind, sort_by, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    g = lg.LibraryGrid(kind)
    g.api = MagicMock()
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)
    g._sort_by = sort_by
    return g


def _load(g, items, monkeypatch):
    """Drive a cold load: stub run_async to capture the cold-fetch callback,
    fire load_items (sets up PAGE_SIZE/gen/scope), then replay the page."""
    captured = []

    def _capture(fn, *a, on_result=None, on_error=None, **k):
        captured.append(on_result)

    monkeypatch.setattr(lp, "run_async", _capture)
    g.load_items("")  # cold (fixture forces a cache miss) -> one run_async
    assert captured and captured[0] is not None
    captured[0]({"Items": list(items)})  # -> _on_cold_fetch -> _on_items_loaded


def test_artist_letter_map_uses_name_under_album_artist_sort(qapp, monkeypatch):
    """The bug: under the "Album artist" sort the artists grid built an empty
    letter map (keyed on the absent AlbumArtist field) so letter jumps did
    nothing. The map must come off Name. Fails on the pre-fix code."""
    g = _make_grid("artist", "AlbumArtist,SortName", monkeypatch)
    # Subsonic _adapt_artist shape: Name only, no AlbumArtist / SortName.
    artists = [
        {"Id": "a1", "Name": "Adele"},
        {"Id": "b1", "Name": "Beach House"},
        {"Id": "c1", "Name": "Caribou"},
    ]
    _load(g, artists, monkeypatch)

    # The letter map must be populated off Name. Pre-fix it keyed on the
    # absent AlbumArtist field -> empty map -> letter jumps did nothing.
    assert g._letter_to_row.get("A") == 0
    assert g._letter_to_row.get("B") == 1
    assert g._letter_to_row.get("C") == 2


def test_album_letter_map_still_uses_album_artist(qapp, monkeypatch):
    """No regression: albums carry AlbumArtist and _sort_for_kind leaves album
    sort untouched, so the rail keeps mapping on AlbumArtist."""
    g = _make_grid("album", "AlbumArtist,SortName", monkeypatch)
    albums = [
        {"Id": "1", "Name": "21", "AlbumArtist": "Adele"},
        {"Id": "2", "Name": "Bloom", "AlbumArtist": "Beach House"},
    ]
    _load(g, albums, monkeypatch)

    # Albums keep mapping on AlbumArtist (unchanged) — no regression.
    assert g._letter_to_row.get("A") == 0
    assert g._letter_to_row.get("B") == 1
