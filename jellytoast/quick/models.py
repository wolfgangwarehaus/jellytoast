"""List models the Qt Quick front end binds to.

Plain ``QAbstractListModel`` subclasses over the shared provider — no
QtWidgets. Fetches run on the shared worker pool (``async_io.run_async``) and
land back on the GUI thread.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    Qt,
    Signal,
    Slot,
)

from jellytoast import async_io

logger = logging.getLogger(__name__)

# Cover size the grid asks the server for (device px). Big enough for a
# two-column phone grid at 3×; the server scales it once and QML caches it.
COVER_PX = 512

_ROOT = QModelIndex()  # the (invalid) root index — a shared default arg


def _provider():
    from jellytoast.providers import get_provider

    return get_provider()


class AlbumsModel(QAbstractListModel):
    """The library's albums, A–Z, loaded a page at a time.

    Roles: ``albumId``, ``name``, ``artist``, ``year``, ``cover`` (an image
    URL the server answers directly — auth rides in the URL)."""

    PAGE = 100

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    ArtistRole = IdRole + 2
    YearRole = IdRole + 3
    CoverRole = IdRole + 4

    loadingChanged = Signal()
    countChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._loading = False
        self._done = False
        self._gen = 0

    # ── QAbstractListModel ──
    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"albumId"),
            self.NameRole: QByteArray(b"name"),
            self.ArtistRole: QByteArray(b"artist"),
            self.YearRole: QByteArray(b"year"),
            self.CoverRole: QByteArray(b"cover"),
        }

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        it = self._items[index.row()]
        if role == self.IdRole:
            return it.get("Id", "")
        if role in (self.NameRole, Qt.ItemDataRole.DisplayRole):
            return it.get("Name", "")
        if role == self.ArtistRole:
            return it.get("AlbumArtist") or ", ".join(
                a.get("Name", "") for a in (it.get("AlbumArtists") or []) if isinstance(a, dict)
            )
        if role == self.YearRole:
            return str(it.get("ProductionYear") or "")
        if role == self.CoverRole:
            return it.get("_cover", "")
        return None

    # ── QML API ──
    def _get_loading(self) -> bool:
        return self._loading

    loading = Property(bool, _get_loading, notify=loadingChanged)

    def _get_count(self) -> int:
        return len(self._items)

    count = Property(int, _get_count, notify=countChanged)

    @Slot()
    def reload(self):
        self._gen += 1
        self.beginResetModel()
        self._items = []
        self.endResetModel()
        self.countChanged.emit()
        self._done = False
        self._loading = False
        self.fetchMore(QModelIndex())

    def canFetchMore(self, parent=_ROOT):  # noqa: N802
        return not parent.isValid() and not self._done and not self._loading

    def fetchMore(self, parent=_ROOT):  # noqa: N802
        if not self.canFetchMore(parent):
            return
        self._loading = True
        self.loadingChanged.emit()
        gen, start = self._gen, len(self._items)
        prov = _provider()

        def _fetch():
            resp = prov.get_items("", "MusicAlbum", self.PAGE, start, "SortName", "Ascending", True)
            items = (resp or {}).get("Items") or []
            for it in items:
                it["_cover"] = prov.get_image_url(it.get("Id", ""), "Primary", COVER_PX)
            return items

        run_async = async_io.run_async  # resolved at call time (tests patch it)
        run_async(
            _fetch,
            on_result=lambda items, g=gen: self._landed(g, items),
            on_error=lambda e, g=gen: self._failed(g, e),
        )

    def _landed(self, gen: int, items: List[Dict]) -> None:
        if gen != self._gen:
            return
        self._loading = False
        if len(items) < self.PAGE:
            self._done = True
        if items:
            first = len(self._items)
            self.beginInsertRows(QModelIndex(), first, first + len(items) - 1)
            self._items.extend(items)
            self.endInsertRows()
            self.countChanged.emit()
        self.loadingChanged.emit()

    def _failed(self, gen: int, e) -> None:
        if gen != self._gen:
            return
        logger.warning("albums page failed: %s", e)
        self._loading = False
        self.loadingChanged.emit()

    def item(self, row: int) -> Dict:
        return self._items[row] if 0 <= row < len(self._items) else {}
