"""
Native Songs view — Phase 4 of the native-UI pivot.

Replaces JF Web's Music → Songs tab with a PySide6 list. Each row:
small cover thumb + title + artist + album + duration. Click any row
to install the visible song list as the live queue and start playing
from that index.

Why a list, not a grid: songs are far denser than albums (a typical
library is hundreds-to-thousands of tracks) and the per-track
metadata (artist + album + duration) reads more comfortably across
a row than stacked under a tile.

Sort is reused from the shared library_sort_by Settings, with
per-kind fallback (artist/release-date sorts that apply to albums
fall back to SortName for songs since the fields aren't always
present on Audio items).
"""

from typing import Dict, List

from PySide6.QtCore import Qt, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QScrollArea,
    QSizePolicy,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.providers import get_provider
from modules.sort_utils import article_stripped_key
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars, fmt_duration_ticks,
    install_song_context_menu,
    ACCENT, TEXT, TEXT_DIM, TEXT_FAINT,
)
from modules.design_tokens import (
    TYPE_BODY, TYPE_CAPTION, type_qss,
    SPACE_MD, SPACE_LG, SPACE_XL,
)


class _ElidingLabel(QLabel):
    """QLabel that elides overflow with `…` (mirrors the helpers in
    library_grid / now_playing_page; tiny enough to keep duplicated)."""
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        super().setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str):
        self._full = text or ""
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        avail = max(0, self.width() - 4)
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return self.minimumSizeHint()


# ── Row ─────────────────────────────────────────────────────────────────

class _ClickableElidingLabel(_ElidingLabel):
    """Eliding label that emits `clicked` on left-click and consumes
    the event so it doesn't bubble to the parent row's play handler."""
    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)


class _SongRow(QFrame):
    """Single song row. Cover thumb + title + artist + album + duration.
    Click → emit play_requested(index) so the parent can install the
    visible list as the live queue starting at this row. Clicking the
    album label specifically emits album_browse_requested(album_id)
    instead, providing an escape hatch from the play-on-click default."""

    play_requested = Signal(int)
    album_browse_requested = Signal(str)  # album_id

    THUMB_SIZE = 44
    ROW_HEIGHT = 56

    def __init__(self, index: int, item: Dict, parent=None):
        super().__init__(parent)
        self._index = index
        self._item = item
        self._is_current = False
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # StrongFocus enables Tab nav + programmatic setFocus from
        # SearchView's "down arrow → first result" flow. The :focus
        # stylesheet matches the hover backdrop so the focused row is
        # easy to spot.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName("songRow")
        self.setStyleSheet("""
            QFrame#songRow {
                background: transparent; border: none; border-radius: 6px;
            }
            QFrame#songRow:hover { background: rgba(255, 255, 255, 0.04); }
            QFrame#songRow:focus { background: rgba(255, 255, 255, 0.08); }
            QFrame#songRow QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, 4, SPACE_MD, 4)
        layout.setSpacing(SPACE_MD)

        # Cover thumb — square, fills row height.
        self._thumb = QLabel()
        self._thumb.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background: rgba(255,255,255,0.04); border-radius: 4px;"
        )
        layout.addWidget(self._thumb)

        # Title — bold body, single line, eliding.
        self._title = _ElidingLabel(item.get("Name", "Unknown"))
        self._title.setStyleSheet(self._title_css(active=False))
        layout.addWidget(self._title, 3)

        # Artist — dim caption.
        artists = item.get("Artists") or []
        artist = ", ".join(artists) if artists else (item.get("AlbumArtist", "") or "")
        self._artist = _ElidingLabel(artist)
        self._artist.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        layout.addWidget(self._artist, 2)

        # Album — fainter caption. Clickable when AlbumId is known so
        # users can jump from a song to the album page; falls back to a
        # plain label when no id is present (some legacy / orphaned
        # tracks have an album name string but no resolvable id).
        album_id = item.get("AlbumId", "") or ""
        if album_id:
            self._album = _ClickableElidingLabel(item.get("Album", "") or "")
            self._album.clicked.connect(
                lambda aid=album_id: self.album_browse_requested.emit(aid)
            )
        else:
            self._album = _ElidingLabel(item.get("Album", "") or "")
        self._album.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        layout.addWidget(self._album, 2)

        # Duration — fixed-width, monospace, right-aligned.
        ticks = item.get("RunTimeTicks", 0) or 0
        self._duration = QLabel(fmt_duration_ticks(ticks) if ticks else "")
        self._duration.setFixedWidth(56)
        self._duration.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._duration.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )
        layout.addWidget(self._duration)

        # Right-click → Play next / Add to queue. Same handler powers
        # SearchView's song bucket (it reuses _SongRow) so search
        # results inherit the menu for free.
        install_song_context_menu(self, lambda: self._item)

    @staticmethod
    def _title_css(active: bool) -> str:
        if active:
            return f"color: {ACCENT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        return f"color: {TEXT}; {type_qss(TYPE_BODY)}"

    def set_thumb(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        scaled = pix.scaled(
            self.THUMB_SIZE, self.THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.size() != QSize(self.THUMB_SIZE, self.THUMB_SIZE):
            x = max(0, (scaled.width() - self.THUMB_SIZE) // 2)
            y = max(0, (scaled.height() - self.THUMB_SIZE) // 2)
            scaled = scaled.copy(x, y, self.THUMB_SIZE, self.THUMB_SIZE)
        self._thumb.setPixmap(scaled)

    def set_current(self, is_current: bool):
        if is_current == self._is_current:
            return
        self._is_current = is_current
        self._title.setStyleSheet(self._title_css(active=is_current))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._index)
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        # Enter on a focused row = play, mirroring the click handler.
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.play_requested.emit(self._index)
            return
        super().keyPressEvent(e)


# ── List view ───────────────────────────────────────────────────────────

class SongsView(QWidget):
    """Vertical list of all songs in the music library. Click a row →
    install the visible list as the live queue starting at that
    index. Sort is honored from the shared library_sort_by setting,
    with per-kind sanitization since some album-level sort fields
    don't apply to Audio items."""

    play_requested = Signal(int, list)  # start_idx, item_list
    album_browse_requested = Signal(str)  # album_id

    _items_loaded = Signal(object)
    _refresh_loaded = Signal(object)

    HEADER_LABEL = "SONGS"
    ITEM_TYPE = "Audio"
    CACHE_NAME = "songs"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._rows: List[_SongRow] = []
        self._items: List[Dict] = []
        self._parent_id: str = ""

        # Default to album-chronological clustering — alphabetical-
        # by-song-title is rarely useful for a multi-thousand-song
        # flat list. The user can still pick a different sort via
        # the top-bar dropdown; set_sort() honors that selection for
        # the session. We deliberately *don't* read library_sort_by
        # here so the dropdown's "Name" default (which makes sense
        # for albums) doesn't propagate to a useless ordering for
        # songs. _safe_sort extends "AlbumArtist" into the full
        # cluster: artist → release year → album → disc → track.
        self._sort_by = "AlbumArtist"
        from modules.settings import get_settings
        s = get_settings()
        self._sort_order = (
            "Descending" if s.library_sort_order == "descending" else "Ascending"
        )

        self.setObjectName("songsView")
        # Sweep transparency across every descendant — the application-
        # level QSS paints all QWidgets with the body color by default;
        # the descendant selector overrides it for everything inside
        # so the scroll bar lane (and any other interior chrome) lets
        # the body show through.
        self.setStyleSheet("""
            QWidget#songsView,
            QWidget#songsView QWidget,
            QWidget#songsView QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        # Status placeholder — used for "Connecting…" while waiting on
        # the credential bridge, "Loading songs…" while the REST fetch
        # is in flight, and "No songs in this library." when the
        # response is genuinely empty. Visible by default so cold
        # opens don't flash an empty list before the first chunk
        # lands.
        self._loading_label = QLabel("Connecting…")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)} "
            f"padding: {SPACE_XL * 2}px {SPACE_XL}px;"
        )
        outer.addWidget(self._loading_label)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Vertical bar lane always reserved so the eliding row labels
        # don't all re-compute their elision the moment the scroll bar
        # appears mid-render — that re-elide pass was the source of
        # the visible "wobble" in long lists. The pill itself is
        # invisible at rest thanks to AutoFadeScrollBar.
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        install_autofade_scrollbars(self._scroll)
        self._container = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setContentsMargins(
            SPACE_LG, 0, SPACE_LG, SPACE_LG,
        )
        self._list_layout.setSpacing(0)
        # Top-align so rows pack flush from the top and Qt doesn't try
        # to vertically distribute extra space across them when the
        # layout's preferred size is shorter than the viewport. The
        # trailing stretch was previously serving the same purpose,
        # but it caused rows to shift their vertical positions as
        # chunks landed (the stretch's zero point flipped between
        # "fits" and "overflows" mid-render). AlignTop is stable.
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        # Chunked-rendering bookkeeping. Inserting all rows synchronously
        # blocks the GUI for ~1-2s on a multi-thousand-item library, so
        # we batch via QTimer.singleShot — each tick inserts CHUNK_SIZE
        # rows with the container's updates suppressed (no per-row
        # repaint) and then yields back to the event loop. Bigger
        # chunks + longer intervals are paradoxically smoother because
        # there's less per-tick overhead and less time spent on partial
        # paints during the burst.
        self._pending_items: List[Dict] = []
        self._pending_idx: int = 0
        self.CHUNK_SIZE = 200
        self.CHUNK_INTERVAL_MS = 50

        # Lazy cover-load bookkeeping. Loading 2000 covers up front
        # serializes through QNAM's 6-per-host connection cap and
        # delays the perceptual completion of the list. Covers only
        # fetch when their row enters (or is near) the viewport;
        # _covers_loaded tracks which row indices have already been
        # asked.
        self._covers_loaded: set = set()
        self._scroll.verticalScrollBar().valueChanged.connect(
            self._load_visible_covers
        )

        self._items_loaded.connect(self._on_items_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)
        # Scope tracked across the cache hit → background refresh
        # round-trip so the refresh callback knows what scope to
        # validate against and persist to.
        self._refresh_scope: dict = {}

    # ── Public API ─────────────────────────────────────────────────────

    def load_songs(self, parent_id: str = ""):
        """Render songs under `parent_id`. Two-phase strategy: if a
        disk cache matches the current scope (parent + sort), render
        from it instantly and kick off a background refresh that
        only re-renders if the items actually changed. On a true
        cold load (no cache, or scope changed) we show the loading
        placeholder and wait for the server.

        Limit 2000 — tracks are denser than albums so we cap higher;
        pagination on the scroll is a follow-up."""
        self._parent_id = parent_id
        sort_by = self._safe_sort(self._sort_by)
        scope = {
            "parent_id": parent_id,
            "sort_by": sort_by,
            "sort_order": self._sort_order,
        }
        cached = disk_cache.load(self.CACHE_NAME, scope)
        self._refresh_scope = scope
        if cached:
            # Render the cached payload immediately, then verify
            # against the server in the background.
            self._items_loaded.emit({"Items": cached})
            run_async(
                self.api.get_items, parent_id, self.ITEM_TYPE, 2000, 0,
                sort_by, self._sort_order, True,  # recursive
                on_result=lambda resp: self._refresh_loaded.emit(resp),
                on_error=lambda _e: None,
            )
            return
        self._clear_rows()
        self._loading_label.setText("Loading songs…")
        self._loading_label.setVisible(True)
        run_async(
            self.api.get_items, parent_id, self.ITEM_TYPE, 2000, 0,
            sort_by, self._sort_order, True,  # recursive
            on_result=lambda resp: self._on_cold_fetch(resp),
            on_error=lambda _e: self._items_loaded.emit({"Items": []}),
        )

    def _on_cold_fetch(self, resp):
        """First-fetch handler when there was no cache — persist to
        disk so subsequent launches render instantly, then drive the
        normal render path."""
        items = (resp or {}).get("Items") or []
        if items and self._refresh_scope:
            disk_cache.save(self.CACHE_NAME, self._refresh_scope, items)
        self._items_loaded.emit(resp)

    def show_connecting(self):
        """Host calls this when the songs view exists but its first
        load is gated on the credential bridge. Surfaces the wait
        instead of leaving the list silently empty."""
        self._loading_label.setText("Connecting…")
        self._loading_label.setVisible(True)

    def set_sort(self, sort_by: str, sort_order: str):
        self._sort_by = sort_by or "SortName"
        self._sort_order = (
            "Descending" if sort_order == "descending" else "Ascending"
        )
        self.load_songs(self._parent_id)

    def _resort_items_by_article(self, items: "List[Dict]") -> "List[Dict]":
        """Client-side re-sort that ignores leading articles in name
        fields. The server's AlbumArtist sort puts 'The Antlers'
        under T; this re-sorts so they cluster as 'Antlers'. The
        secondary keys (ProductionYear / Album / disc / track) keep
        the rest of the iTunes-style album-chronological ordering."""
        first_key = (self._sort_by or "").split(",", 1)[0]
        descending = self._sort_order == "Descending"
        if first_key == "AlbumArtist":
            # Compound key: stripped artist, then album year, album
            # name, disc, track — matches what _safe_sort asks the
            # server for, just with the artist stripped.
            def key(it: dict):
                v = it.get("AlbumArtist", "") or ""
                if isinstance(v, list):
                    v = v[0] if v else ""
                return (
                    article_stripped_key(v),
                    it.get("ProductionYear") or 0,
                    article_stripped_key(it.get("Album", "") or ""),
                    it.get("ParentIndexNumber") or 0,
                    it.get("IndexNumber") or 0,
                )
            return sorted(items, key=key, reverse=descending)
        if first_key == "SortName":
            def key2(it: dict) -> str:
                v = it.get("SortName") or it.get("Name") or ""
                return article_stripped_key(v)
            return sorted(items, key=key2, reverse=descending)
        # Date / play-count sorts: the server's order is fine.
        return items

    @staticmethod
    def _safe_sort(sort_by: str) -> str:
        # Extend the user's sort selection with secondary keys that
        # make sense for a flat song list. Songs are denser than
        # albums and the right tiebreaker matters a lot — picking
        # "Album artist" should produce iTunes-style clustering
        # (artist → album release year → album name → disc → track),
        # not alphabetical-by-song-title within each artist.
        if not sort_by:
            return "SortName"
        first = sort_by.split(",", 1)[0]
        if first == "AlbumArtist":
            # Songs grouped by artist render most usefully when each
            # artist's songs cluster into chronological albums and
            # play in track order. ProductionYear (the album year,
            # mirrored on each track) gives the chronological
            # cluster; Album disambiguates same-year releases;
            # ParentIndexNumber/IndexNumber give disc + track order
            # within an album.
            return (
                "AlbumArtist,ProductionYear,Album,"
                "ParentIndexNumber,IndexNumber"
            )
        if first == "PremiereDate":
            # Audio items don't always carry PremiereDate but they
            # do carry ProductionYear (mirrored from the album).
            # Fall back to that and add track ordering as a
            # tiebreaker for songs sharing a release year.
            return "ProductionYear,Album,ParentIndexNumber,IndexNumber"
        return sort_by

    # ── Async result handler ───────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        # Re-sort client-side so leading articles ("The ", "A ", "An ")
        # are ignored when the active sort starts with AlbumArtist or
        # SortName. "The Antlers" clusters under A like the user
        # expects rather than under T.
        items = self._resort_items_by_article(items)
        self._items = items
        # Clear rows in case this is a re-render driven by the
        # background refresh detecting a change. (Initial cache or
        # cold-load path already cleared in load_songs / __init__.)
        if self._rows:
            self._clear_rows()
        if not items:
            self._loading_label.setText("No songs in this library.")
            self._loading_label.setVisible(True)
            return
        # Pre-allocate the container's height so the scroll bar's range
        # is fixed from the first chunk onward. Without this, the
        # container resizes as each chunk lands — which makes the
        # scroll thumb jump and (combined with layout passes) shifts
        # already-rendered rows around. ROW_HEIGHT (56) per item +
        # the layout's bottom margin (SPACE_LG = 16).
        total_h = len(items) * _SongRow.ROW_HEIGHT + SPACE_LG
        self._container.setMinimumHeight(total_h)
        # Defer actual row construction to chunked ticks so the GUI
        # doesn't freeze for ~1-2s rendering thousands of rows in one
        # go. The first tick fires immediately so the user sees rows
        # populating right away.
        self._pending_items = items
        self._pending_idx = 0
        QTimer.singleShot(0, self._render_next_chunk)

    def _render_next_chunk(self):
        if not self._pending_items:
            return
        end = min(self._pending_idx + self.CHUNK_SIZE, len(self._pending_items))
        # Suppress repaints across the whole chunk — Qt would otherwise
        # repaint after every insertWidget call. Re-enable just before
        # we yield so the user sees the batch land in one go.
        self._container.setUpdatesEnabled(False)
        for i in range(self._pending_idx, end):
            item = self._pending_items[i]
            row = _SongRow(i, item, parent=self._container)
            row.play_requested.connect(self._on_row_clicked)
            row.album_browse_requested.connect(self.album_browse_requested.emit)
            self._rows.append(row)
            # Append at the end — AlignTop keeps the stack flush against
            # the top of the layout regardless of how short the column
            # is during the early chunks.
            self._list_layout.addWidget(row)
            # Cover load is deferred to _load_visible_covers — fired
            # from scroll events and after each chunk. Loading 2000
            # covers eagerly was the dominant perceptual delay.
        self._pending_idx = end
        self._container.setUpdatesEnabled(True)
        # Load covers for whatever rows are now visible (the first
        # chunk's rows are immediately on-screen).
        self._load_visible_covers()
        # Hide the loading placeholder after the first chunk lands so
        # the user sees rows immediately rather than a "Loading…"
        # message that lingers while the rest fill in.
        if self._loading_label.isVisible():
            self._loading_label.setVisible(False)
        if self._pending_idx < len(self._pending_items):
            QTimer.singleShot(self.CHUNK_INTERVAL_MS, self._render_next_chunk)
        else:
            # Done — drop the snapshot so it can be GC'd.
            self._pending_items = []

    def _clear_rows(self):
        # Cancel any in-flight chunked render before tearing down.
        self._pending_items = []
        self._pending_idx = 0
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self._items = []
        self._covers_loaded.clear()
        # Reset the pre-allocated height so the next load_songs can
        # set its own based on the new item count.
        self._container.setMinimumHeight(0)

    @Slot(object)
    def _on_refresh_loaded(self, resp):
        """Background-refresh handler — fires after we already rendered
        from the disk cache. Only re-renders if the fresh result's
        item-id sequence actually differs from what's on screen,
        which keeps the typical "library hasn't changed since last
        launch" path silent (no flash, no chunk re-render). When it
        does differ we drop the existing rows and re-render fresh."""
        items = (resp or {}).get("Items") or []
        # Always persist what the server returned — even an unchanged
        # list might have refreshed timestamps that future cache hits
        # benefit from.
        if self._refresh_scope:
            disk_cache.save(self.CACHE_NAME, self._refresh_scope, items)
        if self._items_signature(items) == self._items_signature(self._items):
            return
        # Library actually changed — re-render.
        self._on_items_loaded({"Items": items})

    @staticmethod
    def _items_signature(items):
        """Comparable signature used to short-circuit no-change refreshes.
        Tuple of item Ids — typical edits (renames, played-flag toggles)
        don't change Ids, so the typical refresh round-trip stays silent.
        Adds / deletions / reorders do show up here and trigger a
        re-render."""
        return tuple(it.get("Id", "") for it in items)

    # ── Lazy cover loading ────────────────────────────────────────────

    def _visible_row_range(self) -> "tuple[int, int]":
        """Inclusive-exclusive [first, last) row indices currently in
        (or near) the viewport. A 5-row buffer above and below means
        small scrolls don't pop covers in late."""
        bar = self._scroll.verticalScrollBar()
        top = bar.value()
        bottom = top + max(self._scroll.viewport().height(), _SongRow.ROW_HEIGHT)
        rh = _SongRow.ROW_HEIGHT
        first = max(0, (top // rh) - 5)
        last = min(len(self._rows), (bottom // rh) + 5)
        return first, last

    @Slot()
    def _load_visible_covers(self):
        if not self._rows:
            return
        first, last = self._visible_row_range()
        for i in range(first, last):
            if i in self._covers_loaded:
                continue
            row = self._rows[i]
            item = row._item
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if not cover_id:
                self._covers_loaded.add(i)
                continue
            cover_url = self.api.get_image_url(cover_id, "Primary", 120)
            if not cover_url:
                self._covers_loaded.add(i)
                continue
            self._covers_loaded.add(i)
            load_image_async(
                f"{cover_id}|songrow",
                cover_url, 120, 120,
                row.set_thumb, rounded_radius=4,
            )

    # ── Row click → emit play with snapshot ────────────────────────────

    @Slot(int)
    def _on_row_clicked(self, index: int):
        if 0 <= index < len(self._items):
            self.play_requested.emit(index, list(self._items))
