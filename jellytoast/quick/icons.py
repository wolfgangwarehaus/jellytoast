"""The app's SVG icon set for QML: ``image://icon/<name>/<rrggbb>``.

Same glyphs as the Widgets UI (``jellytoast.icons._SVG``), rasterized at the
size QML asks for (``sourceSize``), tinted with the hex colour in the URL."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QPixmap
from PySide6.QtQuick import QQuickImageProvider

from jellytoast import icons as _icons


class IconProvider(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_: str, size: QSize, requested: QSize) -> QPixmap:  # noqa: N802
        name, _, color = id_.partition("/")
        color = "#" + (color.lstrip("#") or "ffffff")
        px = max(requested.width(), requested.height())
        px = px if px > 0 else 48
        pix = _icons._svg_pix_cached(name, color, px, 1.0)
        if size is not None:
            size.setWidth(px)
            size.setHeight(px)
        return pix
