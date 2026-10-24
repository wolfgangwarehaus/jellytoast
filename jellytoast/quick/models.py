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
from jellytoast.quick.covers import cover_source

logger = logging.getLogger(__name__)

# Cover size the grid asks the server for (device px). Big enough for a
# two-column phone grid at 3×; the server scales it once and QML caches it.
COVER_PX = 512

_ROOT = QModelIndex()  # the (invalid) root index — a shared default arg


def _alive(obj) -> bool:
    """False once QML has destroyed the model (its page was popped or
    replaced) — late async results must then be dropped, not applied."""
    import shiboken6

    return shiboken6.isValid(obj)


def _provider():
    from jellytoast.providers import get_provider

    return get_provider()


def _letter(name: str) -> str:
    """A–Z bucket for a name, the way the server sorts it: leading article
    dropped ("The Antlers" → A), accents folded ("Ásgeir" → A); ``#`` for
    digits, punctuation and non-Latin."""
    from jellytoast.sort_utils import first_letter

    up = first_letter(name or "")
    return up if len(up) == 1 and "A" <= up <= "Z" else "#"


class LibraryModel(QAbstractListModel):
    """One library section, A–Z, loaded a page at a time.

    ``kind``: ``album`` | ``artist`` | ``playlist`` | ``song`` | ``genre`` |
    ``artistAlbums`` (needs ``parentId``) | ``genreAlbums`` (needs
    ``parentId``). Set ``kind`` / ``parentId`` then ``reload()``.

    Roles: ``itemId``, ``name``, ``subtitle``, ``year``, ``cover`` (an image
    URL the server answers directly — auth rides in the URL), ``letter``."""

    PAGE = 100
    KINDS = (
        "album",
        "artist",
        "playlist",
        "song",
        "genre",
        "artistAlbums",
        "genreAlbums",
        "favAlbums",
        "favSongs",
        # Home rails
        "latest",
        "recent",
        "frequent",
        "random",
    )
    RAIL = 20  # albums per Home rail
    _RAILS = {
        "recent": ("DatePlayed,SortName", "Descending", "IsPlayed"),
        "frequent": ("PlayCount,SortName", "Descending", "IsPlayed"),
        "random": ("Random", "Ascending", ""),
    }

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    ArtistRole = IdRole + 2  # role name "subtitle"; "artist" kept for albums
    YearRole = IdRole + 3
    CoverRole = IdRole + 4
    LetterRole = IdRole + 5
    SubtitleRole = IdRole + 6

    loadingChanged = Signal()
    countChanged = Signal()
    kindChanged = Signal()
    letterFound = Signal(str, int)  # letter, row — answer to seekLetter()

    def __init__(self, parent=None, kind: str = "album"):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._loading = False
        self._done = False
        self._gen = 0
        self._kind = kind
        self._parent_id = ""
        self._seek: str = ""
        self._offline = False
        # A first page that failed (server not reachable yet — say the app
        # started before the network was up): retry on a backoff instead of
        # sitting on "Nothing here yet".
        self._error = False
        self._retries = 0
        from PySide6.QtCore import QTimer

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._auto_retry)
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().offline_mode_changed.connect(self._on_offline_mode)
        PlayerBus.get().libraries_changed.connect(self._on_libraries_changed)
        PlayerBus.get().playlists_changed.connect(self._on_playlists_changed)
        self._plan: List[str] = [""]

    # ── QAbstractListModel ──
    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"itemId"),
            self.NameRole: QByteArray(b"name"),
            self.ArtistRole: QByteArray(b"artist"),
            self.SubtitleRole: QByteArray(b"subtitle"),
            self.YearRole: QByteArray(b"year"),
            self.CoverRole: QByteArray(b"cover"),
            self.LetterRole: QByteArray(b"letter"),
        }

    def _subtitle(self, it: Dict) -> str:
        k = self._kind
        if k in ("album", "artistAlbums", "genreAlbums", "favAlbums") or k in self._HOME:
            return it.get("AlbumArtist") or ", ".join(
                a.get("Name", "") for a in (it.get("AlbumArtists") or []) if isinstance(a, dict)
            )
        if k == "artist":
            n = int(it.get("ChildCount") or it.get("AlbumCount") or 0)
            return f"{n} albums" if n != 1 else "1 album" if n else ""
        if k == "playlist":
            n = int(it.get("ChildCount") or 0)
            return f"{n} songs" if n != 1 else "1 song"
        if k in ("song", "favSongs"):
            artists = ", ".join(a for a in (it.get("Artists") or []) if isinstance(a, str))
            return " · ".join(
                x for x in (artists or it.get("AlbumArtist", ""), it.get("Album", "")) if x
            )
        return ""

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        it = self._items[index.row()]
        if role == self.IdRole:
            return it.get("Id", "")
        if role in (self.NameRole, Qt.ItemDataRole.DisplayRole):
            return it.get("Name", "")
        if role in (self.ArtistRole, self.SubtitleRole):
            return self._subtitle(it)
        if role == self.YearRole:
            return str(it.get("ProductionYear") or "")
        if role == self.CoverRole:
            return it.get("_cover", "")
        if role == self.LetterRole:
            return self._letter_key(it)
        return None

    # ── QML API ──
    def _get_loading(self) -> bool:
        return self._loading

    loading = Property(bool, _get_loading, notify=loadingChanged)
    # The list couldn't load (and has nothing to show): offer "Try again".
    error = Property(bool, lambda self: self._error, notify=loadingChanged)

    @Slot()
    def retry(self) -> None:
        self._retries = 0
        self.reload()

    def _auto_retry(self) -> None:
        if _alive(self) and self._error:
            self.reload()

    def _get_count(self) -> int:
        return len(self._items)

    count = Property(int, _get_count, notify=countChanged)

    def _get_kind(self) -> str:
        return self._kind

    def _set_kind(self, k: str) -> None:
        if k != self._kind:
            self._kind = k
            self.kindChanged.emit()

    kind = Property(str, _get_kind, _set_kind, notify=kindChanged)

    def _get_parent_id(self) -> str:
        return self._parent_id

    def _set_parent_id(self, v: str) -> None:
        if v != self._parent_id:
            self._parent_id = v
            self.kindChanged.emit()

    parentId = Property(str, _get_parent_id, _set_parent_id, notify=kindChanged)

    # ── sorting (album grids; shared with the desktop library's choice) ──
    SORTABLE = ("album", "genreAlbums", "favAlbums")

    def _sort(self):
        from jellytoast.settings import get_settings

        st = get_settings()
        if self._kind == "artistAlbums":  # its own choice, sorted on the device
            by = "SortName" if st.artist_album_sort == "name" else "PremiereDate"
            return by, "Descending" if st.artist_album_sort_desc else "Ascending"
        by = st.library_sort_by or "SortName"
        order = "Descending" if (st.library_sort_order or "") == "descending" else "Ascending"
        return by, order

    sortChanged = Signal()

    def _get_sort_by(self) -> str:
        return self._sort()[0]

    def _get_sort_desc(self) -> bool:
        return self._sort()[1] == "Descending"

    sortBy = Property(str, _get_sort_by, notify=sortChanged)
    sortDescending = Property(bool, _get_sort_desc, notify=sortChanged)
    sortable = Property(
        bool,
        lambda self: self._kind in self.SORTABLE or self._kind == "artistAlbums",
        notify=kindChanged,
    )

    def _sort_artist_albums(self, items: List[Dict]) -> List[Dict]:
        """An artist's albums in the chosen order (name ignores a leading
        "The"; same-year albums fall back to name)."""
        from jellytoast.sort_utils import article_stripped_key

        by, order = self._sort()
        desc = order == "Descending"

        def name(it):
            return article_stripped_key(str(it.get("SortName") or it.get("Name") or ""))

        if by == "SortName":
            return sorted(items, key=name, reverse=desc)
        by_name = sorted(items, key=name)
        return sorted(by_name, key=lambda it: int(it.get("ProductionYear") or 0), reverse=desc)

    @Slot(str, bool)
    def setSort(self, sort_by: str, descending: bool) -> None:  # noqa: N802
        """Re-sort (and remember it, like the desktop library's sort menu)."""
        from jellytoast.settings import get_settings

        st = get_settings()
        if self._kind == "artistAlbums":
            # Everything's already here — re-order in place, no refetch.
            st.artist_album_sort = "name" if sort_by.startswith("SortName") else "year"
            st.artist_album_sort_desc = bool(descending)
            self.sortChanged.emit()
            self.beginResetModel()
            self._items = self._sort_artist_albums(self._items)
            self.endResetModel()
            return
        st.library_sort_by = sort_by or "SortName"
        st.library_sort_order = "descending" if descending else "ascending"
        self.sortChanged.emit()
        self.reload()

    def _get_complete(self) -> bool:
        return self._done

    complete = Property(bool, _get_complete, notify=loadingChanged)

    @Slot()
    def reload(self):
        # State first: endResetModel() makes an attached view call
        # fetchMore() synchronously — it must see the fresh state, and we
        # must not start a second fetch for the same page after it.
        self._gen += 1
        self._done = False
        self._loading = False
        self._seek = ""
        from jellytoast.quick import offline_lib

        self._offline = offline_lib.active()
        self._plan = [""]
        if self._kind in self.SCOPED and not self._offline:
            try:
                from jellytoast import library_selection as _ls

                self._plan = _ls.fetch_plan(_provider()) or [""]
            except Exception as e:
                logger.debug("library plan unavailable: %s", e)
        self.beginResetModel()
        self._items = []
        self.endResetModel()
        self.countChanged.emit()
        self.fetchMore(QModelIndex())  # no-op if the view already asked

    def canFetchMore(self, parent=_ROOT):  # noqa: N802
        return not parent.isValid() and not self._done and not self._loading

    def _fetch_page(self, prov, start: int) -> List[Dict]:
        k, pid, n = self._kind, self._parent_id, self.PAGE
        if self._offline:
            from jellytoast.quick import offline_lib

            items = offline_lib.items(k, pid) if start == 0 else []
            return self._sort_artist_albums(items) if k == "artistAlbums" else items
        by, order = self._sort() if k in self.SORTABLE else ("SortName", "Ascending")
        if k in self.SCOPED and len(self._plan) > 1:
            return self._fetch_union(prov, k, pid, by, order) if start == 0 else []
        lib = self._plan[0] if k in self.SCOPED else ""
        if k == "album":
            resp = prov.get_items(lib, "MusicAlbum", n, start, by, order, True)
        elif k == "genreAlbums":
            resp = prov.get_items(lib, "MusicAlbum", n, start, by, order, True, genre_ids=pid)
        elif k == "playlist":
            resp = prov.get_items("", "Playlist", n, start, "SortName", "Ascending", True)
        elif k == "song":
            resp = prov.get_items(lib, "Audio", n, start, "SortName", "Ascending", True)
        elif k == "latest":
            return list(prov.get_latest_media("", self.RAIL) or []) if start == 0 else []
        elif k in self._RAILS:
            sort_by, order, filters = self._RAILS[k]
            resp = prov.get_items(
                "", "MusicAlbum", self.RAIL, start, sort_by, order, True, "", filters
            )
        elif k in ("favAlbums", "favSongs"):
            typ = "MusicAlbum" if k == "favAlbums" else "Audio"
            resp = prov.get_items(lib, typ, n, start, by, order, True, filters="IsFavorite")
        elif k == "artist":
            return list(prov.get_artists(n, start, parent_id=lib) or [])
        elif k == "genre":
            return list(prov.get_genres() or []) if start == 0 else []
        elif k == "artistAlbums":
            items = list(prov.get_artist_albums(pid) or []) if start == 0 else []
            return self._sort_artist_albums(items)
        else:
            raise ValueError(f"unknown library kind {k!r}")
        return (resp or {}).get("Items") or []

    _HOME = ("latest", "recent", "frequent", "random")

    def _fetch_union(self, prov, k, pid, by, order) -> List[Dict]:
        """Several libraries selected: drain each and merge in sort order
        (the desktop grids' ``library_selection.fetch_union``)."""
        from jellytoast import library_selection as _ls

        typ = "Audio" if k in ("song", "favSongs") else "MusicAlbum"
        filters = "IsFavorite" if k in ("favAlbums", "favSongs") else ""
        genre = pid if k == "genreAlbums" else ""

        def page(parent: str, offset: int, count: int) -> List[Dict]:
            if k == "artist":
                return list(prov.get_artists(count, offset, parent_id=parent) or [])
            resp = prov.get_items(
                parent, typ, count, offset, by, order, True, genre_ids=genre, filters=filters
            )
            return (resp or {}).get("Items") or []

        sort_by = "SortName" if k == "artist" else by
        return _ls.fetch_union(
            page,
            self._plan,
            sort_key=_ls.union_sort_key(sort_by),
            reverse=order == "Descending" and k != "artist",
        )

    def _single_shot(self) -> bool:
        if self._kind in self.SCOPED and len(self._plan) > 1:
            return True
        return self._offline or self._kind in ("genre", "artistAlbums") or self._kind in self._HOME

    # Kinds the library picker scopes (the desktop reloads the same grids).
    SCOPED = ("album", "genreAlbums", "favAlbums", "song", "favSongs", "artist")

    def _on_playlists_changed(self) -> None:
        if _alive(self) and self._gen and self._kind == "playlist":
            self.reload()

    def _on_libraries_changed(self) -> None:
        if _alive(self) and self._gen and self._kind in self.SCOPED:
            self.reload()

    def _on_offline_mode(self, on: bool) -> None:
        """Re-load from the new source — but an AUTOMATIC flip (a server
        hiccup) keeps a list that's already showing, like the desktop grid,
        so a flapping connection doesn't blank it."""
        if not _alive(self) or self._gen == 0:
            return
        if on:
            try:
                from jellytoast import offline

                if offline.offline_source() == "auto" and self._items:
                    return
            except Exception:
                pass
        self.reload()

    def fetchMore(self, parent=_ROOT):  # noqa: N802
        if not self.canFetchMore(parent):
            return
        self._loading = True
        self.loadingChanged.emit()
        gen, start = self._gen, len(self._items)
        prov = _provider()
        kind = self._kind

        def _fetch():
            items = self._fetch_page(prov, start)
            for it in items:
                if kind == "genre":
                    continue
                if kind in ("song", "favSongs"):
                    src = it.get("AlbumId") or it.get("Id", "")
                    it["_cover"] = cover_source(src, 160)
                else:
                    it["_cover"] = cover_source(it.get("Id", ""), COVER_PX)
            return items

        run_async = async_io.run_async  # resolved at call time (tests patch it)
        run_async(
            _fetch,
            on_result=lambda items, g=gen: self._landed(g, items),
            on_error=lambda e, g=gen: self._failed(g, e),
        )

    def _landed(self, gen: int, items: List[Dict]) -> None:
        if not _alive(self):
            return
        if gen != self._gen:
            return
        self._loading = False
        self._error = False
        self._retries = 0
        if len(items) < self.PAGE or self._single_shot():
            self._done = True
        if items:
            first = len(self._items)
            self.beginInsertRows(QModelIndex(), first, first + len(items) - 1)
            self._items.extend(items)
            self.endInsertRows()
            self.countChanged.emit()
        self.loadingChanged.emit()
        if self._seek:
            self.seekLetter(self._seek)

    def _failed(self, gen: int, e) -> None:
        if not _alive(self):
            return
        if gen != self._gen:
            return
        logger.warning("%s page failed: %s", self._kind, e)
        self._loading = False
        self._seek = ""
        if not self._items:
            self._error = True
            # 2 s, 4 s, 8 s … then every 60 s while it stays unreachable.
            self._retry_timer.start(min(60_000, 2000 * 2 ** min(self._retries, 5)))
            self._retries += 1
        self.loadingChanged.emit()

    def _letter_key(self, it: Dict) -> str:
        """The A–Z bucket of a row in the current order: the album artist
        when sorted by it, else the (sort) name."""
        if self._kind in self.SORTABLE and self._sort()[0].startswith("AlbumArtist"):
            return _letter(it.get("AlbumArtist") or "")
        return _letter(it.get("SortName") or it.get("Name", ""))

    def _get_scrubbable(self) -> bool:
        """Whether an A–Z rail means something in the current order."""
        if self._kind in self.SORTABLE:
            return self._sort()[0].split(",")[0] in ("SortName", "AlbumArtist")
        return self._kind in ("artist", "song")

    scrubbable = Property(bool, _get_scrubbable, notify=sortChanged)

    @Slot(str)
    def seekLetter(self, letter: str) -> None:  # noqa: N802
        """Find the first row at ``letter`` (A–Z scrubber) — in either sort
        direction — paging in more of the library if it isn't loaded yet.
        Answers through ``letterFound``."""
        order = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        want = order.index(letter) if letter in order else 0
        desc = self._kind in self.SORTABLE and self._sort()[1] == "Descending"
        for row, it in enumerate(self._items):
            have = order.index(self._letter_key(it))
            if (have <= want) if desc else (have >= want):
                self._seek = ""
                self.letterFound.emit(letter, row)
                return
        if self._done:
            self._seek = ""
            self.letterFound.emit(letter, max(0, len(self._items) - 1))
            return
        self._seek = letter
        self.fetchMore()

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> Dict:
        """One row's fields for QML (id, name, subtitle, year, cover)."""
        if not (0 <= row < len(self._items)):
            return {}
        it = self._items[row]
        out = {
            "id": it.get("Id", ""),
            "name": it.get("Name", ""),
            "artist": self._subtitle(it),
            "year": str(it.get("ProductionYear") or ""),
            "cover": it.get("_cover", ""),
            "kind": self._kind,
            "favorite": bool((it.get("UserData") or {}).get("IsFavorite")),
        }
        if self._kind == "playlist":
            # For the menu's Delete row and its confirmation.
            out["canDelete"] = it.get("CanDelete", True) is not False
            out["sourceFile"] = it.get("SourceFile") or ""
        return out

    @Slot(str, bool)
    def markFavorite(self, item_id: str, on: bool) -> None:  # noqa: N802
        """Reflect a favourite toggle in the loaded rows (menus read it)."""
        for it in self._items:
            if it.get("Id") == item_id:
                it.setdefault("UserData", {})["IsFavorite"] = bool(on)

    def item(self, row: int) -> Dict:
        return self._items[row] if 0 <= row < len(self._items) else {}

    def items(self) -> List[Dict]:
        return list(self._items)

    @Slot(int, str)
    def queueRow(self, row: int, mode: str) -> None:  # noqa: N802
        """Songs section: ``next`` / ``end`` one loaded song."""
        if not (0 <= row < len(self._items)):
            return
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        (bus.queue_add_next if mode == "next" else bus.queue_add_end).emit([self._items[row]])

    @Slot(int)
    def playFrom(self, row: int) -> None:  # noqa: N802
        """Songs section: play the loaded songs from ``row``."""
        if not self._items:
            return
        from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

        PlayerBus.get().queue_play_now.emit(
            list(self._items),
            max(0, row),
            QueueContext(kind=QueueKind.MANUAL, source_label="Songs"),
        )


AlbumsModel = LibraryModel  # M0 name


def _fmt_ms(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    return (
        f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


class TracksModel(QAbstractListModel):
    """One album's tracks, for the album page. ``load(albumId)`` fetches;
    ``play(row)`` / ``shuffle()`` hand the whole album to the queue.

    Roles: ``trackId``, ``name``, ``artist`` (only when it differs from the
    album artist), ``number``, ``disc``, ``length`` (``m:ss``)."""

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    ArtistRole = IdRole + 2
    NumberRole = IdRole + 3
    DiscRole = IdRole + 4
    LengthRole = IdRole + 5

    loadingChanged = Signal()
    infoChanged = Signal()
    kindChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._kind = "album"  # | "playlist"
        self._items: List[Dict] = []
        self._album_id = ""
        self._album_name = ""
        self._loading = False
        self._gen = 0
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().playlists_changed.connect(self._on_playlists_changed)

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"trackId"),
            self.NameRole: QByteArray(b"name"),
            self.ArtistRole: QByteArray(b"artist"),
            self.NumberRole: QByteArray(b"number"),
            self.DiscRole: QByteArray(b"disc"),
            self.LengthRole: QByteArray(b"length"),
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
            artists = ", ".join(a for a in (it.get("Artists") or []) if isinstance(a, str))
            if self._kind == "playlist":
                return artists or it.get("AlbumArtist", "")
            return "" if artists == (it.get("AlbumArtist") or "") else artists
        if role == self.NumberRole:
            return int(it.get("IndexNumber") or 0)
        if role == self.DiscRole:
            return int(it.get("ParentIndexNumber") or 0)
        if role == self.LengthRole:
            return _fmt_ms(int(it.get("RunTimeTicks") or 0) // 10_000)
        return None

    def _get_loading(self) -> bool:
        return self._loading

    loading = Property(bool, _get_loading, notify=loadingChanged)

    def _get_summary(self) -> str:
        if not self._items:
            return ""
        ms = sum(int(it.get("RunTimeTicks") or 0) for it in self._items) // 10_000
        n = len(self._items)
        mins = round(ms / 60_000)
        return f"{n} songs · {mins} min" if n != 1 else f"1 song · {mins} min"

    summary = Property(str, _get_summary, notify=infoChanged)

    def _get_kind(self) -> str:
        return self._kind

    def _set_kind(self, k: str) -> None:
        if k != self._kind:
            self._kind = k
            self.kindChanged.emit()

    kind = Property(str, _get_kind, _set_kind, notify=kindChanged)

    @Slot(str, str)
    def load(self, album_id: str, album_name: str = "") -> None:
        self._gen += 1
        gen = self._gen
        self._album_id, self._album_name = album_id, album_name
        self.beginResetModel()
        self._items = []
        self.endResetModel()
        self.infoChanged.emit()
        self._loading = True
        self.loadingChanged.emit()
        from jellytoast.quick import offline_lib

        if offline_lib.active():
            kind = self._kind
            async_io.run_async(
                offline_lib.tracks,
                album_id,
                kind,
                on_result=lambda items, g=gen: self._landed(g, items),
                on_error=lambda e, g=gen: self._landed(g, [], e),
            )
            return
        prov = _provider()
        fetch = prov.get_playlist_items if self._kind == "playlist" else prov.get_album_tracks
        async_io.run_async(
            fetch,
            album_id,
            on_result=lambda items, g=gen: self._landed(g, items),
            on_error=lambda e, g=gen: self._landed(g, [], e),
        )

    def _landed(self, gen: int, items, err=None) -> None:
        if not _alive(self):
            return
        if gen != self._gen:
            return
        if err is not None:
            logger.warning("album %s tracks failed: %s", self._album_id, err)
        self._loading = False
        self.beginResetModel()
        self._items = list(items or [])
        self.endResetModel()
        self.infoChanged.emit()
        self.loadingChanged.emit()

    def _context(self):
        from jellytoast.player_state import QueueContext, QueueKind

        kind = QueueKind.PLAYLIST if self._kind == "playlist" else QueueKind.ALBUM
        return QueueContext(kind=kind, source_id=self._album_id, source_label=self._album_name)

    @Slot(int)
    def play(self, row: int) -> None:
        if not self._items:
            return
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().queue_play_now.emit(list(self._items), max(0, row), self._context())

    album_id = property(lambda self: self._album_id)

    def take_row(self, row: int):
        """Drop a row from the list (playlist removal shows at once);
        returns ``(position, item)`` for the server call, or None."""
        if not (0 <= row < len(self._items)):
            return None
        item = self._items[row]
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        self.endRemoveRows()
        self.infoChanged.emit()
        return (row, item)

    def _on_playlists_changed(self) -> None:
        # Another edit (or the server's answer to ours): show what's there.
        if _alive(self) and self._kind == "playlist" and self._album_id:
            self.load(self._album_id, self._album_name)

    @Slot(int, str)
    def queueRow(self, row: int, mode: str) -> None:  # noqa: N802
        """Long-press / swipe on a row: ``next`` (play next) or ``end``."""
        if not (0 <= row < len(self._items)):
            return
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        (bus.queue_add_next if mode == "next" else bus.queue_add_end).emit([self._items[row]])

    @Slot()
    def shuffle(self) -> None:
        if not self._items:
            return
        import random

        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        bus.shuffle_changed.emit(True)
        bus.queue_play_now.emit(
            list(self._items), random.randrange(len(self._items)), self._context()
        )


class QueueModel(QAbstractListModel):
    """The play queue, in play order, mirrored from ``queue_changed``.
    Roles: ``trackId``, ``name``, ``artist``, ``cover``, ``current``."""

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    ArtistRole = IdRole + 2
    CoverRole = IdRole + 3
    CurrentRole = IdRole + 4

    currentChanged = Signal()

    def __init__(self, parent=None, queue=None):
        super().__init__(parent)
        from jellytoast.player_state import PlayerBus

        self._items: List[Dict] = list(getattr(queue, "queue", []) or [])
        self._current = int(getattr(queue, "current_index", -1) if queue is not None else -1)
        self._covers: Dict[str, str] = {}
        self._bus = PlayerBus.get()
        self._bus.queue_changed.connect(self._on_queue)

    def attach(self, queue) -> None:
        """Pick up the current queue once the core is running."""
        if queue is not None:
            self._on_queue(list(queue.queue or []), int(queue.current_index))

    def _on_queue(self, items, index) -> None:
        self.beginResetModel()
        self._items = list(items or [])
        self._current = int(index)
        self.endResetModel()
        self.currentChanged.emit()

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"trackId"),
            self.NameRole: QByteArray(b"name"),
            self.ArtistRole: QByteArray(b"artist"),
            self.CoverRole: QByteArray(b"cover"),
            self.CurrentRole: QByteArray(b"current"),
        }

    def _cover(self, it: Dict) -> str:
        src = it.get("AlbumId") or it.get("Id", "")
        if not src:
            return ""
        return cover_source(src, 160)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        it = self._items[index.row()]
        if role == self.IdRole:
            return it.get("Id", "")
        if role in (self.NameRole, Qt.ItemDataRole.DisplayRole):
            return it.get("Name", "")
        if role == self.ArtistRole:
            return ", ".join(a for a in (it.get("Artists") or []) if isinstance(a, str)) or it.get(
                "AlbumArtist", ""
            )
        if role == self.CoverRole:
            return self._cover(it)
        if role == self.CurrentRole:
            return index.row() == self._current
        return None

    current = Property(int, lambda self: self._current, notify=currentChanged)

    @Slot(int)
    def jump(self, row: int) -> None:
        self._bus.track_jumped.emit(int(row))

    @Slot(int)
    def remove(self, row: int) -> None:
        self._bus.queue_remove_at.emit(int(row))

    @Slot(int, int)
    def move(self, src: int, dst: int) -> None:
        if src != dst:
            self._bus.queue_move_item.emit(int(src), int(dst))


class LyricsModel(QAbstractListModel):
    """Lyrics for the playing track. ``synced`` when lines carry times;
    ``lineAt(ms)`` gives the line to highlight. Roles: ``line``, ``start``
    (ms)."""

    TextRole = Qt.ItemDataRole.UserRole + 1
    StartRole = TextRole + 1

    stateChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines: List[Dict] = []
        self._item_id = ""
        self._loading = False
        self._gen = 0

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._lines)

    def roleNames(self):  # noqa: N802
        return {self.TextRole: QByteArray(b"line"), self.StartRole: QByteArray(b"start")}

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._lines)):
            return None
        ln = self._lines[index.row()]
        if role in (self.TextRole, Qt.ItemDataRole.DisplayRole):
            return ln.get("Text", "")
        if role == self.StartRole:
            return int(ln.get("Start") or 0) // 10_000
        return None

    synced = Property(
        bool,
        lambda self: any(int(ln.get("Start") or 0) > 0 for ln in self._lines),
        notify=stateChanged,
    )
    loading = Property(bool, lambda self: self._loading, notify=stateChanged)
    itemId = Property(str, lambda self: self._item_id, notify=stateChanged)

    @Slot(int, result=int)
    def lineAt(self, ms: int) -> int:  # noqa: N802
        best = -1
        for i, ln in enumerate(self._lines):
            if int(ln.get("Start") or 0) // 10_000 <= ms:
                best = i
            else:
                break
        return best

    @Slot(str)
    def load(self, item_id: str) -> None:
        if item_id == self._item_id and (self._lines or self._loading):
            return
        self._gen += 1
        gen = self._gen
        self._item_id = item_id
        self.beginResetModel()
        self._lines = []
        self.endResetModel()
        self._loading = bool(item_id)
        self.stateChanged.emit()
        if not item_id:
            return
        prov = _provider()
        async_io.run_async(
            prov.get_lyrics,
            item_id,
            on_result=lambda r, g=gen: self._landed(g, r),
            on_error=lambda e, g=gen: self._landed(g, None),
        )

    def _landed(self, gen: int, result) -> None:
        if not _alive(self):
            return
        if gen != self._gen:
            return
        lines = [ln for ln in ((result or {}).get("Lyrics") or []) if isinstance(ln, dict)]
        self.beginResetModel()
        self._lines = lines
        self.endResetModel()
        self._loading = False
        self.stateChanged.emit()


class SearchModel(QAbstractListModel):
    """Search-as-you-type over artists, albums and songs (one
    ``search_all`` per settled query). Roles: ``section``, ``kind``
    (``artist`` | ``album`` | ``track``), ``itemId``, ``name``, ``subtitle``,
    ``cover``, ``year``."""

    DEBOUNCE_MS = 250
    _SECTIONS = (
        ("MusicArtist", "artist", "Artists"),
        ("MusicAlbum", "album", "Albums"),
        ("Audio", "track", "Songs"),
    )

    SectionRole = Qt.ItemDataRole.UserRole + 1
    KindRole = SectionRole + 1
    IdRole = SectionRole + 2
    NameRole = SectionRole + 3
    SubtitleRole = SectionRole + 4
    CoverRole = SectionRole + 5
    YearRole = SectionRole + 6

    stateChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QTimer

        self._rows: List[Dict] = []
        self._query = ""
        self._loading = False
        self._gen = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self):  # noqa: N802
        return {
            self.SectionRole: QByteArray(b"section"),
            self.KindRole: QByteArray(b"kind"),
            self.IdRole: QByteArray(b"itemId"),
            self.NameRole: QByteArray(b"name"),
            self.SubtitleRole: QByteArray(b"subtitle"),
            self.CoverRole: QByteArray(b"cover"),
            self.YearRole: QByteArray(b"year"),
        }

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        r = self._rows[index.row()]
        key = {
            self.SectionRole: "section",
            self.KindRole: "kind",
            self.IdRole: "id",
            self.NameRole: "name",
            Qt.ItemDataRole.DisplayRole: "name",
            self.SubtitleRole: "subtitle",
            self.CoverRole: "cover",
            self.YearRole: "year",
        }.get(role)
        return r.get(key, "") if key else None

    def _get_query(self) -> str:
        return self._query

    def _set_query(self, q: str) -> None:
        if q == self._query:
            return
        self._query = q
        self.stateChanged.emit()
        if q.strip():
            self._timer.start()
        else:
            self._timer.stop()
            self._gen += 1
            self._set_rows([])

    query = Property(str, _get_query, _set_query, notify=stateChanged)
    loading = Property(bool, lambda self: self._loading, notify=stateChanged)
    count = Property(int, lambda self: len(self._rows), notify=stateChanged)

    def _set_rows(self, rows: List[Dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()
        self._loading = False
        self.stateChanged.emit()

    def _run(self) -> None:
        self._gen += 1
        gen, term = self._gen, self._query.strip()
        self._loading = True
        self.stateChanged.emit()
        prov = _provider()

        def _fetch():
            from jellytoast.quick import offline_lib

            if offline_lib.active():
                found = offline_lib.search(term)
            else:
                found = prov.search_all(term, songs=20, albums=14, artists=10) or {}
            rows: List[Dict] = []
            for typ, kind, label in self._SECTIONS:
                for it in found.get(typ) or []:
                    if kind == "track":
                        src = it.get("AlbumId") or it.get("Id", "")
                        artists = ", ".join(
                            a for a in (it.get("Artists") or []) if isinstance(a, str)
                        )
                        sub = " · ".join(
                            x
                            for x in (artists or it.get("AlbumArtist", ""), it.get("Album", ""))
                            if x
                        )
                        px = 132
                    elif kind == "album":
                        src, sub, px = it.get("Id", ""), it.get("AlbumArtist", ""), 256
                    else:
                        src, sub, px = it.get("Id", ""), "", 256
                    rows.append(
                        {
                            "section": label,
                            "kind": kind,
                            "id": it.get("Id", ""),
                            "name": it.get("Name", ""),
                            "subtitle": sub,
                            "year": str(it.get("ProductionYear") or ""),
                            "cover": cover_source(src, px),
                            "_raw": it,
                        }
                    )
            return rows

        async_io.run_async(
            _fetch,
            on_result=lambda rows, g=gen: _alive(self) and g == self._gen and self._set_rows(rows),
            on_error=lambda e, g=gen: (
                _alive(self)
                and g == self._gen
                and (logger.warning("search %r failed: %s", term, e) or self._set_rows([]))
            ),
        )

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> Dict:
        if not (0 <= row < len(self._rows)):
            return {}
        return {k: v for k, v in self._rows[row].items() if not k.startswith("_")}

    @Slot(int)
    def play(self, row: int) -> None:
        """A song result: play it (just that song)."""
        if not (0 <= row < len(self._rows)) or self._rows[row]["kind"] != "track":
            return
        from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

        PlayerBus.get().queue_play_now.emit(
            [self._rows[row]["_raw"]],
            0,
            QueueContext(kind=QueueKind.SEARCH, source_label=self._query),
        )

    @Slot(int, str)
    def queueRow(self, row: int, mode: str) -> None:  # noqa: N802
        if not (0 <= row < len(self._rows)) or self._rows[row]["kind"] != "track":
            return
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        (bus.queue_add_next if mode == "next" else bus.queue_add_end).emit(
            [self._rows[row]["_raw"]]
        )


def _fmt_bytes(n: int) -> str:
    n = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class DownloadsModel(QAbstractListModel):
    """What the user asked to keep offline, newest first, with live state.
    Roles: ``itemId``, ``name``, ``subtitle``, ``kind`` (album / playlist /
    artist / track), ``state`` (pending / downloading / complete / failed /
    stale), ``progress`` (0–1), ``size``, ``cover``."""

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    SubtitleRole = IdRole + 2
    KindRole = IdRole + 3
    StateRole = IdRole + 4
    ProgressRole = IdRole + 5
    SizeRole = IdRole + 6
    CoverRole = IdRole + 7

    statsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from jellytoast.player_state import PlayerBus

        self._rows: List[Dict] = []
        self._progress: Dict[str, float] = {}
        self._paused = False
        bus = PlayerBus.get()
        bus.download_progress.connect(self._on_progress)
        bus.download_queue_paused.connect(lambda: self._set_paused(True))
        bus.download_queue_resumed.connect(lambda: self._set_paused(False))

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"itemId"),
            self.NameRole: QByteArray(b"name"),
            self.SubtitleRole: QByteArray(b"subtitle"),
            self.KindRole: QByteArray(b"kind"),
            self.StateRole: QByteArray(b"state"),
            self.ProgressRole: QByteArray(b"progress"),
            self.SizeRole: QByteArray(b"size"),
            self.CoverRole: QByteArray(b"cover"),
        }

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        r = self._rows[index.row()]
        item_id = r.get("item_id", "")
        meta = r.get("metadata") or {}
        if role == self.IdRole:
            return item_id
        if role in (self.NameRole, Qt.ItemDataRole.DisplayRole):
            return r.get("name", "")
        if role == self.SubtitleRole:
            return meta.get("AlbumArtist") or ", ".join(
                a for a in (meta.get("Artists") or []) if isinstance(a, str)
            )
        if role == self.KindRole:
            return str(r.get("kind") or "")
        if role == self.StateRole:
            return str(r.get("state") or "")
        if role == self.ProgressRole:
            state = str(r.get("state") or "")
            return 1.0 if state == "complete" else float(self._progress.get(item_id, 0.0))
        if role == self.SizeRole:
            return r.get("_size", "")
        if role == self.CoverRole:
            return r.get("_cover", "")
        return None

    def _set_paused(self, on: bool) -> None:
        self._paused = on
        self.statsChanged.emit()

    paused = Property(bool, lambda self: self._paused, notify=statsChanged)
    count = Property(int, lambda self: len(self._rows), notify=statsChanged)

    def _usage(self) -> str:
        try:
            from jellytoast import offline

            return _fmt_bytes(offline.storage_usage().get("total", 0))
        except Exception:
            return ""

    storageUsed = Property(str, _usage, notify=statsChanged)

    @Slot()
    def reload(self) -> None:
        from jellytoast import offline

        try:
            rows = offline.list_downloads()
            self._paused = offline.is_paused()
        except Exception as e:
            logger.warning("downloads list failed: %s", e)
            rows = []
        try:
            _provider()
        except Exception:
            pass
        for r in rows:
            item_id = r.get("item_id", "")
            meta = r.get("metadata") or {}
            try:
                r["_size"] = _fmt_bytes(offline.item_size(item_id))
            except Exception:
                r["_size"] = ""
            src = meta.get("AlbumId") if r.get("kind") == "track" else item_id
            try:
                r["_cover"] = cover_source(src, 160)
            except Exception:
                r["_cover"] = ""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()
        self.statsChanged.emit()

    def _on_progress(self, item_id: str, state: str, fraction: float) -> None:
        if not _alive(self):
            return
        self._progress[item_id] = float(fraction)
        for i, r in enumerate(self._rows):
            if r.get("item_id") == item_id:
                r["state"] = str(state)
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.StateRole, self.ProgressRole])
                if str(state) == "complete":
                    self.statsChanged.emit()
                return
        if str(state) == "pending":  # a new request: show it
            self.reload()

    @Slot(int)
    def remove(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        from jellytoast import offline

        offline.remove(self._rows[row].get("item_id", ""))
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()
        self.statsChanged.emit()

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> Dict:
        if not (0 <= row < len(self._rows)):
            return {}
        r = self._rows[row]
        meta = r.get("metadata") or {}
        return {
            "kind": str(r.get("kind") or ""),
            "id": r.get("item_id", ""),
            "name": r.get("name", ""),
            "artist": meta.get("AlbumArtist", ""),
            "year": str(meta.get("ProductionYear") or ""),
            "cover": r.get("_cover", ""),
        }

    @Slot()
    def togglePause(self) -> None:  # noqa: N802
        from jellytoast import offline

        (offline.resume if self._paused else offline.pause)()


class RadioModel(QAbstractListModel):
    """Your internet radio stations (the server's list). Roles: ``itemId``,
    ``name``, ``subtitle`` (homepage host), ``cover`` (logo URL), ``streamUrl``.
    ``play(row)`` installs the same single-item INTERNET_RADIO queue as the
    desktop Radio view; add / remove go through the provider (on Navidrome,
    admin-only — the error comes back as ``failed``)."""

    IdRole = Qt.ItemDataRole.UserRole + 1
    NameRole = IdRole + 1
    SubtitleRole = IdRole + 2
    CoverRole = IdRole + 3
    StreamRole = IdRole + 4

    stateChanged = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[Dict] = []
        self._loading = False

    def rowCount(self, parent=_ROOT):  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self):  # noqa: N802
        return {
            self.IdRole: QByteArray(b"itemId"),
            self.NameRole: QByteArray(b"name"),
            self.SubtitleRole: QByteArray(b"subtitle"),
            self.CoverRole: QByteArray(b"cover"),
            self.StreamRole: QByteArray(b"streamUrl"),
        }

    @staticmethod
    def _logo(st: Dict) -> str:
        from jellytoast.radio_presets import logo_url_for_stream

        return logo_url_for_stream(st.get("streamUrl") or "") or str(st.get("logoUrl") or "")

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        st = self._rows[index.row()]
        if role == self.IdRole:
            return str(st.get("id") or "")
        if role in (self.NameRole, Qt.ItemDataRole.DisplayRole):
            return str(st.get("name") or "")
        if role == self.SubtitleRole:
            from urllib.parse import urlparse

            home = str(st.get("homePageUrl") or "")
            return urlparse(home).netloc or urlparse(str(st.get("streamUrl") or "")).netloc
        if role == self.CoverRole:
            return self._logo(st)
        if role == self.StreamRole:
            return str(st.get("streamUrl") or "")
        return None

    loading = Property(bool, lambda self: self._loading, notify=stateChanged)
    count = Property(int, lambda self: len(self._rows), notify=stateChanged)

    def _set_rows(self, rows) -> None:
        if not _alive(self):
            return
        self.beginResetModel()
        self._rows = [r for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()
        self._loading = False
        self.stateChanged.emit()

    @Slot()
    def reload(self) -> None:
        self._loading = True
        self.stateChanged.emit()
        prov = _provider()

        def _fail(e):
            logger.warning("radio stations failed: %s", e)
            self._set_rows([])

        async_io.run_async(
            prov.get_internet_radio_stations, on_result=self._set_rows, on_error=_fail
        )

    @Slot(int)
    def play(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        play_station(self._rows[row])

    @Slot(str, str, str)
    def add(self, name: str, stream_url: str, home_url: str = "") -> None:
        name, stream_url = (name or "").strip(), (stream_url or "").strip()
        if not name or not stream_url:
            return
        prov = _provider()
        async_io.run_async(
            prov.create_internet_radio_station,
            name,
            stream_url,
            home_url or None,
            on_result=lambda _st: self.reload(),
            on_error=lambda e: self.failed.emit(_radio_error(e, "add")),
        )

    @Slot(int)
    def remove(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        sid = str(self._rows[row].get("id") or "")
        prov = _provider()
        async_io.run_async(
            prov.delete_internet_radio_station,
            sid,
            on_result=lambda _r: self.reload(),
            on_error=lambda e: self.failed.emit(_radio_error(e, "remove")),
        )

    @Slot(result="QVariantList")
    def popular(self) -> List[Dict]:
        """The curated stations (radio_presets), for Browse popular."""
        from jellytoast.radio_presets import POPULAR_STATIONS

        have = {str(r.get("streamUrl") or "") for r in self._rows}
        return [
            {
                "name": p["name"],
                "streamUrl": p["streamUrl"],
                "homePageUrl": p.get("homePageUrl", ""),
                "logo": p.get("logoUrl", ""),
                "description": p.get("description", ""),
                "category": p.get("category", ""),
                "added": p["streamUrl"] in have,
            }
            for p in POPULAR_STATIONS
        ]


def _radio_error(e, verb: str) -> str:
    from PySide6.QtCore import QCoreApplication

    if isinstance(e, NotImplementedError):
        return QCoreApplication.translate(
            "RadioModel", "This server doesn't support radio stations."
        )
    msg = str(e)
    if "50" in msg or "authoriz" in msg.lower() or "permission" in msg.lower():
        return QCoreApplication.translate(
            "RadioModel", "Only a server admin can change radio stations."
        )
    if verb == "add":
        return QCoreApplication.translate("RadioModel", "Couldn't add the station: {0}").format(msg)
    return QCoreApplication.translate("RadioModel", "Couldn't remove the station: {0}").format(msg)


def play_station(st: Dict) -> None:
    """Play an internet radio station — the desktop Radio view's queue
    shape: one item with the stream inline, INTERNET_RADIO context."""
    from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

    stream = str(st.get("streamUrl") or "")
    if not stream:
        return
    sid = str(st.get("id") or "")
    name = str(st.get("name") or "Radio")
    item = {"Id": sid, "Name": name, "Type": "Audio", "streamUrl": stream, "RunTimeTicks": 0}
    ctx = QueueContext(
        kind=QueueKind.INTERNET_RADIO,
        source_id=sid,
        source_label=name,
        source_icon=RadioModel._logo(st),
    )
    PlayerBus.get().queue_play_now.emit([item], 0, ctx)
