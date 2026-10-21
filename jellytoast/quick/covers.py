"""Album / artist art for QML through the app's own image pipeline
(``ui_helpers.load_image_async``).

Why not plain URLs: Subsonic signs every request with a fresh salt, so a
cover's URL never repeats and an HTTP cache can't help — every launch
re-downloaded every cover, and nothing showed offline. The pipeline keys
its memory + disk cache by item, gates the network when offline, and
handles slow servers.

Why not an async image provider: those run Python on Qt's image-reader
thread, which needs the GIL — and the GUI thread holds the GIL whenever
Python makes QML create images (a model load landing), while QML in turn
waits on the reader. That deadlocks. So everything Python happens on the
GUI thread: a ``Cover`` asks ``covers.request(key)``, the pipeline loads
it, ``ready(key)`` fires, and the Cover then shows ``image://coverpx/<key>``
— a *synchronous* pixmap provider, which Qt calls on the GUI thread.

Keys are ``<itemId>/<px>``; models hand QML ``image://cover/<key>`` (see
``cover_source``) and the Cover component unwraps it."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Set

from PySide6.QtCore import QObject, QSize, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtQuick import QQuickImageProvider

logger = logging.getLogger(__name__)

_MAX = 400  # decoded covers kept for QML (the pipeline has its own caches too)


def cover_source(item_id: str, px: int) -> str:
    """The QML ``source`` for an item's art at ``px`` (device pixels)."""
    return f"image://cover/{item_id}/{int(px)}" if item_id else ""


class Covers(QObject):
    ready = Signal(str)  # key — its pixmap is now in the cache

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._inflight: Set[str] = set()
        self._missing: Set[str] = set()
        from jellytoast.player_state import PlayerBus

        # Back online: art that failed while offline may load now.
        PlayerBus.get().offline_mode_changed.connect(lambda on: on or self._missing.clear())

    def pixmap(self, key: str):
        pix = self._pix.get(key)
        if pix is not None:
            self._pix.move_to_end(key)
        return pix

    @Slot(str, result=bool)
    def request(self, key: str) -> bool:
        """True if ``key``'s art is ready now; otherwise start loading it
        and answer later through ``ready``."""
        if not key:
            return False
        if key in self._pix:
            self._pix.move_to_end(key)
            return True
        if key in self._inflight or key in self._missing:
            return False
        item_id, _, px = key.rpartition("/")
        try:
            size = int(px)
        except ValueError:
            return False
        self._inflight.add(key)
        try:
            from jellytoast.providers import get_provider
            from jellytoast.ui_helpers import load_image_async

            url = get_provider().get_image_url(item_id, "Primary", size)
            # `<identity>|<consumer>` — the pipeline shares decoded art
            # across sizes by the part before the first "|".
            load_image_async(
                f"{item_id}|quick",
                url,
                size,
                size,
                lambda pix, k=key: self._landed(k, pix),
                on_error=lambda *_a, k=key: self._landed(k, None),
            )
        except Exception as e:
            logger.debug("cover %s failed: %s", key, e)
            self._landed(key, None)
        return False

    def _landed(self, key: str, pix) -> None:
        self._inflight.discard(key)
        if pix is None or pix.isNull():
            # No art (offline and never cached, or none on the server): the
            # Cover keeps its placeholder. Not retried this session.
            self._missing.add(key)
            return
        self._pix[key] = pix
        while len(self._pix) > _MAX:
            self._pix.popitem(last=False)
        self.ready.emit(key)

    @Slot()
    def forgetMissing(self) -> None:  # noqa: N802
        """Retry art that failed (e.g. back online)."""
        self._missing.clear()


class CoverPixmapProvider(QQuickImageProvider):
    """``image://coverpx/<key>`` — serves what ``Covers`` has loaded.
    Pixmap-type providers run synchronously on the GUI thread."""

    def __init__(self, covers: Covers):
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self._covers = covers

    def requestPixmap(self, id_: str, size: QSize, requested: QSize) -> QPixmap:  # noqa: N802
        pix = self._covers.pixmap(id_)
        if pix is None:
            pix = QPixmap(1, 1)
            pix.fill(0)
        if size is not None:
            size.setWidth(pix.width())
            size.setHeight(pix.height())
        return pix
