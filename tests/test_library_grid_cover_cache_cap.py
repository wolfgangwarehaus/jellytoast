"""The model's decoded-cover store (_LibraryItemsModel._covers) is a bounded
LRU keyed by paint-access — bounded in BYTES (audit #234 finding 9), so a
HiDPI screen's 4×-heavier pixmaps shrink the resident count proportionally
instead of quadrupling memory. A long scroll through a large library can't
grow it without limit, and eviction never drops a cover that's currently
visible: data() bumps a painted tile to the most-recently-used end, so only
off-screen covers fall out. A re-scroll to an evicted row reloads from the
persistent disk cache.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QPixmap

from jellytoast import library_grid as lg


def _pix(side: int = 100) -> QPixmap:
    p = QPixmap(side, side)
    p.fill()  # defined, non-null content → passes set_cover's isNull guard
    return p


_PIX_COST = 100 * 100 * 4  # _pix_bytes of the default test pixmap


@pytest.fixture
def small_budget(monkeypatch):
    """Shrink the byte budget to exactly 10 test pixmaps so eviction is
    observable without decoding hundreds of covers."""
    monkeypatch.setattr(lg._LibraryItemsModel, "_COVER_CACHE_BUDGET_BYTES", 10 * _PIX_COST)
    return 10


def test_cover_cache_is_byte_bounded(qapp, small_budget):
    m = lg._LibraryItemsModel()
    n = small_budget + 6
    m.set_items([{"Id": f"a{i}"} for i in range(n)])
    pix = _pix()
    for row in range(n):
        m.set_cover(row, pix)
    assert len(m._covers) == small_budget  # never grows past the budget
    assert m._covers_bytes <= m._COVER_CACHE_BUDGET_BYTES


def test_bigger_pixmaps_mean_fewer_residents(qapp, small_budget):
    # The HiDPI point of the byte budget: double the physical pixels per
    # side → 4× the bytes → a quarter the resident covers, same memory.
    m = lg._LibraryItemsModel()
    m.set_items([{"Id": f"a{i}"} for i in range(small_budget)])
    big = _pix(200)  # 4× the bytes of the 100px default
    for row in range(small_budget):
        m.set_cover(row, big)
    assert len(m._covers) == small_budget // 4
    assert m._covers_bytes <= m._COVER_CACHE_BUDGET_BYTES


def test_replacing_a_row_does_not_double_count(qapp, small_budget):
    m = lg._LibraryItemsModel()
    m.set_items([{"Id": "a0"}])
    for _ in range(5):
        m.set_cover(0, _pix())
    assert m._covers_bytes == _PIX_COST  # replaced, not accumulated


def test_eviction_spares_recently_painted_cover(qapp, small_budget):
    m = lg._LibraryItemsModel()
    cap = small_budget
    m.set_items([{"Id": f"a{i}"} for i in range(cap + 3)])
    pix = _pix()
    for row in range(cap):  # fill exactly to budget; row 0 is the LRU front
        m.set_cover(row, pix)
    # "Paint" row 0 — data(CoverRole) bumps it to the most-recently-used end.
    m.data(m.index(0, 0), lg._LibraryItemsModel.CoverRole)
    # Three fresh covers evict the three LRU rows — now 1..3, NOT the
    # just-painted row 0. A currently-visible tile is never the target.
    for row in range(cap, cap + 3):
        m.set_cover(row, pix)
    assert 0 in m._covers  # recently painted → survived
    assert 1 not in m._covers  # LRU front → evicted instead


def test_oversized_single_cover_stays_resident(qapp, monkeypatch):
    # A lone pixmap bigger than the whole budget must not evict itself —
    # the len > 1 guard keeps the just-set cover paintable.
    monkeypatch.setattr(lg._LibraryItemsModel, "_COVER_CACHE_BUDGET_BYTES", 100)
    m = lg._LibraryItemsModel()
    m.set_items([{"Id": "a0"}])
    m.set_cover(0, _pix())
    assert 0 in m._covers
