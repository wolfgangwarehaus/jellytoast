"""``image://cover/<itemId>/<px>`` — album / artist art for QML through the
app's own image pipeline (``ui_helpers.load_image_async``).

Why not plain URLs: Subsonic signs every request with a fresh salt, so a
cover's URL never repeats and an HTTP cache can't help — every launch
re-downloaded every cover, and nothing showed offline. The pipeline keys
its memory + disk cache by item, not URL, gates the network when offline,
and handles slow servers; this provider just feeds QML from it."""

from __future__ import annotations

import logging
from typing import Set

from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickAsyncImageProvider, QQuickImageResponse, QQuickTextureFactory

logger = logging.getLogger(__name__)

# Responses in flight. Qt owns a response once returned, but the Python
# wrapper must stay alive until it has emitted finished (its slots live
# here); dropped right after.
_PENDING: Set["_CoverResponse"] = set()


def cover_source(item_id: str, px: int) -> str:
    """The QML ``source`` for an item's art at ``px`` (device pixels)."""
    return f"image://cover/{item_id}/{int(px)}" if item_id else ""


class _CoverResponse(QQuickImageResponse):
    def __init__(self, item_id: str, px: int):
        super().__init__()
        self._img = QImage()
        self._error = ""
        _PENDING.add(self)
        from jellytoast.async_io import call_on_gui

        call_on_gui(lambda: self._start(item_id, px))

    def _start(self, item_id: str, px: int) -> None:
        try:
            from jellytoast.providers import get_provider
            from jellytoast.ui_helpers import load_image_async

            url = get_provider().get_image_url(item_id, "Primary", px)
            # `<identity>|<consumer>` — the pipeline shares decoded art
            # across sizes by the part before the first "|".
            load_image_async(
                f"{item_id}|quick",
                url,
                px,
                px,
                self._done,
                on_error=self._failed,
            )
        except Exception as e:
            self._failed(str(e))

    def _done(self, pix) -> None:
        if pix is None or pix.isNull():
            self._failed("empty image")
            return
        self._img = pix.toImage()
        self._finish()

    def _failed(self, err: str = "") -> None:
        # No art (offline and never cached, or the item has none): hand
        # back a transparent pixel instead of an error — the Cover's
        # placeholder shows through, without a QML warning per tile.
        logger.debug("cover unavailable: %s", err)
        self._img = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
        self._img.fill(0)
        self._finish()

    def _finish(self) -> None:
        self.finished.emit()
        _PENDING.discard(self)

    def textureFactory(self):  # noqa: N802 — Qt override
        return QQuickTextureFactory.textureFactoryForImage(self._img)

    def errorString(self) -> str:  # noqa: N802 — Qt override
        return self._error


class CoverProvider(QQuickAsyncImageProvider):
    def requestImageResponse(self, id_: str, requested_size):  # noqa: N802 — Qt override
        item_id, _, px = id_.rpartition("/")
        try:
            size = int(px)
        except ValueError:
            item_id, size = id_, 512
        if requested_size is not None and requested_size.width() > 0:
            size = max(16, min(size, requested_size.width()))
        return _CoverResponse(item_id, size)
