"""
Native Songs view — model/view rewrite.

The flat-song surface is built on QListView + QAbstractListModel +
QStyledItemDelegate. Each visible row is a single delegate paint call;
there are NO per-row widgets. Loading a 2000-track library used to
involve a 20-tick chunked widget-build that took ~1–2 s and stuttered
during scroll; the model/view rewrite does it in a single
``beginResetModel`` / ``endResetModel`` round-trip and leaves cover-
loading + scrolling on the cheap delegate-paint path.
"""

import logging
from collections import OrderedDict
from typing import Dict, List

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from jellytoast import disk_cache
from jellytoast.async_io import run_async
from jellytoast.design_tokens import (
    RADIUS_SM,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_BODY,
    TYPE_CAPTION,
    rad,
)
from jellytoast.keyboard_focus import (
    focus_first_item_on,
    keyboard_arrow_press,
    keyboard_cursor_active,
    keyboard_focus_in,
    keyboard_focus_out,
    register_keyboard_mode_view,
)
from jellytoast.providers import get_provider
from jellytoast.settings import get_settings
from jellytoast.sort_utils import article_stripped_key
from jellytoast.ui_helpers import (
    EmptyState,
    dpr_bucket,
    fmt_duration_ticks,
    install_autofade_scrollbars,
    load_image_async,
    opaque_menu,
    open_create_smart_playlist,
    screen_dpr,
)

# ── Model ────────────────────────────────────────────────────────────────


class _SongsListModel(QAbstractListModel):
    """Stores the item dicts + a sparse per-row cover pixmap cache.
    The delegate pulls everything it paints via two custom roles —
    ItemRole returns the source dict, CoverRole returns the loaded
    pixmap (None until it lands). Single-shot ``set_items`` replaces
    the chunked widget-build the old implementation needed."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    CoverRole = Qt.ItemDataRole.UserRole + 2

    # Decoded-thumb LRU bound. _covers held a QPixmap per ROW with no cap;
    # scrolling a large flat-song library (a first external user had ~73,000
    # tracks) leaked a pixmap per painted row — multi-GB worst case. Cap it
    # LRU-by-paint exactly like _LibraryItemsModel: data() bumps a painted
    # (visible) thumb to the MRU end so eviction only drops off-screen rows,
    # and _load_visible_covers re-arms an evicted row on scroll-back.
    _COVER_CACHE_MAX = 512

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._covers: "OrderedDict[int, QPixmap]" = OrderedDict()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._items)):
            return None
        if role == self.ItemRole:
            return self._items[row]
        if role == self.CoverRole:
            pix = self._covers.get(row)
            if pix is not None:
                # LRU touch: a painted (visible) thumb becomes most-recently
                # used so set_cover's eviction never drops an on-screen row.
                self._covers.move_to_end(row)
            return pix
        return None

    def items(self) -> List[Dict]:
        return self._items

    def has_cover(self, row: int) -> bool:
        """True if a decoded thumb is resident for ``row`` (no LRU bump).
        Lets _load_visible_covers re-arm a row whose thumb was evicted."""
        return row in self._covers

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self._covers = OrderedDict()
        self.endResetModel()

    def append_items(self, items: List[Dict]):
        """Tail-append. Used by background pagination to grow the list
        without resetting the model (which would wipe scroll position
        + currentIndex). Cover cache is keyed by row index and rows
        only grow at the tail, so existing entries stay valid."""
        if not items:
            return
        first = len(self._items)
        last = first + len(items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._items.extend(items)
        self.endInsertRows()

    def set_cover(self, row: int, pix: QPixmap):
        if not (0 <= row < len(self._items)):
            return
        if pix is None or pix.isNull():
            return
        self._covers[row] = pix
        self._covers.move_to_end(row)
        # Evict the least-recently-painted thumb(s) over the cap. data()
        # bumps every visible row to the MRU end, so the LRU front is always
        # off-screen — a re-scroll reloads it from the disk cache.
        while len(self._covers) > self._COVER_CACHE_MAX:
            self._covers.popitem(last=False)
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.CoverRole])


# ── Delegate ─────────────────────────────────────────────────────────────


class _SongRowDelegate(QStyledItemDelegate):
    """Paints one song row: thumb + title + artist + album + duration.
    All draw operations — no child widgets, no per-row stylesheets, no
    per-row layouts. Hover state comes from option.state (the view's
    mouseTracking feeds State_MouseOver automatically). Album-cell
    click hit-testing is exposed via :meth:`album_rect_for` so the
    view can route album clicks to ``album_browse_requested``."""

    THUMB_SIZE = 44
    ROW_HEIGHT = 56
    LEFT_PAD = SPACE_MD
    RIGHT_PAD = SPACE_MD
    COL_GAP = SPACE_MD
    DURATION_W = 56
    THUMB_RADIUS = RADIUS_SM

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_fonts()
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._build_fonts)

    def _build_fonts(self):
        body_font = QFont()
        body_font.setPixelSize(TYPE_BODY.size_px)
        self._body_font = body_font
        self._fm_body = QFontMetrics(body_font)
        caption_font = QFont()
        caption_font.setPixelSize(TYPE_CAPTION.size_px)
        self._caption_font = caption_font
        self._fm_caption = QFontMetrics(caption_font)
        mono_font = QFont("JetBrains Mono")
        mono_font.setPixelSize(TYPE_CAPTION.size_px)
        self._mono_font = mono_font

    def sizeHint(self, option, index):
        w = option.rect.width() if option.rect.width() > 0 else 200
        return QSize(w, self.ROW_HEIGHT)

    def paint(self, painter, option, index):
        item = index.data(_SongsListModel.ItemRole)
        if item is None:
            return
        cover = index.data(_SongsListModel.CoverRole)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Re-read theme constants on every paint so live-accent /
        # live-theme changes flow through without per-delegate caches.
        from jellytoast.theme import ink_rgb as _ink_rgb
        from jellytoast.ui_helpers import TEXT as _TEXT

        _ink = _ink_rgb()
        rect = option.rect

        # Hover wash (mouse) at white@10; keyboard-focus wash a touch
        # heavier at white@14 so a user navigating by arrow keys can
        # actually see which row Enter targets. State_HasFocus is set
        # on the index that owns the current keyboard cursor while
        # the view itself has focus — Qt's default with NoSelection.
        view = getattr(self, "_view", None) or getattr(option, "widget", None)
        if keyboard_cursor_active(view, index):
            # Accent (purple) highlight — a tinted fill + 2 px ring, so the
            # keyboard cursor reads the same accent language as the Albums
            # grid's focus ring instead of a near-invisible grey wash.
            from jellytoast.ui_helpers import ACCENT

            inset = rect.adjusted(SPACE_SM, 2, -SPACE_SM, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), rad(6), rad(6))
            fill = QColor(ACCENT)
            fill.setAlpha(46)
            painter.fillPath(path, fill)
            ring = QColor(ACCENT)
            ring.setAlpha(220)
            pen = QPen(ring)
            pen.setWidth(2)
            painter.save()
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()
        elif option.state & QStyle.StateFlag.State_MouseOver:
            inset = rect.adjusted(SPACE_SM, 2, -SPACE_SM, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), rad(6), rad(6))
            painter.fillPath(path, QColor(*_ink, 10))

        thumb_y = rect.y() + (rect.height() - self.THUMB_SIZE) // 2
        thumb_rect = QRect(
            rect.x() + self.LEFT_PAD,
            thumb_y,
            self.THUMB_SIZE,
            self.THUMB_SIZE,
        )
        if cover is not None and not cover.isNull():
            painter.drawPixmap(thumb_rect, cover)
        else:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(thumb_rect),
                self.THUMB_RADIUS,
                self.THUMB_RADIUS,
            )
            painter.fillPath(path, QColor(*_ink, 10))

        cols_x = thumb_rect.right() + self.COL_GAP
        cols_right = rect.right() - self.RIGHT_PAD - self.DURATION_W - self.COL_GAP
        cols_w = max(0, cols_right - cols_x)
        title_w = int(cols_w * 3 / 7)
        artist_w = int(cols_w * 2 / 7)
        album_w = cols_w - title_w - artist_w

        title_rect = QRect(cols_x, rect.y(), title_w, rect.height())
        artist_rect = QRect(
            title_rect.right() + self.COL_GAP,
            rect.y(),
            max(0, artist_w - self.COL_GAP),
            rect.height(),
        )
        album_rect = QRect(
            artist_rect.right() + self.COL_GAP,
            rect.y(),
            max(0, album_w - self.COL_GAP),
            rect.height(),
        )
        duration_rect = QRect(
            cols_right + self.COL_GAP,
            rect.y(),
            self.DURATION_W,
            rect.height(),
        )

        # Fonts + metrics cached on the delegate (`_build_fonts`) and
        # rebuilt on `PlayerBus.theme_changed`.
        painter.setFont(self._body_font)
        fm_body = self._fm_body
        painter.setPen(QColor(_TEXT))
        title = item.get("Name") or self.tr("Unknown")
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_body.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        painter.setFont(self._caption_font)
        fm_caption = self._fm_caption
        artists = item.get("Artists") or []
        artist = ", ".join(artists) if artists else (item.get("AlbumArtist", "") or "")
        painter.setPen(QColor(_TEXT))
        painter.drawText(
            artist_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_caption.elidedText(artist, Qt.TextElideMode.ElideRight, artist_rect.width()),
        )

        album = item.get("Album", "") or ""
        painter.setPen(QColor(_TEXT))
        painter.drawText(
            album_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_caption.elidedText(album, Qt.TextElideMode.ElideRight, album_rect.width()),
        )

        ticks = item.get("RunTimeTicks", 0) or 0
        if ticks:
            painter.setFont(self._mono_font)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                duration_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                fmt_duration_ticks(ticks),
            )

        painter.restore()

    def album_rect_for(self, option_rect: QRect) -> QRect:
        """Sub-rect of the album column. Mirrors :meth:`paint`'s column
        math so the view can hit-test album clicks against this rect."""
        cols_x = option_rect.x() + self.LEFT_PAD + self.THUMB_SIZE + self.COL_GAP
        cols_right = option_rect.right() - self.RIGHT_PAD - self.DURATION_W - self.COL_GAP
        cols_w = max(0, cols_right - cols_x)
        title_w = int(cols_w * 3 / 7)
        artist_w = int(cols_w * 2 / 7)
        album_w = cols_w - title_w - artist_w
        return QRect(
            cols_x + title_w + artist_w + self.COL_GAP,
            option_rect.y(),
            max(0, album_w - self.COL_GAP),
            option_rect.height(),
        )


# ── View ─────────────────────────────────────────────────────────────────


class _SongsListView(QListView):
    """QListView tuned for the songs surface:

    - Mouse tracking on, so the delegate's hover state repaints with
      the cursor.
    - Uniform item sizes — Qt skips the per-row sizeHint query, which
      is the dominant cost in a 2000-row model with a non-trivial
      delegate.
    - Per-pixel vertical scroll for kinetic-feel motion.
    - mousePressEvent hit-tests the delegate's album-cell sub-rect; if
      the click landed there it emits ``album_clicked`` and consumes
      the event so the default ``clicked`` (which would mean play) is
      suppressed."""

    album_clicked = Signal(str)

    def __init__(self, delegate: _SongRowDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate
        self.setItemDelegate(delegate)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        # No selection — click is a play action, not a selection. Without
        # this, Qt would mark the clicked row as Selected and the state
        # would persist invisibly (we never draw selection in paint()),
        # which is confusing under keyboard nav. NoSelection also drops
        # the focus-rect render that QStyledItemDelegate's default
        # framework would otherwise apply.
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Viewport transparency — same trick as library_grid's scroll
        # area: prevents the default-palette base colour from flashing
        # under the body QSS cascade on first show.
        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        # Keyboard-nav focus ring — engage on Tab/arrow, paint via the
        # delegate's _keyboard_mode gate (shared recipe — keyboard_focus).
        self._keyboard_mode = False
        register_keyboard_mode_view(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            idx = self.indexAt(pos)
            if idx.isValid():
                item = idx.data(_SongsListModel.ItemRole)
                album_rect = self._delegate.album_rect_for(self.visualRect(idx))
                if album_rect.contains(pos):
                    aid = (item or {}).get("AlbumId") or ""
                    if aid:
                        self.album_clicked.emit(aid)
                        e.accept()
                        return
        super().mousePressEvent(e)

    def focusInEvent(self, e):
        # Engage keyboard mode + seed the cursor on keyboard focus so the
        # focus wash paints immediately (shared recipe — keyboard_focus).
        keyboard_focus_in(self, e)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        keyboard_focus_out(self, e)
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        # Arrow keys engage keyboard mode + seed the cursor (shared recipe).
        if keyboard_arrow_press(self, e):
            return
        # Enter on the current row fires the same path as a click —
        # the host wires `clicked` to play. Without this, keyboard
        # users can move the focus cursor through rows but never
        # actually play one.
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid():
                self.clicked.emit(idx)
                e.accept()
                return
        super().keyPressEvent(e)


# ── Public view ─────────────────────────────────────────────────────────


class SongsView(QWidget):
    """Vertical list of all songs in the music library. Built on
    QListView + QAbstractListModel + QStyledItemDelegate — no per-row
    widgets, so loading thousands of tracks costs one model reset
    instead of a chunked widget-build. Public API matches the old
    implementation so call sites in jellytoast/app.py keep working
    unchanged."""

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
        # Raw scope as given to load_songs — a single parent id (str) or
        # a multi-library fetch plan (list[str]); _parent_ids holds the
        # normalized plan.
        self._parent_id = ""
        self._parent_ids: list = [""]
        # Rows for which we've already fired cover loads, so a scroll
        # signal storm doesn't re-issue identical network requests.
        self._covers_loaded: set = set()
        self._refresh_scope: dict = {}
        # Last server page-1 signature we cold-reloaded for. Guards
        # _on_refresh_loaded against an endless reload when the disk cache's
        # row order differs from the server's for the same sort (a stable
        # mismatch, not a real mutation) — see _on_refresh_loaded.
        self._last_refresh_sig: tuple = ()
        # Marked dirty when the offline-mode bus signal fires while
        # we're hidden. ``showEvent`` drains it on next navigation.
        self._refresh_after_offline_toggle: bool = False
        # Pagination state — see load_songs / _load_next_page. Together
        # they gate background pages so a stray bus signal can't
        # interleave a second cascade on top of an in-flight one.
        self._page_fetch_in_flight: bool = False
        self._tail_reached: bool = False
        # Bumped on every (re)seed of the list (load_songs, _clear). A
        # background page fetch captures the gen at dispatch; if the list
        # has been re-seeded by the time it resolves (offline toggle, sort
        # change, refresh), _on_page_loaded drops it instead of appending
        # stale rows onto the current list.
        self._load_gen: int = 0

        # Default to album-chronological clustering — see _safe_sort
        # for why songs cluster by artist→year→album→disc→track rather
        # than alphabetical-by-song-title.
        self._sort_by = "AlbumArtist"
        s = get_settings()
        self._sort_order = "Descending" if s.library_sort_order == "descending" else "Ascending"

        self.setObjectName("songsView")
        self.setStyleSheet("""
            QWidget#songsView,
            QWidget#songsView QWidget,
            QWidget#songsView QListView {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        self._model = _SongsListModel(self)
        self._delegate = _SongRowDelegate(self)
        self._view = _SongsListView(self._delegate, self)
        self._view.setModel(self._model)
        # Tab / chrome-Down land focus on the inner list, not the dead
        # wrapper QWidget (mirrors LibraryGrid.setFocusProxy).
        self.setFocusProxy(self._view)
        # The delegate paints its keyboard-cursor highlight off this view's
        # currentIndex (reliable under NoSelection, unlike State_HasFocus).
        self._delegate._view = self._view
        # Outer padding around the row stack — matches the old
        # _list_layout's SPACE_LG horizontal contentsMargins.
        self._view.setViewportMargins(SPACE_LG, 0, SPACE_LG, SPACE_LG)
        install_autofade_scrollbars(self._view)

        # Stack the song list with an empty-state surface so an
        # unauthenticated / empty / failed load reads as "no songs"
        # with the same visual language as albums / artists / genres.
        self._empty_state = EmptyState(
            glyph="♪",
            headline=self.tr("No songs yet"),
            sub=self.tr("Your library is empty, or your connection isn't ready."),
            action_label=self.tr("Refresh"),
            parent=self,
        )
        self._empty_state.action_clicked.connect(
            self._on_empty_state_refresh,
        )
        self._content_stack = QStackedWidget(self)
        self._content_stack.setStyleSheet("background: transparent;")
        self._content_stack.addWidget(self._view)
        self._content_stack.addWidget(self._empty_state)
        outer.addWidget(self._content_stack, 1)
        self._initial_load_complete = False

        self._view.clicked.connect(self._on_view_clicked)
        self._view.album_clicked.connect(self.album_browse_requested.emit)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)

        # Coalesce scroll-driven cover loading at ~60Hz — same pattern
        # as library_grid: kinetic scroll fires valueChanged at
        # 60–120Hz, but the visible-range walk only needs to run once
        # per frame. Trailing-edge debounce keeps the work bounded.
        self._scroll_coalesce = QTimer(self)
        self._scroll_coalesce.setSingleShot(True)
        self._scroll_coalesce.setInterval(16)
        self._scroll_coalesce.timeout.connect(self._load_visible_covers)
        self._view.verticalScrollBar().valueChanged.connect(self._on_scroll_raw)
        # First viewport resize kicks an initial cover load — the
        # viewport's height is 0 until first show, and without this
        # the initial batch lands to an empty visible range.
        self._view.viewport().installEventFilter(self)

        # Live-accent: the delegate re-reads ACCENT/TEXT/... on every
        # paint, so a theme change just needs to invalidate the
        # viewport.
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._view.viewport().update)
        # Cross-DPR cover refresh — re-issue cover loads at the new
        # physical target when the user drags the window to a
        # different-scale monitor. Matches the pattern used by
        # library_grid, mini_player, NP bar, NP page.
        PlayerBus.get().dpr_changed.connect(self._on_dpr_changed)
        # Re-render when offline mode flips — swaps between server and
        # downloads.db. QueuedConnection so the re-query lands on the
        # next event-loop tick rather than stalling the GUI thread
        # inside the bus emit chain.
        PlayerBus.get().offline_mode_changed.connect(
            self._on_offline_mode_changed,
            Qt.ConnectionType.QueuedConnection,
        )
        # Settings → "Refresh album art" — re-issue cover loads against
        # the now-cleared caches so visible rows pick up server-side
        # art changes without needing to navigate away and back.
        PlayerBus.get().image_cache_cleared.connect(
            self._on_image_cache_cleared,
        )

        self._items_loaded.connect(self._on_items_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)

    def focus_first_item(self):
        """Keyboard parity with LibraryGrid / Suggestions — the app-level
        chrome-Down filter calls this to dive focus into the first row."""
        focus_first_item_on(self._view)

    def _on_dpr_changed(self):
        """Drop the per-row covers-loaded set + re-run the visible
        loader so covers get re-requested sized for the new monitor's
        DPR. Cheap — the L1 cache is keyed by physical size, so the
        new requests miss naturally and derive from the L2 raw cache
        without a fresh network round-trip."""
        if self._model.rowCount() == 0:
            return
        self._covers_loaded.clear()
        self._load_visible_covers()

    def _on_image_cache_cleared(self):
        # Same shape as DPR refresh: forget what we've loaded so the
        # visible window re-issues against the (now empty) caches.
        if self._model.rowCount() == 0:
            return
        self._covers_loaded.clear()
        self._load_visible_covers()

    # ── Backwards-compatible accessors ────────────────────────────────

    @property
    def _items(self) -> List[Dict]:
        """session_controller.py reads this to gate re-loads on emptiness."""
        return self._model.items()

    # ── Scroll plumbing ───────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._view.viewport():
            if event.type() == event.Type.Resize:
                if not self._scroll_coalesce.isActive():
                    self._scroll_coalesce.start()
        return super().eventFilter(obj, event)

    def _on_scroll_raw(self, *_):
        if not self._scroll_coalesce.isActive():
            self._scroll_coalesce.start()

    @Slot()
    def _load_visible_covers(self):
        rc = self._model.rowCount()
        if rc == 0:
            return
        vp = self._view.viewport()
        h = vp.height()
        if h <= 0:
            return
        top_idx = self._view.indexAt(QPoint(4, 0))
        bot_idx = self._view.indexAt(QPoint(4, max(0, h - 1)))
        first = top_idx.row() if top_idx.isValid() else 0
        last = bot_idx.row() if bot_idx.isValid() else rc - 1
        if first < 0:
            first = 0
        if last < 0:
            last = rc - 1
        buf = 5
        first = max(0, first - buf)
        last = min(rc - 1, last + buf)
        if first > last:
            return

        # Pattern-1 cache contract: server fetch at the fixed worst-
        # case-DPR source size (THUMB_SIZE × 3) so the L2 raw cache
        # holds one entry per item that derives every DPR locally —
        # and cross-surface hits with search_view come free off the
        # same raw. See docs/research/dpr_cache_keys.md.
        dpr = dpr_bucket(screen_dpr(self))
        target_phys = max(
            _SongRowDelegate.THUMB_SIZE,
            int(round(_SongRowDelegate.THUMB_SIZE * dpr)),
        )
        radius_phys = int(round(_SongRowDelegate.THUMB_RADIUS * dpr))
        server_px = _SongRowDelegate.THUMB_SIZE * 3

        items = self._model.items()
        for row in range(first, last + 1):
            # Re-arm a row whose thumb the LRU evicted: skip only if it's
            # loaded AND still resident. Art-less rows fall through and are
            # re-checked cheaply (no network) on each pass.
            if row in self._covers_loaded and self._model.has_cover(row):
                continue
            item = items[row]
            cover_id = item.get("AlbumId") or item.get("Id", "")
            if not cover_id:
                self._covers_loaded.add(row)
                continue
            cover_url = self.api.get_image_url(cover_id, "Primary", server_px)
            if not cover_url:
                self._covers_loaded.add(row)
                continue
            self._covers_loaded.add(row)

            # Default-arg-bind row so the callback captures the right
            # index even if the loop var changes before the reply lands.
            def _on_pix(pix, r=row):
                self._model.set_cover(r, pix)

            load_image_async(
                f"{cover_id}|songrow",
                cover_url,
                target_phys,
                target_phys,
                _on_pix,
                rounded_radius=radius_phys,
            )

    # ── Click + context menu ──────────────────────────────────────────

    @Slot(QPoint)
    def _on_context_menu(self, pos):
        idx = self._view.indexAt(pos)
        if not idx.isValid():
            return
        item = idx.data(_SongsListModel.ItemRole)
        if not item:
            return
        from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

        bus = PlayerBus.get()
        menu = opaque_menu(self._view)
        play_next = menu.addAction(self.tr("Play next"))
        add_end = menu.addAction(self.tr("Add to queue"))

        # Start-radio — seeds an INSTANT_MIX queue with this track; the
        # RadioFeeder auto-extends with similar tracks near the tail.
        song_id = item.get("Id", "") or ""
        radio_act = None
        if song_id:
            menu.addSeparator()
            radio_act = menu.addAction(self.tr("Start radio from this song"))

        # Create smart playlist seeded by this track — "More like
        # {Track}". from_track reads the track's Genres +
        # ProductionYear off the item dict to seed a genre + year-
        # window recipe. More expressive than the old "more by
        # artist" fallback since it captures the SONG's vibe rather
        # than just the artist's catalog.
        track_name = (item.get("Name") or item.get("Title") or "").strip()
        sp_act = None
        if track_name:
            if radio_act is None:
                menu.addSeparator()
            sp_act = menu.addAction(
                self.tr("Create smart playlist: More like {0}").format(track_name)
            )

        # Edit tags — only when the active provider supports metadata
        # editing AND the signed-in account is permitted (Jellyfin
        # admins). Both gates, so a regular user never sees a dead entry.
        edit_act = None
        if song_id:
            from jellytoast.providers import get_provider

            prov = get_provider()
            if getattr(prov, "can_edit_metadata", False) and prov.can_edit_metadata_on_account():
                menu.addSeparator()
                edit_act = menu.addAction(self.tr("Edit tags…"))

        chosen = menu.exec(self._view.viewport().mapToGlobal(pos))
        if chosen is play_next:
            bus.queue_add_next.emit([item])
        elif chosen is add_end:
            bus.queue_add_end.emit([item])
        elif radio_act is not None and chosen is radio_act:
            ctx = QueueContext(
                kind=QueueKind.INSTANT_MIX,
                source_id=song_id,
                source_label=item.get("Name") or item.get("Title") or "",
                seed_kind="track",
            )
            bus.queue_play_now.emit([item], 0, ctx)
        elif sp_act is not None and chosen is sp_act:
            open_create_smart_playlist(self, "track", track_name, item=item)
        elif edit_act is not None and chosen is edit_act:
            from jellytoast.tag_editor import open_tag_editor
            from jellytoast.toast import show_toast

            if open_tag_editor(item, self):
                show_toast(self.window(), self.tr("Tags updated"), bottom_margin=128)

    @Slot(object)
    def _on_view_clicked(self, idx):
        if not idx.isValid():
            return
        row = idx.row()
        if 0 <= row < self._model.rowCount():
            self.play_requested.emit(row, list(self._model.items()))

    # ── Public API ────────────────────────────────────────────────────

    # Songs is the densest read in the app: a 5000-track library used
    # to fetch as a single 2000-item recursive Audio call with a 5-key
    # composite sort, which routinely exceeded the 15 s `_get` timeout
    # AND silently truncated past 2000 items. The new strategy is
    # LibraryGrid's pattern: render page 1 (PAGE_SIZE items) fast, then
    # silently page the rest in the background, saving each page back
    # to the disk cache. Subsequent launches render the full library
    # from the cache in one paint. Server sort is reduced to a single
    # primary key (`_safe_sort`) so each page returns within seconds.
    PAGE_SIZE = 500
    FETCH_TIMEOUT_S = 30

    def load_songs(self, parent_id=""):
        """Render songs under ``parent_id``. Cache-first: if a complete
        cache exists, render it instantly and refresh page 1 in the
        background. Otherwise fetch page 1 + cascade silent background
        pages until the tail is reached.

        ``parent_id`` is a single library id (``str``) OR a multi-library
        fetch plan (``list[str]`` from ``_music_fetch_plan()``). A
        2+-entry plan takes the union path: one worker drains every
        folder and the merged list renders complete in one paint —
        the page cascade never runs (its offsets are per-folder)."""
        plan = list(parent_id) if isinstance(parent_id, (list, tuple)) else [parent_id]
        multi = len(plan) > 1
        # Keep the raw scope so sort/offline/showEvent reloads replay it
        # verbatim (str or list) through this same normalization.
        self._parent_id = parent_id if multi else plan[0]
        self._parent_ids = plan
        # Invalidate any in-flight background page fetch from a prior load:
        # both the offline short-circuit and the server path below re-seed
        # the model, so a fetch that resolves after this point must not
        # append onto the new list. Capture the new generation so the cold
        # fetch + page-1 refresh + render all carry it (the background
        # pages already did) — without it a cold fetch that resolves AFTER a
        # sort change overwrites the current-sort render with stale rows.
        self._load_gen += 1
        gen = self._load_gen
        # Offline mode short-circuit — only downloaded tracks are
        # playable, so the list is gathered from downloads.db. Skips
        # the parent-id filter (downloads aren't bucketed by library
        # collection) and the disk cache.
        from jellytoast import offline as _offline

        if _offline.is_offline_mode():
            self._render_offline_songs()
            return
        sort_by = self._safe_sort(self._sort_by)
        scope = {
            # Order-independent for a plan: the same subset picked in a
            # different order must hit the same cache.
            "parent_id": "|".join(sorted(plan)) if multi else plan[0],
            "sort_by": sort_by,
            "sort_order": self._sort_order,
            # Bumped when item Fields= changed in jellyfin_api.get_items
            # — see library_grid.load_items for the same pattern.
            # Old caches don't carry Genres (added 2026-05-28); a scope
            # bump forces a one-shot re-fetch so smart-playlist
            # seeding from a right-clicked track works.
            # Bumped to 3 on 2026-07-05: DateCreated added to Fields for
            # the multi-library union merges.
            "_item_schema": 3,
        }
        cached = disk_cache.load(self.CACHE_NAME, scope)
        self._refresh_scope = scope
        # Reset pagination state.
        self._page_fetch_in_flight = False
        self._tail_reached = False
        if multi:
            self._multi_load(cached, sort_by, gen)
            return
        # Single-parent path below: unwrap a 1-entry plan so the fetch
        # sites receive the plain string the provider API expects.
        parent_id = plan[0]
        if cached:
            # Cache shape is either the legacy bare list (page 1 only,
            # written by pre-pagination versions) or the new envelope
            # ``{"items": [...], "complete": bool}``. Detect both for
            # forward + back compat.
            if isinstance(cached, dict):
                cached_items = cached.get("items") or []
                cached_complete = bool(cached.get("complete"))
            else:
                cached_items = cached
                cached_complete = False
            self._items_loaded.emit({"Items": cached_items, "_load_gen": gen})
            # Always refresh page 1 to catch tag/metadata mutations.
            run_async(
                self.api.get_items,
                parent_id,
                self.ITEM_TYPE,
                self.PAGE_SIZE,
                0,
                sort_by,
                self._sort_order,
                True,
                timeout=self.FETCH_TIMEOUT_S,
                on_result=lambda resp, g=gen: self._refresh_loaded.emit(
                    {**(resp or {}), "_load_gen": g}
                ),
                on_error=lambda e: logger.warning("songs refresh failed: %r", e),
            )
            # Partial cache → silently continue paging from where the
            # previous session left off. Schedule one tick out so the
            # initial paint + cover-loading work lands first.
            if not cached_complete and len(cached_items) >= self.PAGE_SIZE:
                QTimer.singleShot(500, self._load_next_page)
            return
        self._clear()
        # _clear() bumps _load_gen (to invalidate any prior in-flight
        # fetch), so the `gen` captured above is now STALE. Re-sync it
        # AFTER the clear, or this cold fetch is dispatched already-stale
        # and _on_cold_fetch drops its own result on the generation guard
        # — leaving the view blank forever (0 rows, _page_fetch_in_flight
        # stuck True) and never writing the disk cache. Found live on a
        # cache-cold Navidrome library, 2026-06-02.
        gen = self._load_gen
        # Cold-load: keep the view page showing (blank) until page 1
        # lands; EmptyState only fires after we know the load returned
        # zero (or errored), not during the network round-trip.
        self._content_stack.setCurrentIndex(0)
        self._page_fetch_in_flight = True
        run_async(
            self.api.get_items,
            parent_id,
            self.ITEM_TYPE,
            self.PAGE_SIZE,
            0,
            sort_by,
            self._sort_order,
            True,
            timeout=self.FETCH_TIMEOUT_S,
            on_result=lambda resp, g=gen: self._on_cold_fetch(resp, g),
            on_error=lambda e: self._on_cold_error(e),
        )

    def _load_next_page(self):
        """Background-paginate: fetch the next chunk past whatever's
        already in the model and append. Stops when the tail is reached
        (short page) or on error. Saves the accumulated cache after
        each page so the next launch can render everything in one
        paint."""
        if self._page_fetch_in_flight or self._tail_reached:
            return
        if not self._refresh_scope:
            return
        if len(self._parent_ids) > 1:
            return  # union scope renders complete; per-folder offsets don't apply
        offset = self._model.rowCount()
        if offset == 0:
            return
        sort_by = self._refresh_scope.get("sort_by") or "SortName"
        self._page_fetch_in_flight = True
        gen = self._load_gen
        run_async(
            self.api.get_items,
            self._parent_id,
            self.ITEM_TYPE,
            self.PAGE_SIZE,
            offset,
            sort_by,
            self._sort_order,
            True,
            timeout=self.FETCH_TIMEOUT_S,
            on_result=lambda resp, g=gen: self._on_page_loaded(resp, g),
            on_error=lambda e: self._on_page_error(e),
        )

    def _on_page_loaded(self, resp, gen=None):
        # Drop a page that resolved after the list was re-seeded (offline
        # toggle, sort change, refresh): its scope no longer matches the
        # current list, so appending would tack stale rows onto it.
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        self._page_fetch_in_flight = False
        raw_count = len(items)
        if not items:
            self._tail_reached = True
            # Persist complete=True so next launch knows we're done.
            self._save_cache_async(self._model.items(), True)
            return
        # Drop rows already in the list. Defends against a provider whose
        # pagination isn't deterministic — e.g. a Subsonic random-songs feed
        # that ignores the offset and re-rolls an overlapping batch each page
        # (#10): without this, such a source appends duplicate rows forever
        # and never trips the short-page tail-stop. A no-op for providers
        # that page deterministically (Jellyfin), whose pages never overlap.
        existing = {it.get("Id") for it in self._model.items() if it.get("Id")}
        items = [it for it in items if it.get("Id") not in existing]
        if not items:
            # The whole page was already shown — the source isn't advancing.
            # Stop rather than spin re-fetching the same rows.
            self._tail_reached = True
            self._save_cache_async(self._model.items(), True)
            return
        if getattr(self.api, "sorts_songs_server_side", True):
            # Per-page article-strip cluster fix. Mild cross-page artifact
            # possible (an article-stripped "The X" in page N could sort
            # before items in page N-1) but corrected on next launch's
            # full-list re-sort from the cache.
            items = self._resort_items_by_article(items)
            self._model.append_items(items)
        else:
            # The provider's song feed arrives in ITS OWN fixed order
            # (Subsonic search3 has no sort parameter), so a page-local
            # sort can only ever be right within one page — the user's
            # sort held per 500-track chunk, not globally. Merge and
            # re-sort the full accumulated list. The model reset shifts
            # row numbers, so the row-keyed cover bookkeeping resets with
            # it; the scroll offset is restored so a background page
            # landing mid-scroll doesn't yank the viewport.
            merged = self._client_sort_items(self._model.items() + items)
            bar = self._view.verticalScrollBar()
            pos = bar.value()
            self._model.set_items(merged)
            self._covers_loaded.clear()
            bar.setValue(pos)
            self._invalidate_smooth_scroll()
        # Tail = the SERVER returned a short page. Measured on the raw count,
        # not the post-dedup count, so a full page with a few incidental
        # overlaps still schedules the next fetch (and Jellyfin, whose pages
        # never overlap, behaves exactly as before).
        complete = raw_count < self.PAGE_SIZE
        if complete:
            self._tail_reached = True
        self._save_cache_async(self._model.items(), complete)
        # Refresh cover loading in case the new tail intersects the
        # visible viewport (rare unless user is scrolled all the way
        # down right as the page lands).
        self._load_visible_covers()
        if not self._tail_reached:
            # Brief delay between pages keeps background fetching from
            # competing with the user's scroll / cover-loading work.
            QTimer.singleShot(200, self._load_next_page)

    def _on_page_error(self, exc):
        """Background page failed — stop the cascade, log, don't blow
        away the user's view. The next refresh / navigation can retry."""
        logger.warning("songs page fetch failed: %r", exc)
        self._page_fetch_in_flight = False
        self._tail_reached = True

    def _save_cache_async(self, items: List[Dict], complete: bool):
        """Persist the cache envelope off the GUI thread. The full
        songs payload can be tens of MB serialized; doing it on the
        GUI thread between page appends is what makes scroll hitch."""
        if not self._refresh_scope:
            return
        scope = dict(self._refresh_scope)
        payload = {"items": list(items), "complete": complete}
        run_async(
            disk_cache.save,
            self.CACHE_NAME,
            scope,
            payload,
            on_result=lambda _r: None,
            on_error=lambda e: logger.warning("songs cache save failed: %r", e),
        )

    def _on_cold_error(self, exc):
        """Cold load failed — timeout, network, 5xx. Show a real error
        state instead of pretending the library is empty; the previous
        ``{Items: []}`` fallback rendered as 'No songs yet' with a
        Refresh button that just retried the same failing request,
        which actively misled the user."""
        logger.warning("songs cold fetch failed: %r", exc)
        self._page_fetch_in_flight = False
        self._tail_reached = True
        self._model.set_items([])
        self._covers_loaded.clear()
        self._initial_load_complete = True
        self._empty_state.set_state(
            glyph="⚠",
            headline=self.tr("Couldn't load songs"),
            sub=self.tr(
                "The server didn't respond. Check your connection and try again."
            ),
            action_label=self.tr("Retry"),
        )
        self._content_stack.setCurrentIndex(1)

    def _on_cold_fetch(self, resp, gen=None):
        # Drop a cold page-1 that resolved after a newer load_songs() (e.g. a
        # sort change during the round trip): rendering it would overwrite
        # the current-sort list with stale rows, and the cascade it kicks
        # would then paginate the current sort onto a wrong head.
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        self._page_fetch_in_flight = False
        complete = len(items) < self.PAGE_SIZE
        if complete:
            self._tail_reached = True
        if items:
            self._save_cache_async(items, complete)
        # Render page 1 (or empty state if zero items returned). Stamp the
        # generation so _on_items_loaded drops it too if superseded.
        self._items_loaded.emit({"Items": items, "_load_gen": gen})
        # If page 1 was full, schedule page 2 silently.
        if not self._tail_reached:
            QTimer.singleShot(200, self._load_next_page)

    # ── Multi-library union path ──────────────────────────────────────

    def _multi_load(self, cached, sort_by: str, gen: int):
        """Load a 2+-folder plan. Cache-first like the single path, but
        the union always renders as one complete list; the page cascade
        never runs (per-folder offsets mean nothing against a merged
        list). A cache hit paints instantly and a background union
        re-fetch replaces the render + cache if the set of songs
        changed."""
        self._tail_reached = True  # no incremental pages on this path
        if cached:
            cached_items = (
                cached.get("items") or [] if isinstance(cached, dict) else cached
            )
            self._items_loaded.emit({"Items": cached_items, "_load_gen": gen})
            run_async(
                self._fetch_union_sync,
                sort_by,
                on_result=lambda items, g=gen: self._on_union_refresh(items, g),
                on_error=lambda e: logger.warning("songs union refresh failed: %r", e),
            )
            return
        self._clear()
        # _clear() bumps _load_gen — re-sync or this fetch is born stale
        # (same footgun the single cold path documents).
        gen = self._load_gen
        self._tail_reached = True
        self._content_stack.setCurrentIndex(0)
        self._page_fetch_in_flight = True
        run_async(
            self._fetch_union_sync,
            sort_by,
            on_result=lambda items, g=gen: self._on_union_loaded(items, g),
            on_error=lambda e: self._on_cold_error(e),
        )

    def _fetch_union_sync(self, sort_by: str) -> "List[Dict]":
        """Drain + merge every folder in the plan. Blocking — runs on
        the run_async worker, never the GUI thread. The union key gives
        a stable global order for the merge; the render pass re-sorts
        with the full iTunes cascade (``_client_sort_items``) either
        way, so the final order matches the single-folder path."""
        from jellytoast import library_selection as _ls

        def _page(pid: str, offset: int, count: int) -> "List[Dict]":
            resp = self.api.get_items(
                pid,
                self.ITEM_TYPE,
                count,
                offset,
                sort_by,
                self._sort_order,
                True,
                timeout=self.FETCH_TIMEOUT_S,
            )
            return (resp or {}).get("Items") or []

        return _ls.fetch_union(
            _page,
            self._parent_ids,
            sort_key=_ls.union_sort_key(sort_by),
            reverse=self._sort_order == "Descending",
            page_size=self.PAGE_SIZE,
        )

    def _on_union_loaded(self, items, gen=None):
        if gen is not None and gen != self._load_gen:
            return
        self._page_fetch_in_flight = False
        items = items or []
        if items and self._refresh_scope:
            self._save_cache_async(items, True)
        self._items_loaded.emit({"Items": items, "_load_gen": gen})

    def _on_union_refresh(self, items, gen=None):
        """Background union re-fetch after a cache-hit paint. Compare as
        ID SETS, not ordered tuples — the union's pre-render order is a
        merge artifact and an order-only diff must not re-render forever
        (the single path's ``_last_refresh_sig`` guard, solved
        structurally)."""
        if gen is not None and gen != self._load_gen:
            return
        items = items or []
        fresh = frozenset(it.get("Id", "") for it in items)
        have = frozenset(it.get("Id", "") for it in self._model.items())
        if fresh == have:
            return
        if items and self._refresh_scope:
            self._save_cache_async(items, True)
        self._items_loaded.emit({"Items": items, "_load_gen": gen})

    def _render_offline_songs(self):
        """Render every playable downloaded track. ``list_complete_items``
        spans both explicitly-requested tracks and the children pulled
        in by an album / playlist / artist download — what a user
        thinks of as "the music I have offline."""
        from jellytoast import offline as _offline

        items = _offline.list_complete_items("track") or []
        items = [it for it in items if it.get("Id")]
        self._refresh_scope = {}
        self._items_loaded.emit({"Items": items})

    def _on_offline_mode_changed(self, _on: bool):
        # QueuedConnection on the bus side defers this to the next
        # event-loop tick. Hidden views (the user is in Settings or
        # any non-songs surface) skip the reload entirely and mark
        # themselves dirty — ``showEvent`` drains the flag the next
        # time the user navigates here. Keeps the offline-mode toggle
        # responsive while the user is staring at something else.
        if not self.isVisible():
            self._refresh_after_offline_toggle = True
            return
        self._refresh_after_offline_toggle = False
        self.load_songs(self._parent_id)

    def showEvent(self, event):
        super().showEvent(event)
        if self._refresh_after_offline_toggle:
            self._refresh_after_offline_toggle = False
            self.load_songs(self._parent_id)

    def set_sort(self, sort_by: str, sort_order: str):
        self._sort_by = sort_by or "SortName"
        self._sort_order = "Descending" if sort_order == "descending" else "Ascending"
        self.load_songs(self._parent_id)

    # ── Sort helpers ──────────────────────────────────────────────────

    def _resort_items_by_article(self, items: "List[Dict]") -> "List[Dict]":
        """Client-side re-sort that ignores leading articles in name
        fields. Server's AlbumArtist sort puts 'The Antlers' under T;
        this re-sorts so they cluster as 'Antlers'. The secondary
        keys keep iTunes-style album-chronological ordering within
        each artist."""
        first_key = (self._sort_by or "").split(",", 1)[0]
        descending = self._sort_order == "Descending"
        if first_key == "AlbumArtist":

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
        return items

    def _client_sort_items(self, items: "List[Dict]") -> "List[Dict]":
        """Full client-side sort for providers whose song feed can't be
        server-sorted (``sorts_songs_server_side`` False — Subsonic's
        search3 returns a fixed order). Unlike ``_resort_items_by_article``
        (which passes date sorts through, trusting the server's order),
        this must handle EVERY ``LIBRARY_SORT_OPTIONS`` key itself. Items
        missing the key cluster together in stable (feed) order —
        notably "Recently played": Subsonic items carry no last-played
        date, so that sort can't be honoured and the list stays in feed
        order rather than pretending."""
        first_key = (self._sort_by or "").split(",", 1)[0]
        if first_key in ("", "SortName", "AlbumArtist"):
            return self._resort_items_by_article(items)
        descending = self._sort_order == "Descending"
        if first_key == "PremiereDate":

            def key(it: dict):
                return (
                    it.get("ProductionYear") or 0,
                    article_stripped_key(it.get("Album", "") or ""),
                    it.get("ParentIndexNumber") or 0,
                    it.get("IndexNumber") or 0,
                )

        elif first_key == "DateCreated":

            def key(it: dict):
                # ISO-8601 strings — lexical order IS chronological order.
                return (
                    it.get("DateCreated") or "",
                    article_stripped_key(it.get("SortName") or it.get("Name") or ""),
                )

        elif first_key == "DatePlayed":

            def key(it: dict):
                ud = it.get("UserData") or {}
                return (
                    ud.get("LastPlayedDate") or "",
                    article_stripped_key(it.get("SortName") or it.get("Name") or ""),
                )

        else:
            return items
        return sorted(items, key=key, reverse=descending)

    def _invalidate_smooth_scroll(self):
        """Programmatic scroll jump → drop the wheel filter's cached
        target for our bar, or the next wheel notch snaps the view back
        (the SmoothScrollFilter invariant)."""
        app = QApplication.instance()
        sf = getattr(app, "_smooth_scroll", None)
        if sf is not None:
            sf.invalidate(self._view.verticalScrollBar())

    @staticmethod
    def _safe_sort(sort_by: str) -> str:
        """Wire-side sort key. We ask the server for the PRIMARY key only
        — `_resort_items_by_article` re-sorts on the full iTunes cascade
        (artist → release year → album → disc → track) client-side, so
        asking the server for the same composite was duplicative work
        that pushed the recursive Audio fetch past the 15 s timeout on
        large libraries. PremiereDate is mapped to ProductionYear since
        Audio items don't always carry PremiereDate."""
        if not sort_by:
            return "SortName"
        first = sort_by.split(",", 1)[0]
        if first == "PremiereDate":
            return "ProductionYear"
        return first

    # ── Async result handlers ─────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        # Uniform generation guard for every render path (cold + cache): a
        # superseded load's render must not overwrite the current one. The
        # synchronous offline path stamps no gen (None) and renders as usual.
        gen = (resp or {}).get("_load_gen")
        if gen is not None and gen != self._load_gen:
            return
        items = (resp or {}).get("Items") or []
        if len(self._parent_ids) > 1 or not getattr(
            self.api, "sorts_songs_server_side", True
        ):
            # A merged union has no server order to trust (each folder was
            # sorted independently), and Subsonic's feed can't be server-
            # sorted at all — apply the user's sort in full client-side.
            items = self._client_sort_items(items)
        else:
            items = self._resort_items_by_article(items)
        # The big perf win: single model reset replaces the chunked
        # widget-build the old implementation did over ~20 ticks.
        self._model.set_items(items)
        self._covers_loaded.clear()
        self._initial_load_complete = True
        if not items:
            self._empty_state.set_state(
                glyph="♪",
                headline=self.tr("No songs yet"),
                sub=self.tr("Your library is empty, or your connection isn't ready."),
                action_label=self.tr("Refresh"),
            )
            self._content_stack.setCurrentIndex(1)
            return
        self._content_stack.setCurrentIndex(0)
        self._load_visible_covers()

    def _on_empty_state_refresh(self):
        """User tapped Refresh on the empty-state — drop the cache
        and re-fetch the parent scope. Mirrors LibraryGrid's pattern."""
        try:
            disk_cache.clear(self.CACHE_NAME)
        except Exception:
            pass
        self._initial_load_complete = False
        self._content_stack.setCurrentIndex(0)
        self.load_songs(self._parent_id)

    @Slot(object)
    def _on_refresh_loaded(self, resp):
        """Background refresh fetched page 1 against the live server.
        Compare its signature to the first PAGE_SIZE rows of the model
        (NOT the full model — refresh is page 1 only). On a diff, the
        library has mutated; tear down and re-cold-load from scratch."""
        gen = (resp or {}).get("_load_gen")
        if gen is not None and gen != self._load_gen:
            return  # superseded by a newer load_songs()
        items = (resp or {}).get("Items") or []
        head = self._model.items()[: self.PAGE_SIZE]
        sig = self._items_signature(items)
        if sig == self._items_signature(head):
            self._last_refresh_sig = sig
            return
        # Mutation detected — re-fetch from scratch. But guard against an
        # endless reload: if the disk cache's row order differs from the
        # server's for the same sort (a STABLE condition, not a real
        # mutation), `_clear()` + cold-load just re-renders that same cache,
        # the next refresh re-detects the "mutation", and it spins ~6×/sec —
        # each reset wiping the model's keyboard cursor. Cold-reload at most
        # once per distinct server signature.
        if sig == self._last_refresh_sig:
            return
        self._last_refresh_sig = sig
        self._clear()
        self.load_songs(self._parent_id)

    @staticmethod
    def _items_signature(items):
        return tuple(it.get("Id", "") for it in items)

    def _clear(self):
        self._model.set_items([])
        self._covers_loaded.clear()
        self._page_fetch_in_flight = False
        self._tail_reached = False
        # Re-seed → invalidate any in-flight page fetch (see _load_gen).
        self._load_gen += 1
