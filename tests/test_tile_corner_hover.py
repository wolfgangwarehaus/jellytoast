"""Album-tile corner buttons reveal only in their corner zone (2026-06-07).

The heart (bottom-right) and download (bottom-left) corner buttons now paint
only while the cursor is in that corner's zone (``_corner_hover_rect``), not
on a general hover anywhere over the cover. The centred play overlay still
reveals on a plain tile hover — that's gated on ``State_MouseOver`` alone and
is unaffected here. ``_hover_pos`` is the cursor position the view pushes onto
the delegate on every mouse move.
"""

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyle

import jellytoast.library_grid as lg
from jellytoast.library_grid import _corner_hover_rect, _LibraryItemsModel, _TileDelegate

_HOVER = QStyle.StateFlag.State_MouseOver


class _Opt:
    def __init__(self, rect, state):
        self.rect = rect
        self.state = state
        self.widget = None


class _Idx:
    def data(self, role):
        if role == _LibraryItemsModel.ItemRole:
            return {"Name": "Album", "Id": "a1"}
        # CoverRole None → placeholder branch; fav/downloaded False; not
        # downloading (-1) so the BL slot is the hover button, not the ring.
        if role == _LibraryItemsModel.DownloadFractionRole:
            return -1.0
        return None


def _corners_painted(monkeypatch, hover_pos, state=_HOVER):
    """Paint one album tile and return the list of corners ("bl"/"br") the
    delegate drew a corner button for."""
    calls = []
    monkeypatch.setattr(
        lg,
        "_paint_corner_button",
        lambda painter, cover_rect, corner, **k: calls.append(corner),
    )
    d = _TileDelegate("album")
    d._hover_pos = hover_pos
    cell = QRect(0, 0, 200, 240)
    img = QImage(200, 240, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    d.paint(p, _Opt(cell, state), _Idx())
    p.end()
    return d, cell, calls


def _cover(cell):
    return _TileDelegate("album")._cover_rect_for(cell)


def test_no_corners_on_plain_center_hover(qapp, monkeypatch):
    cell = QRect(0, 0, 200, 240)
    center = _cover(cell).center()  # middle of the cover — neither corner
    _, _, calls = _corners_painted(monkeypatch, center)
    assert calls == [], f"corner buttons leaked on a center hover: {calls}"


def test_heart_reveals_only_in_br_zone(qapp, monkeypatch):
    cell = QRect(0, 0, 200, 240)
    pt = _corner_hover_rect(_cover(cell), "br").center()
    _, _, calls = _corners_painted(monkeypatch, pt)
    assert "br" in calls and "bl" not in calls, calls


def test_download_reveals_only_in_bl_zone(qapp, monkeypatch):
    cell = QRect(0, 0, 200, 240)
    pt = _corner_hover_rect(_cover(cell), "bl").center()
    _, _, calls = _corners_painted(monkeypatch, pt)
    assert "bl" in calls and "br" not in calls, calls


def test_no_corners_when_hover_pos_unknown(qapp, monkeypatch):
    _, _, calls = _corners_painted(monkeypatch, None)
    assert calls == []


def test_corner_zones_dont_overlap_center(qapp):
    """The two corner zones must not meet in the middle — there has to be a
    neutral band so a center hover shows the play overlay alone."""
    cover = _cover(QRect(0, 0, 200, 240))
    assert not _corner_hover_rect(cover, "bl").intersects(
        _corner_hover_rect(cover, "br")
    )
