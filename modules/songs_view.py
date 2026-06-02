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
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QListView,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.design_tokens import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_BODY,
    TYPE_CAPTION,
)
from modules.providers import get_provider
from modules.settings import get_settings
from modules.sort_utils import article_stripped_key
from modules.ui_helpers import (
    EmptyState,
    dpr_bucket,
    fmt_duration_ticks,
    install_autofade_scrollbars,
    load_image_async,
    opaque_menu,
    open_create_smart_playlist,
    screen_dpr,
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


# ── Model ────────────────────────────────────────────────────────────────


class _SongsListModel(QAbstractListModel):
    """Stores the item dicts + a sparse per-row cover pixmap cache.
    The delegate pulls everything it paints via two custom roles —
    ItemRole returns the source dict, CoverRole returns the loaded
    pixmap (None until it lands). Single-shot ``set_items`` replaces
    the chunked widget-build the old implementation needed."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    CoverRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._covers: Dict[int, QPixmap] = {}

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
            return self._covers.get(row)
        return None

    def items(self) -> List[Dict]:
        return self._items

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self._covers = {}
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
    THUMB_RADIUS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_fonts()
        from modules.player_state import PlayerBus

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
        from modules.theme import ink_rgb as _ink_rgb
        from modules.ui_helpers import TEXT as _TEXT

        _ink = _ink_rgb()
        rect = option.rect

        # Hover wash (mouse) at white@10; keyboard-focus wash a touch
        # heavier at white@14 so a user navigating by arrow keys can
        # actually see which row Enter targets. State_HasFocus is set
        # on the index that owns the current keyboard cursor while
        # the view itself has focus — Qt's default with NoSelection.
        if option.state & QStyle.StateFlag.State_HasFocus:
            inset = rect.adjusted(SPACE_SM, 2, -SPACE_SM, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 6, 6)
            painter.fillPath(path, QColor(*_ink, 18))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            inset = rect.adjusted(SPACE_SM, 2, -SPACE_SM, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 6, 6)
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
        title = item.get("Name") or "Unknown"
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
        # Seed currentIndex to row 0 on first keyboard focus so the
        # focus wash paints immediately and the next arrow keypress
        # has a sensible base to step from.
        if (
            not self.currentIndex().isValid()
            and self.model() is not None
            and self.model().rowCount() > 0
        ):
            self.setCurrentIndex(self.model().index(0, 0))
        super().focusInEvent(e)

    def keyPressEvent(self, e):
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
    implementation so call sites in jellytoast.py keep working
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
        self._parent_id: str = ""
        # Rows for which we've already fired cover loads, so a scroll
        # signal storm doesn't re-issue identical network requests.
        self._covers_loaded: set = set()
        self._refresh_scope: dict = {}
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
        # Outer padding around the row stack — matches the old
        # _list_layout's SPACE_LG horizontal contentsMargins.
        self._view.setViewportMargins(SPACE_LG, 0, SPACE_LG, SPACE_LG)
        install_autofade_scrollbars(self._view)

        # Stack the song list with an empty-state surface so an
        # unauthenticated / empty / failed load reads as "no songs"
        # with the same visual language as albums / artists / genres.
        self._empty_state = EmptyState(
            glyph="♪",
            headline="No songs yet",
            sub="Your library is empty, or your connection isn't ready.",
            action_label="Refresh",
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
        from modules.player_state import PlayerBus

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
        """jellytoast.py reads this to gate re-loads on emptiness."""
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
            if row in self._covers_loaded:
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
        from modules.player_state import PlayerBus, QueueContext, QueueKind

        bus = PlayerBus.get()
        menu = opaque_menu(self._view)
        play_next = menu.addAction("Play next")
        add_end = menu.addAction("Add to queue")

        # Start-radio — seeds an INSTANT_MIX queue with this track; the
        # RadioFeeder auto-extends with similar tracks near the tail.
        song_id = item.get("Id", "") or ""
        radio_act = None
        if song_id:
            menu.addSeparator()
            radio_act = menu.addAction("Start radio from this song")

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
            sp_act = menu.addAction(f"Create smart playlist: More like {track_name}")

        # Edit tags — only when the active provider supports metadata
        # editing AND the signed-in account is permitted (Jellyfin
        # admins). Both gates, so a regular user never sees a dead entry.
        edit_act = None
        if song_id:
            from modules.providers import get_provider

            prov = get_provider()
            if getattr(prov, "can_edit_metadata", False) and prov.can_edit_metadata_on_account():
                menu.addSeparator()
                edit_act = menu.addAction("Edit tags…")

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
            from modules.tag_editor import open_tag_editor
            from modules.toast import show_toast

            if open_tag_editor(item, self):
                show_toast(self.window(), "Tags updated", bottom_margin=128)

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

    def load_songs(self, parent_id: str = ""):
        """Render songs under ``parent_id``. Cache-first: if a complete
        cache exists, render it instantly and refresh page 1 in the
        background. Otherwise fetch page 1 + cascade silent background
        pages until the tail is reached."""
        self._parent_id = parent_id
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
        from modules import offline as _offline

        if _offline.is_offline_mode():
            self._render_offline_songs()
            return
        sort_by = self._safe_sort(self._sort_by)
        scope = {
            "parent_id": parent_id,
            "sort_by": sort_by,
            "sort_order": self._sort_order,
            # Bumped when item Fields= changed in jellyfin_api.get_items
            # — see library_grid.load_items for the same pattern.
            # Old caches don't carry Genres (added 2026-05-28); a scope
            # bump forces a one-shot re-fetch so smart-playlist
            # seeding from a right-clicked track works.
            "_item_schema": 2,
        }
        cached = disk_cache.load(self.CACHE_NAME, scope)
        self._refresh_scope = scope
        # Reset pagination state.
        self._page_fetch_in_flight = False
        self._tail_reached = False
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
        # Per-page article-strip cluster fix. Mild cross-page artifact
        # possible (an article-stripped "The X" in page N could sort
        # before items in page N-1) but corrected on next launch's
        # full-list re-sort from the cache.
        items = self._resort_items_by_article(items)
        self._model.append_items(items)
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
            headline="Couldn't load songs",
            sub="The server didn't respond. Check your connection and try again.",
            action_label="Retry",
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

    def _render_offline_songs(self):
        """Render every playable downloaded track. ``list_complete_items``
        spans both explicitly-requested tracks and the children pulled
        in by an album / playlist / artist download — what a user
        thinks of as "the music I have offline."""
        from modules import offline as _offline

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
        items = self._resort_items_by_article(items)
        # The big perf win: single model reset replaces the chunked
        # widget-build the old implementation did over ~20 ticks.
        self._model.set_items(items)
        self._covers_loaded.clear()
        self._initial_load_complete = True
        if not items:
            self._empty_state.set_state(
                glyph="♪",
                headline="No songs yet",
                sub="Your library is empty, or your connection isn't ready.",
                action_label="Refresh",
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
        if self._items_signature(items) == self._items_signature(head):
            return
        # Mutation detected — re-fetch from scratch. _clear + cold-load
        # restarts the pagination cascade and overwrites the stale cache
        # via `_save_cache_async`.
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
