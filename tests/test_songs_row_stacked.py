"""Phone-width song rows stack title over "artist · album" — the desktop
three-column split elided each column to a few letters at 400 px."""

from __future__ import annotations

from PySide6.QtCore import QRect

from jellytoast.songs_view import _SongRowDelegate


def test_album_column_only_at_desktop_width(qapp):
    d = _SongRowDelegate()
    assert d.album_rect_for(QRect(0, 0, 400, 56)).isEmpty()
    assert not d.album_rect_for(QRect(0, 0, 900, 56)).isEmpty()


def test_stacked_row_paints(qapp):
    from PySide6.QtGui import QImage, QPainter

    d = _SongRowDelegate()
    img = QImage(400, 56, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    item = {"Name": "Dancing Queen", "Artists": ["ABBA"], "Album": "Gold", "RunTimeTicks": 2310000000}
    d._paint_stacked(p, QRect(0, 0, 400, 56), item, 70, 300, "#ffffff", (255, 255, 255))
    p.end()
    # Something was drawn in both the title and the subtitle halves.
    top = any(img.pixelColor(x, y).alpha() for x in range(70, 300) for y in range(8, 28))
    bottom = any(img.pixelColor(x, y).alpha() for x in range(70, 300) for y in range(30, 50))
    assert top and bottom
