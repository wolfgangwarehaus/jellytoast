"""The model's decoded-cover store (_LibraryItemsModel._covers) is a bounded
LRU keyed by paint-access. A long scroll through a large library can't grow it
without limit (it held a full-res QPixmap per album — GBs on a 5k library at
HiDPI), and eviction never drops a cover that's currently visible: data()
bumps a painted tile to the most-recently-used end, so only off-screen covers
fall out. A re-scroll to an evicted row reloads from the persistent disk cache.
"""

from __future__ import annotations

from PySide6.QtGui import QPixmap

from jellytoast import library_grid as lg


def _pix() -> QPixmap:
    p = QPixmap(2, 2)
    p.fill()  # defined, non-null content → passes set_cover's isNull guard
    return p


def test_cover_cache_is_bounded(qapp):
    m = lg._LibraryItemsModel()
    cap = lg._LibraryItemsModel._COVER_CACHE_MAX
    n = cap + 64
    m.set_items([{"Id": f"a{i}"} for i in range(n)])
    pix = _pix()
    for row in range(n):
        m.set_cover(row, pix)
    assert len(m._covers) == cap  # never grows past the cap


def test_eviction_spares_recently_painted_cover(qapp):
    m = lg._LibraryItemsModel()
    cap = lg._LibraryItemsModel._COVER_CACHE_MAX
    m.set_items([{"Id": f"a{i}"} for i in range(cap + 10)])
    pix = _pix()
    for row in range(cap):  # fill exactly to the cap; row 0 is the LRU front
        m.set_cover(row, pix)
    # "Paint" row 0 — data(CoverRole) bumps it to the most-recently-used end.
    m.data(m.index(0, 0), lg._LibraryItemsModel.CoverRole)
    # Ten fresh covers evict the ten LRU rows — now 1..10, NOT the just-painted
    # row 0. A currently-visible tile is never the eviction target.
    for row in range(cap, cap + 10):
        m.set_cover(row, pix)
    assert len(m._covers) == cap
    assert 0 in m._covers  # recently painted → survived
    assert 1 not in m._covers  # LRU front → evicted instead
