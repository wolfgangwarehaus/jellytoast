"""
Full-window now-playing / queue page.

Two-pane layout:
- Left:  cover art + title + artist · album + lyrics (scrollable, lazy-
         fetched per track with a small LRU).
- Right: track list. Renders the queue's `original_items` for ALBUM /
         PLAYLIST contexts (so the user always sees the source's
         natural order, regardless of shuffle), and the play-order
         items for SHUFFLE / MANUAL / SEARCH / ARTIST / INSTANT_MIX
         contexts. The currently-playing track is highlighted; clicking
         a row jumps to it via `bus.track_jumped`.

The page is swapped in/out of the main window's content stack — it
covers the WebEngine but leaves the top bar and bottom now-playing bar
visible.
"""

import bisect
from collections import OrderedDict
from typing import Dict, List, Optional

from PySide6.QtCore import (
    Qt, QEvent, QObject, QPoint, QRect, QRectF, QSize, QTimer,
    QPropertyAnimation, QEasingCurve, Signal, Slot,
    QAbstractListModel, QMimeData, QModelIndex,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette,
    QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QAbstractItemView, QListView, QMenu, QStyle, QStyledItemDelegate,
)

from modules.player_state import (
    PlayerBus, NowPlaying, get_now_playing, QueueKind, QueueContext,
)
from modules.ui_helpers import (
    load_image_async, fmt_duration_ticks, install_song_context_menu,
    ACCENT, TEXT, TEXT_DIM, TEXT_FAINT, screen_dpr,
)
from modules.design_tokens import (
    TYPE_TITLE, TYPE_BODY, TYPE_CAPTION, TYPE_TINY,
    TYPE_MICRO, BTN_PRIMARY, font, type_qss, button_qss,
    SPACE_SM, SPACE_MD, SPACE_LG,
)
from modules.icons import icon, accent_icon
from modules.providers import get_provider
from modules.async_io import run_async
from modules import disk_cache


# Right-pane behavior per queue context kind. ALBUM/PLAYLIST want
# source order (so the user can see "track 1, 2, 3..."); everything
# else wants the actual play sequence.
_SOURCE_ORDER_KINDS = {QueueKind.ALBUM, QueueKind.PLAYLIST}


class _ScrollbarFader(QObject):
    """Drives a QScrollBar's `QGraphicsOpacityEffect` so the bar fades
    out after a short idle window and fades back in on scroll or hover.
    Layout is unaffected — the bar still occupies its slot, it's just
    invisible when not in use.

    Wakes on:
      - value changes (the user scrolled, or content scrolled them)
      - range changes (content size changed, e.g. queue swap)
      - mouse-enter on the bar itself or its parent scroll area's viewport
    """

    IDLE_MS = 900       # how long after last activity before fading
    FADE_MS = 220       # fade animation duration

    def __init__(self, scroll_area: QScrollArea):
        super().__init__(scroll_area)
        self._area = scroll_area
        self._bar = scroll_area.verticalScrollBar()
        self._effect = QGraphicsOpacityEffect(self._bar)
        self._effect.setOpacity(0.0)
        self._bar.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(self.FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._fade_out)

        self._bar.valueChanged.connect(self._wake)
        self._bar.rangeChanged.connect(self._wake)
        # Hover anywhere over the scroll area's viewport (including the
        # bar itself) keeps the bar awake.
        self._bar.installEventFilter(self)
        viewport = scroll_area.viewport()
        if viewport is not None:
            viewport.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.Enter, QEvent.Type.MouseMove,
                 QEvent.Type.Wheel):
            self._wake()
        return False

    def _wake(self, *_):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._idle_timer.start(self.IDLE_MS)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()


class _ElidingLabel(QLabel):
    """QLabel that quietly truncates overflow text with `…` instead of
    forcing the parent layout wider. QLabel's default `minimumSizeHint`
    is the full text width — inside a `QScrollArea(setWidgetResizable
    True)` that pins the inner widget to the content width, which makes
    a long song title spawn a horizontal scrollbar. Reporting a near-
    zero horizontal size hint plus eliding the text on every resize
    fixes both the scrollbar and the visual clipping."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = text
        super().setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str):
        self._full_text = text or ""
        self._apply_elision()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_elision()

    def _apply_elision(self):
        fm = self.fontMetrics()
        avail = max(0, self.width() - 4)  # tiny pad keeps glyphs off the edge
        elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, avail)
        super().setText(elided)

    def minimumSizeHint(self):
        # Don't make the layout reserve space for the full text. We can
        # render in any width — overflow becomes "…".
        h = super().minimumSizeHint().height()
        return QSize(0, h)

    def sizeHint(self):
        return self.minimumSizeHint()


class _LyricsCache:
    """Tiny LRU keyed by item_id. Avoids re-fetching when the user
    rapidly hops back and forth across the queue. Capacity matches a
    typical album side; bigger caches just hold memory."""
    def __init__(self, capacity: int = 32):
        self.capacity = capacity
        self._d: "OrderedDict[str, Optional[Dict]]" = OrderedDict()

    def get(self, item_id: str) -> "tuple[bool, Optional[Dict]]":
        if item_id in self._d:
            self._d.move_to_end(item_id)
            return True, self._d[item_id]
        return False, None

    def put(self, item_id: str, data: Optional[Dict]):
        self._d[item_id] = data
        self._d.move_to_end(item_id)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)


# ── Track list: model + delegate + view ─────────────────────────────────
#
# Replaces the widget-based _TrackRow / _QueueDropTarget / _DiscDivider
# stack with the same model/view/delegate scaffolding used by every
# other big list in the app (SongsView, LibraryGrid, GenresView,
# HorizontalRail). Drag-to-reorder uses Qt's InternalMove (default
# drop-indicator line) instead of the previous custom grabMouse-based
# animated row-shift; the polished animation is gone but the move
# semantics are identical and the rendering scaffolding now matches
# the rest of the surfaces. Multi-disc album dividers preserved via
# heterogeneous model rows ("track" vs "disc" entries).


class _TracksModel(QAbstractListModel):
    """Items list + presentation state for the NP page track list.
    Holds tracks and (optionally) disc-divider markers as heterogeneous
    rows; the delegate dispatches paint by KindRole.

    Drag-reorder uses Qt's InternalMove framework — flags() advertises
    tracks as drag/drop enabled when ``drag_enabled`` is True; dropMimeData
    translates model-row → play-index and emits
    ``PlayerBus.queue_move_item``. The QueueManager owns truth: after
    the bus signal fires, ``queue_changed`` re-renders via set_state."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    IsCurrentRole = Qt.ItemDataRole.UserRole + 2
    ShowArtistRole = Qt.ItemDataRole.UserRole + 3
    KindRole = Qt.ItemDataRole.UserRole + 4    # "track" | "disc"
    DiscInfoRole = Qt.ItemDataRole.UserRole + 5  # (disc_num, count)
    PlayIndexRole = Qt.ItemDataRole.UserRole + 6  # int or -1
    IsDragGhostRole = Qt.ItemDataRole.UserRole + 7  # bool
    SuppressHoverRole = Qt.ItemDataRole.UserRole + 8  # bool

    def __init__(self, parent=None):
        super().__init__(parent)
        # Each entry: {"kind": "track", "item": {...}, "play_index": N}
        # or {"kind": "disc", "disc_info": (disc, count)}.
        self._entries: List[Dict] = []
        self._current_play_index: int = -1
        self._show_artist: bool = False
        self._drag_enabled: bool = False
        # Custom-drag state — read by the delegate so it can suppress
        # the hover wash globally during a drag (every row should read
        # as static while the user is rearranging) and ghost out the
        # source row (its slot reads as the gap that follows the
        # floating widget). Set by _TracksListView.
        self._drag_active: bool = False
        self._drag_src_row: int = -1

    # ── QAbstractListModel overrides ──────────────────────────────────

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._entries)):
            return None
        entry = self._entries[row]
        if role == self.KindRole:
            return entry["kind"]
        if role == self.ItemRole:
            return entry.get("item")
        if role == self.IsCurrentRole:
            if entry["kind"] != "track":
                return False
            return entry["play_index"] == self._current_play_index
        if role == self.ShowArtistRole:
            return self._show_artist
        if role == self.DiscInfoRole:
            return entry.get("disc_info")
        if role == self.PlayIndexRole:
            return entry.get("play_index", -1)
        if role == self.IsDragGhostRole:
            return (self._drag_active and row == self._drag_src_row)
        if role == self.SuppressHoverRole:
            return self._drag_active
        return None

    def flags(self, index):
        # Drag/drop is driven by the view's own grabMouse-based drag
        # (see _TracksListView) — Qt's QDrag/InternalMove framework
        # isn't used because we can't horizontally-lock the floating
        # widget or tint it opaque/accent through QDrag. So flags here
        # are bookkeeping only: tracks are selectable, dividers are
        # not. ItemIsDragEnabled / ItemIsDropEnabled aren't needed.
        base = Qt.ItemFlag.ItemIsEnabled
        if not index.isValid():
            return base
        row = index.row()
        if not (0 <= row < len(self._entries)):
            return base
        entry = self._entries[row]
        if entry["kind"] != "track":
            return base
        base |= Qt.ItemFlag.ItemIsSelectable
        return base

    # Custom drag flow lives in _TracksListView; the model is mutated
    # in place by `move_track` when the drop commits, mirroring what
    # QueueManager will rebuild on the next queue_changed.

    def move_track(self, src_row: int, dest_row: int) -> bool:
        """Reorder entries in place — called from the view's drop
        handler. Returns (src_play, dest_play) as a tuple via the
        emitted signal, or None if the move is a no-op."""
        if not (0 <= src_row < len(self._entries)):
            return False
        src_entry = self._entries[src_row]
        if src_entry["kind"] != "track":
            return False
        # Clamp dest_row to a valid slot.
        if dest_row < 0:
            dest_row = 0
        if dest_row > len(self._entries):
            dest_row = len(self._entries)
        if dest_row == src_row or dest_row == src_row + 1:
            return False
        self.beginResetModel()
        entry = self._entries.pop(src_row)
        if dest_row > src_row:
            dest_row -= 1
        self._entries.insert(dest_row, entry)
        # Re-number track play_indices in their new order so subsequent
        # drags compute correctly against the new layout.
        n = 0
        for e in self._entries:
            if e["kind"] == "track":
                e["play_index"] = n
                n += 1
        self.endResetModel()
        return True

    def play_index_of_entry(self, src_row: int) -> int:
        """Returns the original play_index of a track entry at src_row,
        snapshotted BEFORE move_track is called (which re-numbers
        play indices)."""
        if 0 <= src_row < len(self._entries):
            e = self._entries[src_row]
            if e["kind"] == "track":
                return e["play_index"]
        return -1

    def dest_play_index_for(self, src_row: int, dest_row: int) -> int:
        """Compute the destination play-index a drop at ``dest_row``
        would land on, given the source is being removed from
        ``src_row``."""
        dest_play = 0
        for i in range(dest_row):
            if i == src_row:
                continue
            e = self._entries[i]
            if e["kind"] == "track":
                dest_play += 1
        return dest_play

    # ── Public API ────────────────────────────────────────────────────

    def items(self) -> List[Dict]:
        """Returns track items only (skipping dividers), in play-order."""
        return [e["item"] for e in self._entries if e["kind"] == "track"]

    def play_index_at(self, row: int) -> int:
        if 0 <= row < len(self._entries):
            entry = self._entries[row]
            if entry["kind"] == "track":
                return entry["play_index"]
        return -1

    def row_for_play_index(self, play_index: int) -> int:
        for row, entry in enumerate(self._entries):
            if (entry["kind"] == "track"
                    and entry["play_index"] == play_index):
                return row
        return -1

    def set_state(self, items: List[Dict], current_play_index: int,
                  show_artist: bool, drag_enabled: bool,
                  multi_disc: bool = False):
        """Replace the entire model state. ``multi_disc`` interleaves
        disc-divider entries between disc groups (detected via
        ParentIndexNumber)."""
        self.beginResetModel()
        new_entries: List[Dict] = []
        if multi_disc and items:
            disc_counts: Dict[int, int] = {}
            for t in items:
                d = int(t.get("ParentIndexNumber") or 1)
                disc_counts[d] = disc_counts.get(d, 0) + 1
            current_disc: Optional[int] = None
            for play_idx, t in enumerate(items):
                d = int(t.get("ParentIndexNumber") or 1)
                if d != current_disc:
                    new_entries.append({
                        "kind": "disc",
                        "disc_info": (d, disc_counts.get(d, 0)),
                    })
                    current_disc = d
                new_entries.append({
                    "kind": "track",
                    "item": t,
                    "play_index": play_idx,
                })
        else:
            for play_idx, t in enumerate(items):
                new_entries.append({
                    "kind": "track",
                    "item": t,
                    "play_index": play_idx,
                })
        self._entries = new_entries
        self._current_play_index = current_play_index
        self._show_artist = show_artist
        self._drag_enabled = drag_enabled
        self.endResetModel()

    def set_drag_state(self, active: bool, src_row: int = -1):
        """Toggle the global drag-in-progress state. The delegate
        suppresses hover wash on every row when active=True and ghosts
        out the source row (paints an empty placeholder slot)."""
        changed = (active != self._drag_active
                   or src_row != self._drag_src_row)
        self._drag_active = active
        self._drag_src_row = src_row
        if changed and self._entries:
            top = self.index(0, 0)
            bot = self.index(len(self._entries) - 1, 0)
            self.dataChanged.emit(
                top, bot, [self.IsDragGhostRole, self.SuppressHoverRole],
            )

    def set_current_play_index(self, idx: int):
        """Update the highlighted row without a full model reset.
        Emits dataChanged on the old + new rows so the view re-paints
        only those two."""
        if idx == self._current_play_index:
            return
        old = self._current_play_index
        self._current_play_index = idx
        for play_idx in (old, idx):
            if play_idx < 0:
                continue
            r = self.row_for_play_index(play_idx)
            if r >= 0:
                i = self.index(r, 0)
                self.dataChanged.emit(i, i, [self.IsCurrentRole])


class _TrackDelegate(QStyledItemDelegate):
    """Paints one entry in the track list — either a track row or a
    disc divider depending on KindRole. Track rows: index + title +
    (optional artist sub) + duration, with current-row accent on the
    title + index. Disc dividers: 'Disc N · M tracks' label + hairline."""

    TRACK_HEIGHT = 44
    DIVIDER_HEIGHT = 28
    IDX_W = 32
    DUR_W = 56
    LEFT_PAD = 12
    RIGHT_PAD = 12
    COL_GAP = 14

    def sizeHint(self, option, index):
        kind = index.data(_TracksModel.KindRole) or "track"
        h = self.DIVIDER_HEIGHT if kind == "disc" else self.TRACK_HEIGHT
        w = option.rect.width() if option.rect.width() > 0 else 200
        return QSize(w, h)

    def paint(self, painter, option, index):
        kind = index.data(_TracksModel.KindRole) or "track"
        if kind == "disc":
            self._paint_divider(painter, option, index)
        else:
            self._paint_track(painter, option, index)

    def _paint_divider(self, painter, option, index):
        info = index.data(_TracksModel.DiscInfoRole)
        if not info:
            return
        disc_num, count = info
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        text_font = QFont(painter.font())
        text_font.setPixelSize(TYPE_MICRO.size_px)
        text_font.setBold(True)
        painter.setFont(text_font)
        fm = QFontMetrics(text_font)
        label = f"Disc {disc_num}  ·  {count} tracks"
        label_w = fm.horizontalAdvance(label)
        label_rect = QRect(
            rect.x() + self.LEFT_PAD, rect.y() + 4,
            label_w + 4, rect.height() - 8,
        )
        # Inline RGBA — ui_helpers' TEXT_FAINT is a CSS rgba() string
        # (works in QSS but QColor doesn't parse it; passing it through
        # rendered as opaque black).
        painter.setPen(QColor(255, 255, 255, 140))
        painter.drawText(
            label_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )
        # Hairline from end of label text to the right edge.
        line_x = label_rect.right() + 8
        line_y = rect.center().y()
        painter.setPen(QColor(255, 255, 255, 20))
        painter.drawLine(
            QPoint(line_x, line_y),
            QPoint(rect.right() - self.RIGHT_PAD, line_y),
        )
        painter.restore()

    def _paint_track(self, painter, option, index):
        # If this row is the drag source, leave the slot empty (the
        # floating widget overhead is the visual stand-in). Return
        # before any paint so the slot reads as a clean gap.
        if bool(index.data(_TracksModel.IsDragGhostRole)):
            return
        item = index.data(_TracksModel.ItemRole)
        if not item:
            return
        is_current = bool(index.data(_TracksModel.IsCurrentRole))
        show_artist = bool(index.data(_TracksModel.ShowArtistRole))
        play_index = int(index.data(_TracksModel.PlayIndexRole) or 0)
        suppress_hover = bool(index.data(_TracksModel.SuppressHoverRole))
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Re-read theme constants every paint so live-accent flows
        # through with a viewport().update().
        from modules.ui_helpers import ACCENT as _ACCENT

        # Hover wash — subtle highlight when the cursor's over the
        # row. Suppressed while a drag is in flight so the rest of
        # the list reads as static while the user rearranges.
        if (not suppress_hover
                and option.state & QStyle.StateFlag.State_MouseOver):
            inset = rect.adjusted(self.LEFT_PAD - 4, 2,
                                  -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 6, 6)
            painter.fillPath(path, QColor(255, 255, 255, 10))

        # Index column — IndexNumber when present else play-position.
        idx_n = item.get("IndexNumber") or (play_index + 1)
        idx_rect = QRect(
            rect.x() + self.LEFT_PAD, rect.y(),
            self.IDX_W, rect.height(),
        )
        idx_font = QFont("JetBrains Mono")
        idx_font.setPixelSize(TYPE_CAPTION.size_px)
        idx_font.setBold(is_current)
        painter.setFont(idx_font)
        if is_current:
            painter.setPen(QColor(_ACCENT))
        else:
            painter.setPen(QColor(255, 255, 255, 115))
        painter.drawText(
            idx_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            str(idx_n),
        )

        # Title + duration column geometry.
        text_x = idx_rect.right() + self.COL_GAP
        dur_x = rect.right() - self.RIGHT_PAD - self.DUR_W
        text_w = max(0, dur_x - text_x - self.COL_GAP)

        title_font = QFont(painter.font())
        title_font.setPixelSize(TYPE_BODY.size_px)
        title_font.setBold(is_current)
        painter.setFont(title_font)
        fm_title = QFontMetrics(title_font)

        # Resolve subtitle text first so we know whether to split the
        # vertical space.
        sub = ""
        if show_artist:
            artists = item.get("Artists") or []
            sub = (", ".join(artists) if artists
                   else (item.get("AlbumArtist", "") or ""))
        if sub:
            title_h = 22
            sub_h = 14
            title_y = rect.y() + (rect.height() - (title_h + sub_h)) // 2
            sub_y = title_y + title_h
            title_rect = QRect(text_x, title_y, text_w, title_h)
            sub_rect = QRect(text_x, sub_y, text_w, sub_h)
        else:
            title_rect = QRect(text_x, rect.y(), text_w, rect.height())
            sub_rect = None

        title = item.get("Name") or "Unknown"
        if is_current:
            painter.setPen(QColor(_ACCENT))
        else:
            painter.setPen(QColor(255, 255, 255, 224))
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight, text_w),
        )

        if sub_rect is not None and sub:
            sub_font = QFont(painter.font())
            sub_font.setPixelSize(TYPE_TINY.size_px)
            sub_font.setBold(False)
            painter.setFont(sub_font)
            painter.setPen(QColor(255, 255, 255, 140))
            fm_sub = QFontMetrics(sub_font)
            painter.drawText(
                sub_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                fm_sub.elidedText(sub, Qt.TextElideMode.ElideRight, text_w),
            )

        # Duration column.
        dur_ticks = item.get("RunTimeTicks", 0) or 0
        if dur_ticks:
            dur_font = QFont("JetBrains Mono")
            dur_font.setPixelSize(TYPE_CAPTION.size_px)
            dur_font.setBold(False)
            painter.setFont(dur_font)
            painter.setPen(QColor(255, 255, 255, 140))
            dur_rect = QRect(dur_x, rect.y(), self.DUR_W, rect.height())
            painter.drawText(
                dur_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                fmt_duration_ticks(dur_ticks),
            )

        painter.restore()


class _TracksListView(QListView):
    """QListView for the NP page track list. Custom drag-reorder (no
    Qt InternalMove) gives us a horizontally-locked floating widget
    and an opaque accent-tinted drag card that Qt's QDrag framework
    can't produce. Click → ``track_clicked(play_index)``; drop →
    ``queue_move_item`` on the bus + model.move_track to keep the
    visual order in sync until the QueueManager re-renders."""

    track_clicked = Signal(int)
    track_context_menu = Signal(int, QPoint)
    drag_state_changed = Signal(bool)

    SHIFT_MS = 90

    def __init__(self, model: _TracksModel,
                 delegate: _TrackDelegate, parent=None):
        super().__init__(parent)
        self._model = model
        self._delegate = delegate
        self.setModel(model)
        self.setItemDelegate(delegate)
        self.setMouseTracking(True)
        # Heterogeneous heights (tracks vs disc dividers) — uniform
        # sizes can't be enabled.
        self.setUniformItemSizes(False)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # No selection — click = play, not select. Qt's InternalMove
        # drag isn't used (we drive the drag manually below) so we
        # don't need selectedIndexes() to be populated.
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Viewport flash fix.
        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self.setStyleSheet(
            "QListView { background: transparent; border: none; }"
            "QListView::item:focus { outline: none; }"
        )
        # Custom context menu — host wires Play next / Add to queue /
        # Remove from queue.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # Press-tracking + custom drag state.
        self._press_row = -1
        self._press_pos: Optional[QPoint] = None
        self._dragging: bool = False
        self._drag_src_row: int = -1
        self._drag_hover_slot: int = -1
        self._float_label: Optional[QLabel] = None
        # Drop indicator y-position (in viewport coords) and the row
        # the indicator sits ABOVE. Repainted via update() on the
        # viewport so the line follows the cursor.
        self._drop_line_y: int = -1

    # ── Drag state observability ──────────────────────────────────────

    def is_dragging(self) -> bool:
        return self._dragging

    # ── Mouse handling ────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(e.position().toPoint())
            if idx.isValid() and idx.data(_TracksModel.KindRole) == "track":
                self._press_row = idx.row()
                self._press_pos = e.position().toPoint()
            else:
                self._press_row = -1
                self._press_pos = None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging:
            # Drive the float widget + hover slot from the cursor's
            # viewport-coordinate y.
            pos = self.viewport().mapFromGlobal(e.globalPosition().toPoint())
            self._update_drag(pos)
            return
        # Possibly enter drag.
        if (self._press_row < 0 or self._press_pos is None
                or not (e.buttons() & Qt.MouseButton.LeftButton)):
            super().mouseMoveEvent(e)
            return
        if not self._model._drag_enabled:
            super().mouseMoveEvent(e)
            return
        from PySide6.QtWidgets import QApplication
        dist = (e.position().toPoint() - self._press_pos).manhattanLength()
        if dist < QApplication.startDragDistance():
            super().mouseMoveEvent(e)
            return
        # Threshold crossed — start custom drag.
        self._begin_drag(self._press_row)

    def mouseReleaseEvent(self, e):
        if self._dragging and e.button() == Qt.MouseButton.LeftButton:
            self._end_drag()
            return
        super().mouseReleaseEvent(e)
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._press_row < 0 or self._press_pos is None:
            return
        from PySide6.QtWidgets import QApplication
        dist = (e.position().toPoint() - self._press_pos).manhattanLength()
        if dist < QApplication.startDragDistance():
            play_idx = self._model.play_index_at(self._press_row)
            if play_idx >= 0:
                self.track_clicked.emit(play_idx)
        self._press_row = -1
        self._press_pos = None

    # ── Custom drag lifecycle ─────────────────────────────────────────

    def _begin_drag(self, src_row: int):
        rect = self.visualRect(self._model.index(src_row, 0))
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._dragging = True
        self._drag_src_row = src_row
        self._drag_hover_slot = src_row
        # Build the floating drag card — tinted snapshot of the source
        # row's painted content. The viewport.grab(rect) snapshot
        # already captures the delegate paint; tint it via overlay.
        card = self._make_drag_card(rect)
        self._float_label = QLabel(self.viewport())
        self._float_label.setPixmap(card)
        self._float_label.resize(card.size())
        self._float_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._float_label.show()
        # Ghost the source row + suppress hover on every row through
        # the model state (delegate honours the flags).
        self._model.set_drag_state(active=True, src_row=src_row)
        self.viewport().grabMouse()
        self.drag_state_changed.emit(True)
        # Place the card at the current cursor position.
        pos = self.viewport().mapFromGlobal(
            self.cursor().pos() if hasattr(self, "cursor") else QPoint(0, 0)
        )
        self._update_drag(pos)

    def _update_drag(self, viewport_pos: QPoint):
        if not self._dragging or self._float_label is None:
            return
        # Lock the float card to the row column at x=0 (viewport's
        # left edge — the row's natural x). Y tracks the cursor.
        h = self._float_label.height()
        y = viewport_pos.y() - h // 2
        # Clamp so the card doesn't slide off the top/bottom of the
        # viewport.
        max_y = max(0, self.viewport().height() - h)
        if y < 0:
            y = 0
        if y > max_y:
            y = max_y
        self._float_label.move(0, y)
        self._float_label.raise_()
        # Compute the hover slot — between which rows the drop would
        # land. The drop-line y is the midpoint between rows.
        slot, line_y = self._compute_hover_slot(viewport_pos.y())
        if slot != self._drag_hover_slot or line_y != self._drop_line_y:
            self._drag_hover_slot = slot
            self._drop_line_y = line_y
            self.viewport().update()

    def _end_drag(self):
        src_row = self._drag_src_row
        dest_row = self._drag_hover_slot
        # Tear down float + restore model state BEFORE committing the
        # move so the queue_changed re-render lands in a clean state.
        self.viewport().releaseMouse()
        if self._float_label is not None:
            self._float_label.hide()
            self._float_label.setParent(None)
            self._float_label.deleteLater()
            self._float_label = None
        self._model.set_drag_state(active=False, src_row=-1)
        self._dragging = False
        self._drag_src_row = -1
        self._drag_hover_slot = -1
        self._drop_line_y = -1
        self.viewport().update()
        self.drag_state_changed.emit(False)
        self._press_row = -1
        self._press_pos = None
        if src_row < 0 or dest_row < 0:
            return
        # Translate model rows → play indices for the QueueManager.
        src_play = self._model.play_index_of_entry(src_row)
        dest_play = self._model.dest_play_index_for(src_row, dest_row)
        if src_play < 0 or src_play == dest_play:
            return
        # Mutate the model in place so the view stays visually correct
        # until the QueueManager's queue_changed lands. move_track
        # rewrites play_indices so a subsequent drag references the
        # new layout.
        self._model.move_track(src_row, dest_row)
        from modules.player_state import PlayerBus
        bus = PlayerBus.get()
        bus.queue_move_item.emit(src_play, dest_play)
        # Drop-at-top = play that track. The dragged track is now at
        # play-index 0; track_jumped jumps playback there.
        if dest_play == 0:
            bus.track_jumped.emit(0)

    # ── Drop indicator math + paint ───────────────────────────────────

    def _compute_hover_slot(self, y: int) -> tuple:
        """Translate cursor y → (slot, drop_line_y). slot N means
        "land before row N". Returns slot=rowCount + y of the last
        row's bottom edge when y is past the last row."""
        rc = self._model.rowCount()
        if rc == 0:
            return 0, 0
        # Use visualRect of each track-eligible row to compute slot.
        # Stop at the first row whose midpoint is below y; that's the
        # slot.
        last_bottom = 0
        for row in range(rc):
            r = self.visualRect(self._model.index(row, 0))
            last_bottom = r.bottom()
            mid = r.center().y()
            if y < mid:
                return row, r.top()
        return rc, last_bottom + 1

    def paintEvent(self, e):
        super().paintEvent(e)
        # Paint the drop indicator line on top of the viewport.
        if not self._dragging or self._drop_line_y < 0:
            return
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            from modules.ui_helpers import ACCENT as _ACCENT
            pen = QPen(QColor(_ACCENT))
            pen.setWidth(2)
            painter.setPen(pen)
            y = self._drop_line_y
            x1 = self._delegate.LEFT_PAD
            x2 = self.viewport().width() - self._delegate.RIGHT_PAD
            painter.drawLine(x1, y, x2, y)
        finally:
            painter.end()

    def _make_drag_card(self, source_rect: QRect) -> QPixmap:
        """Render the source row through the delegate, then composite
        with an opaque dark base + accent wash so the floating card
        reads as "this row, lifted". Same logical size as the row."""
        from modules.ui_helpers import ACCENT as _ACCENT
        # Capture the row's painted pixels off the viewport. dpr-aware
        # via QWidget.grab.
        grabbed = self.viewport().grab(source_rect)
        if grabbed.isNull():
            grabbed = QPixmap(source_rect.size())
            grabbed.fill(Qt.GlobalColor.transparent)
        out = QPixmap(grabbed.size())
        out.setDevicePixelRatio(grabbed.devicePixelRatio() or 1.0)
        # Opaque dark base so rows beneath don't bleed through.
        out.fill(QColor(20, 22, 26, 255))
        from modules.theme import _hex_to_rgb
        try:
            r, g, b = _hex_to_rgb(_ACCENT)
        except Exception:
            r, g, b = (140, 80, 220)
        p = QPainter(out)
        try:
            # Light accent wash on top of the dark base.
            p.fillRect(out.rect(), QColor(r, g, b, 36))
            # The row's actual content on top so text stays crisp.
            p.drawPixmap(0, 0, grabbed)
        finally:
            p.end()
        return out

    # ── Context menu ──────────────────────────────────────────────────

    @Slot(QPoint)
    def _on_context_menu(self, pos: QPoint):
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        if idx.data(_TracksModel.KindRole) != "track":
            return
        play_idx = self._model.play_index_at(idx.row())
        if play_idx < 0:
            return
        global_pos = self.viewport().mapToGlobal(pos)
        self.track_context_menu.emit(play_idx, global_pos)


class _TrackRow(QFrame):
    """One track row in the right pane. Index, title (+ optional artist
    when not in an album-context view), duration. Clickable; emits the
    play-order index that should be jumped to."""
    clicked = Signal(int)

    # MIME format for drag-reorder. Carries the row's play-order index
    # as utf-8 text. Custom format keeps Qt's drop accept-policy from
    # confusing it with text/uri-list or text/plain drags.
    DRAG_MIME = "application/x-jellytoast-queue-row"

    def __init__(self, play_index: int, item: Dict, show_artist: bool,
                 parent=None, allow_drag: bool = True):
        super().__init__(parent)
        self._play_index = play_index
        self._item = dict(item) if item else {}
        self._is_current = False
        self._press_pos: QPoint | None = None
        # ``allow_drag=False`` skips the drag wiring entirely — used
        # in browse/preview mode where the user is looking at an album
        # track list, not the live queue. Right-click "Play next / Add
        # to queue" still works because the context menu is installed
        # below regardless.
        self._allow_drag = allow_drag
        # True while a press has crossed the drag threshold and the
        # row is in custom-drag mode (grabMouse held). Distinguishes
        # click vs drag at release time.
        self._dragging = False
        self.setObjectName("npTrackRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Comfortable row height — Apple Music's "Up Next" is ~44px,
        # which is the sweet spot between dense and breathable on a
        # frosted background where dividers vanish.
        self.setFixedHeight(44)
        # No dividers, no zebra — both fight the blur. Hover is a
        # subtle wash so the user knows the row is interactive.
        self.setStyleSheet("""
            QFrame#npTrackRow {
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QFrame#npTrackRow:hover { background: rgba(255, 255, 255, 0.04); }
            QFrame#npTrackRow QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(14)

        # Track number — IndexNumber when present, else play-position.
        # Tabular-nums via monospace family so 1/2/…/12 all line up
        # neatly in the column without per-glyph width drift.
        idx_n = item.get("IndexNumber") or (play_index + 1)
        self._idx = QLabel(str(idx_n))
        self._idx.setFixedWidth(32)
        self._idx.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._idx.setStyleSheet(self._idx_css(active=False))
        layout.addWidget(self._idx)

        # Title (+ optional artist subtitle for cross-artist queues).
        # Both labels elide on overflow rather than push the row wider —
        # otherwise long song titles (Sufjan, classical, …) would spawn
        # a horizontal scrollbar inside the right pane.
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        self._title = _ElidingLabel(item.get("Name", "Unknown"))
        self._title.setStyleSheet(self._title_css(active=False))
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        text_col.addWidget(self._title)
        if show_artist:
            artists = item.get("Artists") or []
            sub = ", ".join(artists) if artists else item.get("AlbumArtist", "")
            if sub:
                self._sub = _ElidingLabel(sub)
                self._sub.setStyleSheet(
                    f"color: rgba(255,255,255,0.55); {type_qss(TYPE_TINY)}"
                )
                text_col.addWidget(self._sub)
        layout.addLayout(text_col, 1)

        # Duration column — fixed width so all rows align on the right.
        dur_ticks = item.get("RunTimeTicks", 0) or 0
        self._dur = QLabel(fmt_duration_ticks(dur_ticks) if dur_ticks else "")
        self._dur.setFixedWidth(56)
        self._dur.setStyleSheet(
            f"color: rgba(255,255,255,0.55); {type_qss(TYPE_CAPTION)} "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )
        self._dur.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._dur)

        # Right-click → Play next / Add to queue / Remove from queue.
        # The remove action emits queue_remove_at against this row's
        # play-order index — captured at click time so it stays correct
        # even after the queue rerenders.
        install_song_context_menu(
            self,
            lambda: self._item,
            extra_actions=[(
                "Remove from queue",
                lambda: PlayerBus.get().queue_remove_at.emit(self._play_index),
            )],
        )

    @staticmethod
    def _idx_css(active: bool) -> str:
        # Re-import ACCENT at call time (rather than reading the
        # module-level snapshot at the top of this file) so live-accent
        # changes propagate when `NowPlayingPage._reapply_accent` walks
        # the rows and re-applies their styling.
        from modules.ui_helpers import ACCENT as _ACCENT
        if active:
            return (
                f"color: {_ACCENT}; {type_qss(TYPE_CAPTION)} font-weight: 700; "
                "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
            )
        return (
            f"color: rgba(255,255,255,0.45); {type_qss(TYPE_CAPTION)} "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )

    @staticmethod
    def _title_css(active: bool) -> str:
        from modules.ui_helpers import ACCENT as _ACCENT
        if active:
            return f"color: {_ACCENT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        return f"color: rgba(255,255,255,0.88); {type_qss(TYPE_BODY)}"

    # ── Custom drag (no QDrag) ──────────────────────────────────────────
    # We roll our own drag because QDrag.exec()'s modal event loop
    # was blocking QPropertyAnimation timers (rows wouldn't visibly
    # shift) and couldn't horizontally constrain the floating pixmap.
    # Instead: mousePress grabs press position, mouseMove triggers
    # grabMouse + drag state when the threshold is crossed, and
    # mouseRelease either fires `clicked` (no drag happened) or
    # commits the drop.

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
            self._dragging = False
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_pos is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(e)
        if not self._allow_drag:
            return super().mouseMoveEvent(e)
        host = self._queue_drop_target()
        if not self._dragging:
            from PySide6.QtWidgets import QApplication
            if ((e.position().toPoint() - self._press_pos).manhattanLength()
                    < QApplication.startDragDistance()):
                return
            # Threshold crossed — enter drag. grabMouse() routes ALL
            # subsequent mouse events to this widget regardless of
            # cursor position, so we can drive update_drag uniformly
            # via mouseMoveEvent below.
            self._dragging = True
            self.grabMouse()
            if host is not None:
                host.begin_drag(self._play_index, self.grab())
        # Drive the host's floating widget + slot animation from the
        # cursor's host-coordinate position. globalPosition is reliable
        # even when the cursor is outside the row.
        if host is not None:
            global_pos = e.globalPosition().toPoint()
            host_pos = host.mapFromGlobal(global_pos)
            host.update_drag(host_pos)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(e)
        if self._dragging:
            # Drop — translate the release point into host coords and
            # commit. releaseMouse first so subsequent UI interactions
            # (e.g. the np-page rebuild) get normal event routing back.
            self.releaseMouse()
            host = self._queue_drop_target()
            if host is not None:
                global_pos = e.globalPosition().toPoint()
                host_pos = host.mapFromGlobal(global_pos)
                host.end_drag(self._play_index, host_pos)
            self._dragging = False
            self._press_pos = None
            return
        if self._press_pos is not None:
            # Press → release without crossing threshold = plain click.
            self._press_pos = None
            self.clicked.emit(self._play_index)
            return
        super().mouseReleaseEvent(e)

    def _queue_drop_target(self):
        """Walk up to the parent _QueueDropTarget so the class lookup
        isn't resolved at class-body time."""
        p = self.parent()
        while p is not None:
            if p.__class__.__name__ == "_QueueDropTarget":
                return p
            p = p.parent()
        return None

    def set_current(self, is_current: bool):
        if is_current == self._is_current:
            return
        self._is_current = is_current
        self._reapply_styling()

    def _reapply_styling(self):
        """Re-stamp the title + index colour from the current
        ``_is_current`` state. Used by NowPlayingPage._reapply_accent
        to refresh the active-row accent after a Settings → Display
        accent change."""
        self._title.setStyleSheet(self._title_css(active=self._is_current))
        self._idx.setStyleSheet(self._idx_css(active=self._is_current))


def _make_drag_card(row_pix: QPixmap) -> QPixmap:
    """Tint the row's grabbed pixmap with a *subtle* accent so the
    floating chip reads as "the same row, lifted". Same logical size
    as the row — no resizing, no padding.

    ``row_pix.size()`` is the *physical* pixel size on HiDPI displays,
    so we copy it straight into the new pixmap and replicate the
    source DPR. Without that, the chip ended up at 2× the row's
    logical size on a 2× screen, which is what was reading as "huge".
    """
    if row_pix.isNull():
        return row_pix
    out = QPixmap(row_pix.size())
    out.setDevicePixelRatio(row_pix.devicePixelRatio() or 1.0)
    # Solid opaque fill so rows beneath don't bleed through the chip
    # as it slides over them. Color picked to match the np-page body
    # closely so the chip reads as the same panel surface.
    out.fill(QColor(20, 22, 26, 255))
    from modules.theme import get_active_theme, _hex_to_rgb
    accent = get_active_theme().accent
    r, g, b = _hex_to_rgb(accent)
    p = QPainter(out)
    try:
        # Light accent wash (~11% alpha) over the opaque base — just
        # enough to read as "this row is being moved" without making
        # the chip dominate the rest of the list.
        p.fillRect(out.rect(), QColor(r, g, b, 28))
        # Row content on top so text stays crisp + readable.
        p.drawPixmap(0, 0, row_pix)
    finally:
        p.end()
    return out


class _QueueDropTarget(QWidget):
    """Host widget for the queue's track rows that accepts drag-reorder
    drops. Rows start the drag (see ``_TrackRow``); this widget
    computes the destination play-order index from cursor y, animates
    sibling rows out of the way as the cursor moves, and emits
    ``bus.queue_move_item`` on drop. The QueueManager handles the
    actual reorder; the post-drop ``queue_changed`` re-renders the
    list, so the animation is purely a hover affordance — no
    reconciliation between the animated state and the final layout
    is needed.
    """

    SHIFT_MS = 90  # row-shift animation duration

    def __init__(self, parent=None):
        super().__init__(parent)
        # Custom drag — no setAcceptDrops/QDrag. _TrackRow drives us
        # directly via begin_drag/update_drag/end_drag from its mouse
        # handlers, with grabMouse routing all motion events to the
        # source row.
        self._drag_src_idx: int = -1
        self._drag_hover_slot: int = -1
        # Per-row {id(row): base QPoint} captured at drag start. Both
        # the offset animation and the cancelled-drop snap-back use
        # these as their targets, so animations don't fight whatever
        # state a row happened to be in mid-cycle.
        self._row_base_pos: dict[int, QPoint] = {}
        self._ghost_row: "_TrackRow | None" = None
        # Children of the ghost row that we hid on begin_drag. Storing
        # the list lets end_drag restore them even if the row was
        # rebuilt mid-drag (show() is no-op on already-visible widgets).
        self._ghost_hidden: list = []
        # Floating drag pixmap — a QLabel parented to this widget so
        # its x can be locked to the list column while y follows the
        # cursor. Tinted via _make_drag_card.
        self._float_label: "QLabel | None" = None
        # Set on commit (end_drag) so we know whether to animate rows
        # back (cancelled) or skip animation (queue_changed will
        # rebuild rows in the new order any moment).
        self._drag_dropped = False

    # ── Drag lifecycle (called by _TrackRow's mouse handlers) ──────

    def begin_drag(self, src_idx: int, row_pix):
        """Start a drag of row at ``src_idx``. ``row_pix`` is the
        source row's grabbed QPixmap — used to build the floating
        drag card via _make_drag_card."""
        rows = self._track_rows()
        if not (0 <= src_idx < len(rows)):
            return
        self._drag_src_idx = src_idx
        self._drag_hover_slot = src_idx
        self._drag_dropped = False
        # Snapshot each row's layout-assigned position before any
        # animation runs.
        self._row_base_pos = {id(r): r.pos() for r in rows}
        # Empty the source row's slot by hiding its visible children.
        # The row's outline (transparent frame) still occupies the
        # layout cell, so the slot reads as an empty gap.
        self._ghost_row = rows[src_idx]
        self._ghost_hidden = [
            child for child in self._ghost_row.findChildren(QWidget)
            if child.isVisible()
        ]
        for child in self._ghost_hidden:
            child.hide()
        # Suppress the :hover pseudo-state on the source row while
        # the drag is running — Qt keeps WA_UnderMouse active on the
        # grabMouse target, which would otherwise leave the row's
        # subtle hover highlight visible underneath the floating chip.
        self._ghost_original_style = self._ghost_row.styleSheet()
        self._ghost_row.setStyleSheet("""
            QFrame#npTrackRow {
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QFrame#npTrackRow QLabel { background: transparent; }
        """)
        # Build the floating drag card. Parented to this widget so x
        # can be locked to the list column (rows[0].x()); y is driven
        # by cursor in update_drag.
        if row_pix is not None and not row_pix.isNull():
            card = _make_drag_card(row_pix)
            self._float_label = QLabel(self)
            self._float_label.setPixmap(card)
            self._float_label.resize(card.size())
            self._float_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
            self._float_label.show()
            self._float_label.raise_()
        # Notify the np page so the kicker swaps to "QUEUE" the moment
        # a drag starts — the queue is conceptually no longer a
        # pristine source list from this point on.
        self._notify_drag_state(True)

    def update_drag(self, host_pos: QPoint):
        """Called on every mouseMoveEvent during drag. Updates the
        floating widget's y and the hover slot."""
        if self._drag_src_idx < 0:
            return
        # Position the float card: x locked to list column, y at cursor.
        if self._float_label is not None:
            rows = self._track_rows()
            target_x = rows[0].x() if rows else 0
            target_y = host_pos.y() - self._float_label.height() // 2
            self._float_label.move(target_x, target_y)
            self._float_label.raise_()
        # Update hover slot + offsets.
        slot = self._compute_hover_slot(host_pos.y())
        if slot != self._drag_hover_slot:
            self._drag_hover_slot = slot
            self._apply_offsets()

    def _notify_drag_state(self, dragging: bool):
        """Walk up to the parent NowPlayingPage and let it update
        any chrome that should reflect "drag in progress" — currently
        just the right-pane kicker."""
        p = self.parentWidget()
        while p is not None:
            if hasattr(p, "_on_drag_state_changed"):
                p._on_drag_state_changed(dragging)
                return
            p = p.parentWidget()

    def end_drag(self, src_idx: int, host_pos: QPoint):
        """Drop point reached. Commit the move, tear down ghost +
        float, and let queue_changed rebuild the rows in the new
        order."""
        if self._drag_src_idx < 0:
            return
        # Use the last hover slot rather than recomputing from the
        # release cursor — the user dropped where the visible gap
        # was, which is exactly what _drag_hover_slot tracks. Also
        # avoids cases where release happens a few px from the last
        # mouseMove and slot is sensitive to that drift.
        #
        # The hover slot is already in source-removed space (which is
        # what ``move_item``'s dest_play_idx expects), so no further
        # adjustment is needed — earlier code's ``dest_slot - 1 if
        # dest_slot > src else dest_slot`` was double-applying the
        # conversion and causing off-by-one drops when dragging down.
        dest_idx = self._drag_hover_slot
        if dest_idx < 0:
            dest_idx = self._compute_hover_slot(host_pos.y())
        moved = dest_idx != src_idx
        # Tear down ghost + float BEFORE emitting the move signal so
        # the np-page rebuild that follows queue_changed renders into
        # a clean state. Each step is wrapped in try because the row /
        # label could be dangling on rare paths (e.g. mid-drag widget
        # rebuild).
        for child in self._ghost_hidden:
            try:
                child.show()
            except RuntimeError:
                pass
        self._ghost_hidden = []
        # Restore the source row's :hover-enabled stylesheet.
        if self._ghost_row is not None:
            try:
                self._ghost_row.setStyleSheet(
                    getattr(self, "_ghost_original_style", "")
                )
            except RuntimeError:
                pass
        self._ghost_row = None
        try:
            if self._float_label is not None:
                self._float_label.hide()
                self._float_label.setParent(None)
                self._float_label.deleteLater()
        except RuntimeError:
            pass
        self._float_label = None
        # Flip the kicker back to its normal state — _refresh_track_list
        # picks the right label (ALBUM / PLAYLIST / QUEUE if modified).
        self._notify_drag_state(False)
        # Snap rows back to base positions if the drop was a no-op
        # (no move); otherwise queue_changed will rebuild rows fresh.
        if moved:
            self._drag_dropped = True
            for row in self._track_rows():
                prior = row.findChild(QPropertyAnimation, "queueRowAnim")
                if prior is not None:
                    prior.stop()
                    prior.deleteLater()
        else:
            for row in self._track_rows():
                base = self._row_base_pos.get(id(row))
                if base is not None:
                    self._animate_row(row, base.y())
        # Commit.
        bus = PlayerBus.get()
        if moved:
            bus.queue_move_item.emit(src_idx, dest_idx)
        # Drop-at-top → play that track. After the move (or if it was
        # already in slot 0), the dragged track is at play-order
        # index 0; track_jumped jumps playback there.
        if dest_idx == 0:
            bus.track_jumped.emit(0)
        # Reset state.
        self._drag_src_idx = -1
        self._drag_hover_slot = -1
        self._row_base_pos = {}
        self._drag_dropped = False
        # Flush any queue_changed / queue_context_changed that fired
        # during the drag (deferred so the drag's call stack fully
        # unwinds first — re-rendering inline would race with any
        # pending row-animation cleanup).
        QTimer.singleShot(0, self._flush_np_refresh)

    def _flush_np_refresh(self):
        """Walk up to the parent NowPlayingPage and have it replay any
        queue refresh that was deferred during the drag."""
        p = self.parentWidget()
        while p is not None:
            if hasattr(p, "_flush_pending_refresh"):
                p._flush_pending_refresh()
                return
            p = p.parentWidget()

    def _track_rows(self) -> list:
        rows = [c for c in self.children() if isinstance(c, _TrackRow)]
        if self._row_base_pos:
            # During an active drag, sort by the *captured base*
            # position (not current animated y). Otherwise animations
            # that bring two rows to the same y cause the sort to
            # interleave them — ``enumerate(rows)`` then maps i to
            # the wrong layout slot, and subsequent _apply_offsets
            # cycles skip / animate the wrong row. Base-position sort
            # keeps i ↔ original-slot stable for the lifetime of the
            # drag.
            def _base_y(r):
                base = self._row_base_pos.get(id(r))
                return base.y() if base is not None else r.geometry().top()
            rows.sort(key=_base_y)
        else:
            rows.sort(key=lambda r: r.geometry().top())
        return rows

    def _compute_hover_slot(self, y: int) -> int:
        """Translate cursor y (in this widget's coordinates) to a slot
        index — slot N means "land before row N". Final slot (after
        last row) is len(rows).

        While a drag is active, midpoint thresholds use the *base*
        (unanimated) row positions so the slot trigger stays under the
        user's cursor as rows shift around — otherwise the threshold
        would slide with the animation and the user would have to
        chase it. Outside a drag we fall through to live geometry.
        """
        rows = self._track_rows()
        if not rows:
            return 0
        row_h = rows[0].height() or 44
        if self._row_base_pos:
            # Use the captured first-row y as the anchor; subsequent
            # midpoints are first_y + i*row_h + row_h/2. Indexing
            # matches _track_rows() since both reflect layout order at
            # drag-start (no add/remove during a drag).
            first_base = self._row_base_pos.get(id(rows[0]))
            first_y = (first_base.y() if first_base is not None
                       else rows[0].geometry().top())
            slot = len(rows)
            for i in range(len(rows)):
                mid = first_y + i * row_h + row_h // 2
                if y < mid:
                    slot = i
                    break
            return slot
        slot = len(rows)
        for i, row in enumerate(rows):
            mid = row.geometry().top() + row.geometry().height() // 2
            if y < mid:
                slot = i
                break
        return slot

    def _apply_offsets(self):
        """Animate each row to its target y given the current drag
        state. Conceptually: remove the source row from the list, then
        insert a gap at ``hover_slot``. Each remaining row computes its
        new slot in this "with-gap" layout and animates into position,
        producing a single consistent gap that always tracks the
        cursor — including when hovering the source's own slot, since
        the source is hidden (opacity 0) and its slot reads as the gap.
        """
        if self._drag_src_idx < 0:
            return
        rows = self._track_rows()
        if not rows:
            return
        row_h = rows[0].height() or 44
        src = self._drag_src_idx
        hover = self._drag_hover_slot
        first_base = self._row_base_pos.get(id(rows[0]))
        first_y = first_base.y() if first_base is not None else 0
        for i, row in enumerate(rows):
            if i == src:
                continue  # source is hidden via opacity; keep at base
            # Effective slot in the "source-removed" list — rows above
            # src keep their index, rows below shift up by 1.
            eff_slot = i if i < src else i - 1
            # Slot >= hover gets pushed down by 1 to make room for the
            # gap; slot < hover stays put.
            final_slot = eff_slot + 1 if eff_slot >= hover else eff_slot
            target_y = first_y + final_slot * row_h
            self._animate_row(row, target_y)

    def _animate_row(self, row, target_y: int):
        if row.y() == target_y:
            return
        # Stop any prior animation on this row before starting a new
        # one so we always interpolate from the current position.
        prior = row.findChild(QPropertyAnimation, "queueRowAnim")
        if prior is not None:
            prior.stop()
            prior.deleteLater()
        anim = QPropertyAnimation(row, b"pos", row)
        anim.setObjectName("queueRowAnim")
        anim.setDuration(self.SHIFT_MS)
        anim.setStartValue(row.pos())
        anim.setEndValue(QPoint(row.x(), target_y))
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.start()


class _DiscDivider(QWidget):
    """Hairline + 'Disc N · M tracks' kicker between disc groups in a
    multi-disc album. Hidden chrome — easy to overlook in a 12-track
    single-disc release, deliberate signpost in a boxset."""

    def __init__(self, disc: int, count: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 4)
        layout.setSpacing(10)
        label = QLabel(f"Disc {disc}  ·  {count} tracks")
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT}; background: transparent;")
        layout.addWidget(label)
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            "background: rgba(255,255,255,0.08); border: none;"
        )
        rule.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(rule, 1)


class NowPlayingPage(QWidget):
    """Full-screen now-playing view. Owned by JellyToastWindow; swapped
    into the content stack when the user clicks the now-playing pill
    in the transport bar."""

    # Emitted when the user wants to dismiss the page (back button).
    dismiss_requested = Signal()
    # Emitted whenever the page enters / leaves preview mode. The host
    # uses this to keep the bottom-transport-bar's left cluster (cover +
    # title + artist + heart) visible while the user browses (so the
    # currently-playing track stays surfaced) and hide it again when
    # the page returns to live mode (the page itself displays the
    # active track in large).
    preview_changed = Signal(bool)  # True = entering preview, False = leaving
    # Internal — fires from the lyrics worker thread; the auto-routed
    # queued connection delivers it on the main thread so we can touch
    # widgets safely. Without this we'd be calling QTimer.singleShot
    # from a thread that has no event loop and the callback would never
    # fire.
    _lyrics_loaded = Signal(str, object)
    # Async preview-fetch results land on the GUI thread via these.
    _preview_meta_loaded = Signal(str, object)    # (preview_id, meta or None)
    _preview_tracks_loaded = Signal(str, object)  # (preview_id, list or None)

    # Panes split 50/50; cover sits at the top of the left pane and the
    # lyrics column owns the visual weight underneath. Apple Music's
    # macOS lyrics view is the reference — the cover anchors, lyrics
    # are the focal point.
    COVER_SIZE = 200          # square art

    def __init__(self, queue_mgr, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_provider()
        self.queue_mgr = queue_mgr
        self._lyrics_cache = _LyricsCache()
        self._lyrics_loading_for: str = ""  # in-flight item_id
        self._cover_orig: Optional[QPixmap] = None
        self._displayed_items_kind: str = ""  # "source" | "play"

        # Preview mode — when set, the page browses an album/playlist
        # without taking over the live queue. Click Play (or any track)
        # to install + play, which transitions back to live mode.
        # _preview_kind drives the right fetch endpoint and the
        # QueueKind installed when the user converts preview to live.
        self._preview_id: str = ""
        # Set by _on_queue_changed / _on_context_changed when a drag
        # is in flight; flushed by the drop target's end_drag hook so
        # the post-drop rerender happens cleanly outside the drag's
        # event loop.
        self._refresh_pending: bool = False
        self._preview_kind: QueueKind = QueueKind.ALBUM
        self._preview_meta: Dict = {}
        self._preview_tracks: List[Dict] = []

        # Lyrics visibility toggle. Default ON in live mode (auto-fetched
        # for the active track); forced OFF in preview mode (you're
        # browsing, not listening). The user can flip the toggle either
        # way; we remember the live-mode preference across preview trips.
        self._show_lyrics: bool = True

        # Auto-scroll vs user-scroll detection for the lyrics pane. The
        # "Live" pill button appears when the user has manually scrolled
        # away from the active line; clicking it re-snaps. The flag is
        # raised before each programmatic scroll and lowered when the
        # animation finishes — valueChanged callbacks check it to tell
        # which kind of scroll fired the signal.
        self._lyric_scroll_is_auto: bool = False
        self._user_off_live: bool = False

        # Synced lyrics state. `_lyrics_lines` parallels `_lyrics_widgets`
        # 1:1 — each entry is the line's start in *milliseconds* (0 for
        # unsynced lines). `_lyrics_starts_ms` is the same starts list,
        # cached because bisect over a list-of-tuples is awkward; we
        # search this and use the index into `_lyrics_widgets` to
        # highlight. `_active_line_idx` is the most recently highlighted
        # entry — the position-update handler bails early when the
        # active line hasn't changed, so 4Hz position pings don't cause
        # 4Hz repaints.
        self._lyrics_widgets: List[QLabel] = []
        self._lyrics_starts_ms: List[int] = []
        self._lyrics_synced: bool = False
        self._active_line_idx: int = -1

        self.setObjectName("npPage")
        # The host window paints its translucent body (with KWin blur
        # behind it) underneath; we want the frosted look to continue
        # all the way through this page. The descendant rule clears the
        # opaque QWidget background that GLOBAL_STYLE paints on every
        # QWidget so panes, scroll areas, labels, and frames let the
        # body show through. Per-widget styles on QPushButton / QSlider
        # / QScrollBar still take precedence because they're more
        # specific than this descendant selector.
        #
        # Scrollbar override: GLOBAL_STYLE colors the handle in the
        # accent (Jellyfin blue / purple, depending on theme). On this
        # page we want a quiet dim-white track that recedes when the
        # user isn't actively scrolling — the scrollbar isn't part of
        # the visual story here, it's a fallback affordance. Hover
        # state brightens it so it's still discoverable.
        self.setStyleSheet("""
            QWidget#npPage,
            QWidget#npPage QWidget,
            QWidget#npPage QFrame,
            QWidget#npPage QLabel,
            QWidget#npPage QScrollArea,
            QWidget#npPage QScrollArea > QWidget,
            QWidget#npPage QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QWidget#npPage QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 4px 2px 4px 0;
                border: none;
            }
            QWidget#npPage QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.12);
                border-radius: 3px;
                min-height: 28px;
            }
            QWidget#npPage QScrollBar::handle:vertical:hover,
            QWidget#npPage QScrollBar::handle:vertical:pressed {
                background: rgba(255, 255, 255, 0.32);
            }
            QWidget#npPage QScrollBar::add-line:vertical,
            QWidget#npPage QScrollBar::sub-line:vertical {
                height: 0;
                background: transparent;
                border: none;
            }
            QWidget#npPage QScrollBar::add-page:vertical,
            QWidget#npPage QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QWidget#npPage QScrollBar:horizontal { height: 0; }
        """)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.setSpacing(20)

        # Equal stretch factors give the panes a 50/50 split. The cover
        # column gets pushed left and the track listing gets meaningfully
        # more horizontal room, so longer track titles stop truncating.
        outer.addWidget(self._build_left_pane(), 1)
        outer.addWidget(self._build_right_pane(), 1)

        self._connect_bus()
        # Render whatever's currently playing the first time the page
        # is shown — caller may already have a queue installed.
        self._refresh_now_playing(get_now_playing())
        self._refresh_track_list()
        # Initial chrome state: hide the lyrics toggle (no lyrics yet),
        # the Live button, and the preview-only Play CTA.
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()
        self._update_cta_visibility()

        # Auto-hide scrollbars on both panes — they appear dim white on
        # scroll/hover and fade out after ~1s idle. The track-list
        # view (now a QListView, not a QScrollArea) uses the shared
        # install_autofade_scrollbars helper from ui_helpers instead
        # — same fade behavior, applied at view-construction time.
        self._lyrics_fader = _ScrollbarFader(self._lyrics_scroll)

    # ── Left pane (cover + metadata + lyrics) ───────────────────────────────

    def _build_left_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Back button — top-left, ghost.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._back_btn = QPushButton()
        self._back_btn.setIcon(icon("back"))
        self._back_btn.setIconSize(QSize(18, 18))
        self._back_btn.setFixedSize(34, 30)
        self._back_btn.setToolTip("Back to library")
        self._back_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.08); }
        """)
        self._back_btn.clicked.connect(self.dismiss_requested.emit)
        header.addWidget(self._back_btn)
        header.addStretch(1)
        v.addLayout(header)

        # Cover — square, top-aligned, soft drop-shadow. The shadow is
        # what reads as "this is a real album object" against the frosted
        # body; without it the cover looks flat-pasted.
        v.addSpacing(20)
        self._cover = QLabel()
        self._cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet("""
            background: rgba(255, 255, 255, 0.04);
            border-radius: 6px;
        """)
        shadow = QGraphicsDropShadowEffect(self._cover)
        shadow.setBlurRadius(32)
        shadow.setColor(QColor(0, 0, 0, 115))  # ≈ rgba(0,0,0,0.45)
        shadow.setOffset(0, 12)
        self._cover.setGraphicsEffect(shadow)
        cover_row = QHBoxLayout()
        cover_row.addStretch(1)
        cover_row.addWidget(self._cover)
        cover_row.addStretch(1)
        v.addLayout(cover_row)
        v.addSpacing(20)

        # Lyrics own the moment; title is the label.
        # Pin title and subtitle to their natural height — without this
        # QLabel's default Preferred vertical policy lets them grow into
        # any unclaimed space (e.g. when lyrics are hidden), pulling
        # them away from the cover and away from the CTAs below them.
        self._title = QLabel("Nothing playing")
        self._title.setFont(font(TYPE_TITLE))
        self._title.setStyleSheet("color: rgba(255, 255, 255, 0.95);")
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        v.addWidget(self._title)
        v.addSpacing(4)

        self._subtitle = QLabel("")
        self._subtitle.setFont(font(TYPE_CAPTION))
        self._subtitle.setStyleSheet("color: rgba(255, 255, 255, 0.62);")
        self._subtitle.setTextFormat(Qt.TextFormat.RichText)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._subtitle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        v.addWidget(self._subtitle)

        # Tertiary line under the subtitle — track count + total
        # runtime, only shown in preview mode where the page
        # represents a whole album / playlist rather than a single
        # active track. Uses the MICRO tier with all-caps + a wider
        # letter-spacing so it reads as metadata rather than a
        # header.
        self._meta_line = QLabel("")
        self._meta_line.setFont(font(TYPE_MICRO))
        self._meta_line.setStyleSheet(
            "color: rgba(255, 255, 255, 0.42); letter-spacing: 0.6px;"
        )
        self._meta_line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._meta_line.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        self._meta_line.setVisible(False)
        v.addWidget(self._meta_line)
        v.addSpacing(SPACE_MD)

        # ── CTAs ────────────────────────────────────────────────────────
        # Heart always visible. Play button visible *only in preview
        # mode* — clicking it installs the previewed album as the live
        # queue and starts playback (the page transitions back to live
        # mode automatically on playback_started). In live mode there's
        # no Play here — the bottom transport bar already plays.
        cta_row = QHBoxLayout()
        cta_row.setSpacing(SPACE_MD)
        cta_row.setContentsMargins(0, 0, 0, 0)
        cta_row.addStretch(1)

        self._play_cta = QPushButton(" Play")
        self._play_cta.setIcon(icon("play"))
        self._play_cta.setIconSize(QSize(16, 16))
        self._play_cta.setStyleSheet(button_qss(BTN_PRIMARY))
        self._play_cta.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_cta.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_cta.clicked.connect(self._on_play_preview)
        self._play_cta.hide()  # shown by _update_cta_visibility in preview mode
        cta_row.addWidget(self._play_cta)

        self._fav_cta = self._cta_icon_btn("favorite_outline", "")
        self._fav_cta.clicked.connect(self._on_favorite_cta)
        cta_row.addWidget(self._fav_cta)

        cta_row.addStretch(1)
        v.addLayout(cta_row)
        # Tight spacing under the heart so more lyrics fit when the
        # window is shrunk down to its minimum width.
        v.addSpacing(SPACE_SM)

        # ── Lyrics toggle row ───────────────────────────────────────────
        # Small text button right-above the lyrics scroll. Hidden in
        # preview mode (lyrics aren't relevant when not listening) and
        # when the active track has no lyrics at all.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(SPACE_LG, 0, SPACE_LG, 0)
        toggle_row.setSpacing(0)
        toggle_row.addStretch(1)
        self._lyrics_toggle_btn = QPushButton("Hide lyrics")
        self._lyrics_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lyrics_toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lyrics_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_FAINT};
                border: none; padding: 4px 8px;
                {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        self._lyrics_toggle_btn.clicked.connect(self._toggle_lyrics)
        toggle_row.addWidget(self._lyrics_toggle_btn)
        v.addLayout(toggle_row)

        # Live button row — sits just under the lyrics toggle, same
        # subtle styling so the two read as a stacked control cluster.
        # Visible only when the user has manually scrolled away from
        # the auto-tracked active line; click → re-snap.
        live_row = QHBoxLayout()
        live_row.setContentsMargins(SPACE_LG, 0, SPACE_LG, 0)
        live_row.setSpacing(0)
        live_row.addStretch(1)
        self._live_btn = QPushButton("● Live")
        self._live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._live_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._live_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_FAINT};
                border: none; padding: 4px 8px;
                {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        self._live_btn.clicked.connect(self._resnap_to_live)
        self._live_btn.hide()
        live_row.addWidget(self._live_btn)
        v.addLayout(live_row)

        # Lyrics scroll area — fills the remaining vertical space.
        self._lyrics_scroll = QScrollArea(self)
        self._lyrics_scroll.setWidgetResizable(True)
        self._lyrics_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._lyrics_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._lyrics_scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self._lyrics_container = QWidget()
        self._lyrics_container.setStyleSheet("background: transparent;")
        self._lyrics_layout = QVBoxLayout(self._lyrics_container)
        # Generous left padding so the active line breathes; we
        # left-align the lyrics like Apple Music macOS rather than
        # center, which reads better as verse on a wide pane.
        self._lyrics_layout.setContentsMargins(24, 8, 24, 24)
        self._lyrics_layout.setSpacing(0)
        self._lyrics_layout.addStretch(1)
        self._lyrics_scroll.setWidget(self._lyrics_container)
        # High stretch so the lyrics scroll dominates available vertical
        # space when visible, plus a low-stretch trailing absorber that
        # claims the leftover when lyrics is hidden. This keeps the
        # widgets above (cover, title, subtitle, CTAs, toggle, live)
        # at stable y-positions across toggle — without the trailing
        # stretch, hiding the lyrics removes the only stretch claimer
        # and Qt redistributes the leftover space among the remaining
        # widgets, sliding everything around.
        v.addWidget(self._lyrics_scroll, 100)
        v.addStretch(1)

        # Smooth-scroll animation on the lyrics scrollbar — used by the
        # synced-lyrics auto-scroll. 300ms ease-out per the design pass.
        self._lyrics_anim = QPropertyAnimation(
            self._lyrics_scroll.verticalScrollBar(), b"value", self
        )
        self._lyrics_anim.setDuration(300)
        self._lyrics_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # When the smooth scroll finishes, drop the auto-scroll flag so
        # the next valueChanged event is correctly attributed to the user.
        self._lyrics_anim.finished.connect(
            lambda: setattr(self, "_lyric_scroll_is_auto", False)
        )
        # Watch the scrollbar to detect manual user scrolls — if the
        # user grabs the bar (or wheels in the viewport), we surface the
        # "Live" button so they can re-snap to the active line.
        self._lyrics_scroll.verticalScrollBar().valueChanged.connect(
            self._on_lyrics_scrolled
        )

        return pane

    def _cta_icon_btn(self, name: str, tooltip: str) -> QPushButton:
        # Bare icon — no circle outline, no fill. Subtle hover wash for
        # affordance. Reads as a caption-row action under the title
        # rather than a primary CTA surface.
        b = QPushButton()
        b.setIcon(icon(name))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(32, 32)
        b.setToolTip(tooltip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; border-radius: 8px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.08); }
            QPushButton:pressed { background: rgba(255, 255, 255, 0.14); }
        """)
        return b

    # ── Right pane (track list / queue) ─────────────────────────────────────

    def _build_right_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Single small ALL-CAPS kicker. The left pane already carries
        # the title/artist; a redundant big "Album / 19" header on the
        # right just clutters. The kicker tells the user *what kind* of
        # context they're looking at and what its source is — see the
        # text built in _refresh_track_list().
        # type_qss(TYPE_MICRO) (rather than font(TYPE_MICRO)) so that the
        # kind/source-label concatenation in _refresh_track_list ("ALBUM ·
        # Currents") keeps its mixed casing — QFont's AllUppercase would
        # force-uppercase the source label too.
        self._right_kicker = QLabel("UP NEXT")
        # Bumped from TYPE_MICRO (11px) to 13px bold so the kicker
        # reads as a real heading at glance distance, brighter color
        # (0.55 → 0.78) so it doesn't disappear against the frosted
        # background. Letter-spacing stays out of QSS — Qt stylesheets
        # ignore that property; the all-caps source strings carry the
        # visual rhythm without it.
        self._right_kicker.setStyleSheet(
            f"color: rgba(255,255,255,0.78); "
            f"{type_qss(TYPE_BODY)} font-weight: 700;"
        )
        # Left-align with the row's title column. _TrackRow's layout
        # is contentsMargins(12, 0, 12, 0) + 32 wide index + 14 spacing
        # = 58px from the row's left edge to the title text. Match that
        # so the kicker sits directly above where titles start, not
        # centered above the whole pane.
        self._right_kicker.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._right_kicker.setContentsMargins(58, 4, 12, 0)
        v.addWidget(self._right_kicker)
        v.addSpacing(16)

        # Track list — model/view/delegate. The view's own scroll bar
        # replaces the legacy QScrollArea wrapper; install_autofade_
        # scrollbars handles the fade behavior. _list_container is
        # kept as an attribute name (== the view) so existing call
        # sites that reference it stay valid.
        from modules.ui_helpers import install_autofade_scrollbars
        self._tracks_model = _TracksModel(self)
        self._tracks_delegate = _TrackDelegate(self)
        self._list_container = _TracksListView(
            self._tracks_model, self._tracks_delegate, self,
        )
        install_autofade_scrollbars(self._list_container)
        # Click on a row → jump to that play index.
        self._list_container.track_clicked.connect(
            self._on_row_clicked
        )
        # Drag start/end → flip the kicker to "QUEUE" during drag.
        self._list_container.drag_state_changed.connect(
            self._on_drag_state_changed
        )
        # Right-click → context menu (Play next / Add to queue /
        # Remove from queue).
        self._list_container.track_context_menu.connect(
            self._on_track_context_menu
        )
        # Live-accent: delegate re-reads ACCENT on every paint, so a
        # theme change just needs viewport().update().
        self.bus.theme_changed.connect(
            self._list_container.viewport().update
        )
        v.addWidget(self._list_container, 1)

        return pane

    # ── Bus wiring ──────────────────────────────────────────────────────────

    def _connect_bus(self):
        self.bus.playback_started.connect(self._on_playback_started)
        self.bus.playback_stopped.connect(self._on_playback_stopped)
        self.bus.queue_changed.connect(self._on_queue_changed)
        self.bus.queue_context_changed.connect(self._on_context_changed)
        self.bus.position_updated.connect(self._on_position_updated)
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        self.bus.lyrics_font_size_changed.connect(self._on_lyrics_font_size_changed)
        # Cover-art prefetch for the next-up track — same pattern as
        # the bar / mini player. See feedback_now_playing_cover_pipeline.
        self.bus.queue_prefetch_request.connect(self._prefetch_cover)
        # Live-accent: walk every visible track row and refresh the
        # active-row tint, plus restamp the heart CTA from current
        # state. The track-row CSS staticmethods now re-read ACCENT
        # at call time so a fresh _reapply_styling() picks up the new
        # colour.
        self.bus.theme_changed.connect(self._reapply_accent)
        # Cross-DPR cover refresh — re-issue the main cover load when
        # the user moves the window to a different-scale monitor so
        # the result is sized for the new physical target.
        self.bus.dpr_changed.connect(self._on_dpr_changed)
        # Internal async-result signals — preview meta / tracks land on
        # the GUI thread via these. Wired here (one-time) so the slots
        # are connected before the first load_preview() can fire.
        self._lyrics_loaded.connect(self._on_lyrics_loaded)
        self._preview_meta_loaded.connect(self._on_preview_meta_loaded)
        self._preview_tracks_loaded.connect(self._on_preview_tracks_loaded)

    def _on_dpr_changed(self):
        # If we're showing a preview, re-fire the preview cover load;
        # otherwise re-fire the live now-playing cover load. Either
        # path goes through load_image_async at the new physical
        # target so the resulting pixmap is correctly sized.
        if self._preview_id:
            # _load_preview holds the meta + fires the cover load;
            # we reuse the simpler approach of re-calling it with
            # the current preview id.
            self.load_preview(self._preview_id, kind="album")
            return
        np = get_now_playing()
        if np.item_id:
            self._refresh_now_playing(np)

    def _reapply_accent(self):
        # Track rows — the delegate re-reads ACCENT on every paint, so
        # a viewport invalidate is enough to pick up a new accent.
        # (Wired in __init__ via bus.theme_changed → viewport().update,
        # so this is a no-op for tracks; the heart-CTA refresh below
        # is the load-bearing part of this handler.)
        # Heart CTA — infer current favourite state from app state
        # (preview meta if in preview mode, otherwise NowPlaying).
        if self._preview_id and self._preview_meta is not None:
            cur_fav = bool(
                self._preview_meta.get("UserData", {}).get("IsFavorite", False)
            )
        else:
            np = get_now_playing()
            cur_fav = bool(np.is_favorite)
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if cur_fav else icon("favorite_outline")
        )

    @Slot(object)
    def _prefetch_cover(self, np):
        if np is None:
            return
        image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
        if not image_id:
            return
        # HiDPI: target physical pixels for the cache + rounded radius
        # so the live load (which uses identical keying) lands on the
        # same slot. _on_cover_loaded tags the result with DPR.
        dpr = screen_dpr(self)
        target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
        radius_phys = int(round(12 * dpr))
        server_px = max(512, target_phys)
        url = self.api.get_image_url(image_id, "Primary", server_px)
        if not url:
            return
        load_image_async(
            f"{image_id}|nppage", url,
            target_phys, target_phys,
            lambda _pix: None, rounded_radius=radius_phys,
            on_error=lambda: None,
        )

    @Slot(str)
    def _on_lyrics_font_size_changed(self, _key: str):
        # Restyle every existing line with the new tier so the change is
        # visible immediately, no track skip required.
        for i, w in enumerate(self._lyrics_widgets):
            w.setStyleSheet(self._lyric_line_css(abs(i - self._active_line_idx)))
        # Re-snap so the active line lands at its proper anchor under
        # the new line spacing.
        if 0 <= self._active_line_idx < len(self._lyrics_widgets):
            self._scroll_to_active_lyric(self._active_line_idx)

    @Slot(object)
    def _on_playback_started(self, np: NowPlaying):
        # In preview mode the page is showing a different album — only
        # update the now-playing data when we're in live mode.
        if self._preview_id:
            return
        self._refresh_now_playing(np)

    @Slot()
    def _on_playback_stopped(self):
        if self._preview_id:
            return
        self._title.setText("Nothing playing")
        self._subtitle.setText("")
        self._cover.clear()
        self._cover_orig = None
        self._set_lyrics_text("")

    def _flush_pending_refresh(self):
        """Called by the queue drop target after a drag ends. Replays
        a queue_changed refresh that we deferred so the source row
        wouldn't get deleted mid-drag."""
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self._preview_id:
            return
        self._refresh_track_list()

    def _on_drag_state_changed(self, dragging: bool):
        """Called by the track list view on begin/end drag. The right-
        pane kicker should read "QUEUE" the moment the user starts
        dragging — even before the drop completes — because a drag-in-
        progress conceptually breaks the source ordering. On drag end
        the regular logic in _refresh_track_list picks the correct
        label (ALBUM / PLAYLIST / QUEUE if modified)."""
        if dragging:
            self._right_kicker.setText("QUEUE")
        elif not self._preview_id:
            # Drop the deferred-refresh flag — _refresh_track_list
            # always rebuilds from current state so the pending bit
            # is moot once we've ticked through it.
            self._refresh_pending = False
            self._refresh_track_list()

    @Slot(list, int)
    def _on_queue_changed(self, _items: list, _index: int):
        # Preview mode is browsing a different list — ignore live-queue
        # mutations until the user exits preview.
        if self._preview_id:
            return
        # Defer if a drag is in flight. ``move_item`` runs synchronously
        # inside ``dropEvent`` and re-emits ``queue_changed`` before
        # the drag fully unwinds — re-rendering here would delete the
        # source row mid-drag, stranding our _ghost_row reference and
        # leaving the new rows in an inconsistent state. The drop
        # target replays the refresh on end_drag.
        if self._list_container.is_dragging():
            self._refresh_pending = True
            return
        self._refresh_track_list()

    @Slot(object)
    def _on_context_changed(self, _ctx: QueueContext):
        if self._preview_id:
            return
        if self._list_container.is_dragging():
            self._refresh_pending = True
            return
        self._refresh_track_list()

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        # Sync the heart icon when the live queue's source (album /
        # playlist) is favorited from another client (a phone app,
        # Jellyfin Web in a browser, another machine).
        target = self._preview_id or self.queue_mgr.context.source_id
        if item_id == target:
            self._fav_cta.setIcon(
                accent_icon("favorite_filled") if fav else icon("favorite_outline")
            )

    # ── Updaters ────────────────────────────────────────────────────────────

    def _refresh_now_playing(self, np: NowPlaying):
        if not np.item_id:
            return
        self._title.setText(np.title or "Unknown")
        bits = []
        if np.subtitle:
            bits.append(np.subtitle)
        if np.album:
            bits.append(np.album)
        # Render the bullet at lower opacity so the eye reads "Artist · Album"
        # as a single phrase. setTextFormat(RichText) is set in _build.
        if bits:
            sep = '<span style="color: rgba(255,255,255,0.40);"> · </span>'
            self._subtitle.setText(sep.join(bits))
        else:
            self._subtitle.setText("")

        image_id = np.image_id or np.item_id
        if image_id:
            # Build our own URL at the page's target size — see the
            # bar's _on_started for why we don't reuse np.thumb_url.
            # 512 covers a 200-logical cover at 2× DPR with headroom;
            # at 3+× we bump the server request past 512 so the source
            # stays larger than the physical render target.
            dpr = screen_dpr(self)
            target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
            radius_phys = int(round(12 * dpr))
            server_px = max(512, target_phys)
            url = self.api.get_image_url(image_id, "Primary", server_px)
            load_image_async(
                f"{image_id}|nppage", url,
                target_phys, target_phys,
                self._on_cover_loaded, rounded_radius=radius_phys,
                on_error=lambda: None,
                priority="high",
            )
        self._fetch_lyrics(np.item_id)

    def _on_cover_loaded(self, pix: QPixmap):
        self._cover_orig = pix
        if pix.isNull():
            return
        # load_image_async already produced the cover at the physical
        # target size with the DPR-multiplied rounded radius; tag it
        # with the device pixel ratio so Qt paints at COVER_SIZE logical
        # points using the full-resolution texture.
        dpr = screen_dpr(self)
        if dpr != 1.0:
            pix.setDevicePixelRatio(dpr)
        self._cover.setPixmap(pix)

    def _refresh_track_list(self):
        # Preview mode short-circuits the queue-driven path: we render
        # the previewed item's tracks in source order and only highlight
        # a row if it matches the live now-playing track (which can
        # happen when the user previews the same album they're listening
        # to).
        if self._preview_id:
            label = self._preview_meta.get("Name", "") or "Loading…"
            # Kind-specific kicker (ALBUM / PLAYLIST / ARTIST) — the
            # "browsing vs now-playing" distinction lives in the top
            # bar now, so the kicker focuses on *what kind of content*
            # the user is looking at.
            preview_kicker = {
                "album":    "ALBUM",
                "playlist": "PLAYLIST",
                "artist":   "ARTIST",
            }.get(self._preview_kind, "BROWSING")
            self._right_kicker.setText(f"{preview_kicker}  ·  {label}")
            self._displayed_items_kind = "source"
            highlight_index = self._preview_current_highlight_index()
            # Playlists (and any future cross-artist preview kind) need
            # the per-row artist; album previews are by-definition
            # single-artist so we suppress the sub-line.
            is_album = self._preview_kind == QueueKind.ALBUM
            self._populate_rows(
                self._preview_tracks,
                show_artist=not is_album,
                highlight_index=highlight_index,
                multi_disc_enabled=is_album,
            )
            return

        ctx = self.queue_mgr.context
        # Single ALL-CAPS kicker. When there's a human-readable source
        # (album / playlist name) we append it after the kind label —
        # "ALBUM · 19", "PLAYLIST · Coffeehouse" — so the user has the
        # full context in one glance without a separate big title.
        # Once the queue diverges from its source (user added a track,
        # dragged a row, removed an item) the queue is no longer a
        # faithful reflection of the source — the kicker collapses to
        # "QUEUE" so the label can't lie.
        is_modified = getattr(self.queue_mgr._q, "is_modified", False)
        if is_modified:
            self._right_kicker.setText("QUEUE")
        else:
            kind_label, default_label = {
                QueueKind.ALBUM: ("ALBUM", "Album"),
                QueueKind.PLAYLIST: ("PLAYLIST", "Playlist"),
                QueueKind.ARTIST: ("ARTIST", "Artist"),
                QueueKind.SHUFFLE: ("LIBRARY SHUFFLE", "Library shuffle"),
                QueueKind.SEARCH: ("SEARCH RESULTS", "Search"),
                QueueKind.MANUAL: ("QUEUE", "Up next"),
                QueueKind.INSTANT_MIX: ("INSTANT MIX", "Instant mix"),
            }.get(ctx.kind, ("QUEUE", "Up next"))
            if ctx.source_label and ctx.source_label != default_label:
                self._right_kicker.setText(f"{kind_label}  ·  {ctx.source_label}")
            else:
                self._right_kicker.setText(kind_label)

        # Pick the right items list per the context's natural ordering.
        # Source-order rendering (album / playlist track list) is only
        # honored while the queue is *pristine* — once the user has
        # added a track, dragged a row, or removed an item the queue
        # has diverged from the source and we render in play-order so
        # the drag visibly takes effect.
        if ctx.kind in _SOURCE_ORDER_KINDS and not is_modified:
            items = self.queue_mgr.original_items
            self._displayed_items_kind = "source"
            # In source-order mode the highlighted row is the
            # original_items index of the currently-playing track.
            current_orig_idx = self._current_original_index()
            highlight_index = current_orig_idx
        else:
            items = self.queue_mgr.queue  # play-order
            self._displayed_items_kind = "play"
            highlight_index = self.queue_mgr.current_index

        # Show artist column on cross-artist queues (everything except
        # an unmodified ALBUM context, where every track is the same
        # artist by definition). Modified queues might cross artists
        # so we surface the column there too.
        show_artist = ctx.kind != QueueKind.ALBUM or is_modified
        # Disc dividers only apply to a *pristine* ALBUM context — once
        # the queue is reordered they no longer correspond to discs.
        multi_disc_enabled = ctx.kind == QueueKind.ALBUM and not is_modified

        self._populate_rows(items, show_artist, highlight_index, multi_disc_enabled)

    def _preview_current_highlight_index(self) -> int:
        """If the live now-playing track happens to be in the previewed
        item's track list, return that row's index so we can highlight
        it. -1 if the previewed item doesn't contain the live track."""
        np = get_now_playing()
        cur_id = (np.item_id or "").lower() if np else ""
        if not cur_id or not self._preview_tracks:
            return -1
        for i, t in enumerate(self._preview_tracks):
            if (t.get("Id") or "").lower() == cur_id:
                return i
        return -1

    def _current_original_index(self) -> int:
        """Index into `original_items` of the currently-playing track —
        what the right pane should highlight when it's rendering source
        order. -1 if nothing is playing."""
        cur = self.queue_mgr.current_item
        if not cur:
            return -1
        target = (cur.get("Id") or "").lower()
        for i, it in enumerate(self.queue_mgr.original_items):
            if (it.get("Id") or "").lower() == target:
                return i
        return -1

    def _populate_rows(self, items: List[Dict], show_artist: bool,
                       highlight_index: int, multi_disc_enabled: bool = False):
        """Drop the previous rendering and rebuild the model with the
        new items. Disc dividers only show in pristine multi-disc
        ALBUM context (caller flips multi_disc_enabled accordingly)
        and only when more than one disc is actually represented in
        the items."""
        # Multi-disc detection — only meaningful for ALBUM contexts. In
        # PLAYLIST / SHUFFLE / SEARCH views every track comes from a
        # different album with its own ParentIndexNumber, so grouping
        # by disc would produce an absurd "Disc 1, Disc 2, Disc 1, …"
        # interleave. Caller flips multi_disc_enabled only for ALBUM.
        disc_numbers = {int(t.get("ParentIndexNumber") or 1) for t in items}
        multi_disc = (
            multi_disc_enabled
            and (len(disc_numbers) > 1 or any(d > 1 for d in disc_numbers))
        )
        drag_enabled = not bool(self._preview_id)
        self._tracks_model.set_state(
            items, highlight_index, show_artist, drag_enabled,
            multi_disc=multi_disc,
        )
        # Scroll the highlighted row into view (if any). Deferred a
        # tick so QListView has computed cell rects post-reset.
        if highlight_index >= 0:
            def _scroll_to_target(h=highlight_index):
                try:
                    row = self._tracks_model.row_for_play_index(h)
                    if row >= 0:
                        idx = self._tracks_model.index(row, 0)
                        self._list_container.scrollTo(
                            idx,
                            QAbstractItemView.ScrollHint.PositionAtCenter,
                        )
                except RuntimeError:
                    pass

            QTimer.singleShot(0, _scroll_to_target)

    @Slot(int)
    def _on_row_clicked(self, displayed_index: int):
        # Preview mode: clicking any row installs the previewed item as
        # the live queue and starts from that index. The page transitions
        # back to live mode automatically once playback_started fires.
        if self._preview_id:
            if not (0 <= displayed_index < len(self._preview_tracks)):
                return
            # Snapshot, then drop preview state *before* emitting so the
            # sync-fired playback_started / queue_changed handlers see
            # live mode (same race as _on_play_preview).
            tracks = list(self._preview_tracks)
            ctx = QueueContext(
                kind=self._preview_kind,
                source_id=self._preview_id,
                source_label=self._preview_meta.get("Name", ""),
            )
            self._preview_id = ""
            self._preview_meta = {}
            self._preview_tracks = []
            self._update_cta_visibility()
            self.preview_changed.emit(False)
            self.bus.queue_play_now.emit(tracks, displayed_index, ctx)
            return
        # The displayed index is into either `original_items` (source
        # order) or `queue` (play order). track_jumped wants a play-order
        # index, so map source → play when needed.
        if self._displayed_items_kind == "source":
            orig = self.queue_mgr.original_items
            if not (0 <= displayed_index < len(orig)):
                return
            target_id = (orig[displayed_index].get("Id") or "").lower()
            for play_idx, it in enumerate(self.queue_mgr.queue):
                if (it.get("Id") or "").lower() == target_id:
                    self.bus.track_jumped.emit(play_idx)
                    return
        else:
            self.bus.track_jumped.emit(displayed_index)

    @Slot(int, QPoint)
    def _on_track_context_menu(self, play_idx: int, global_pos: QPoint):
        """Right-click on a track row → Play next / Add to queue /
        Remove from queue. Resolve the item dict from the appropriate
        list given the current display mode (source-order vs play-order
        vs preview)."""
        # Resolve the item dict from the right source.
        if self._preview_id:
            tracks = self._preview_tracks
        elif self._displayed_items_kind == "source":
            tracks = self.queue_mgr.original_items
        else:
            tracks = self.queue_mgr.queue
        if not (0 <= play_idx < len(tracks)):
            return
        item = tracks[play_idx]
        # Remove from queue is only meaningful in live mode (the
        # queue is the displayed list); preview mode has no concept
        # of "remove" since the user isn't editing a queue.
        from modules.ui_helpers import opaque_menu
        menu = opaque_menu(self._list_container)
        play_next = menu.addAction("Play next")
        add_end = menu.addAction("Add to queue")
        remove_act = None
        if not self._preview_id:
            menu.addSeparator()
            remove_act = menu.addAction("Remove from queue")
        chosen = menu.exec(global_pos)
        if chosen is play_next:
            self.bus.queue_add_next.emit([item])
        elif chosen is add_end:
            self.bus.queue_add_end.emit([item])
        elif chosen is not None and chosen is remove_act:
            self.bus.queue_remove_at.emit(play_idx)

    # ── Lyrics ──────────────────────────────────────────────────────────────

    def _fetch_lyrics(self, item_id: str):
        if not item_id:
            self._set_lyrics_text("")
            return
        hit, cached = self._lyrics_cache.get(item_id)
        if hit:
            self._render_lyrics_payload(cached)
            return
        if self._lyrics_loading_for == item_id:
            return  # already in flight
        self._lyrics_loading_for = item_id
        self._set_lyrics_text("Loading lyrics…", muted=True)
        # Fetch on the shared QThreadPool; `_lyrics_loaded` is wired to
        # `_on_lyrics_loaded` and dispatches on the GUI thread.
        run_async(
            self.api.get_lyrics, item_id,
            on_result=lambda payload, iid=item_id:
                self._lyrics_loaded.emit(iid, payload),
            on_error=lambda _e, iid=item_id:
                self._lyrics_loaded.emit(iid, None),
        )

    @Slot(str, object)
    def _on_lyrics_loaded(self, item_id: str, payload):
        self._lyrics_cache.put(item_id, payload)
        if self._lyrics_loading_for == item_id:
            self._lyrics_loading_for = ""
        # Only render if this is still the active item — the user may
        # have skipped tracks while we were fetching.
        np = get_now_playing()
        if np.item_id == item_id:
            self._render_lyrics_payload(payload)

    def _render_lyrics_payload(self, payload: Optional[Dict]):
        if not payload:
            self._set_lyrics_text("No lyrics available", muted=True)
            return
        lines = payload.get("Lyrics") or []
        if not lines:
            self._set_lyrics_text("No lyrics available", muted=True)
            return

        # Synced if any line carries a non-zero `Start` (Jellyfin returns
        # 100-ns ticks). Build per-line widgets either way so we can
        # later highlight; for unsynced we just don't drive scroll.
        any_timed = any(int(ln.get("Start") or 0) > 0 for ln in lines)
        starts_ms: List[int] = []
        widgets: List[QLabel] = []
        for ln in lines:
            text = (ln.get("Text") or "").strip()
            start_ticks = int(ln.get("Start") or 0)
            start_ms = start_ticks // 10_000
            label = QLabel(text or "♪")  # blank lines render as a beat marker
            label.setWordWrap(True)
            # Left-align lyrics on a wide desktop pane — reads as verse
            # the way Apple Music macOS does. iOS centers; desktop is
            # different ergonomics.
            label.setAlignment(Qt.AlignmentFlag.AlignLeft)
            # Initial style: dim / falloff. The first synced position
            # tick will recolor by distance from the active line.
            label.setStyleSheet(self._lyric_line_css(distance=99))
            starts_ms.append(start_ms)
            widgets.append(label)

        self._install_lyrics_widgets(widgets, starts_ms, synced=any_timed)
        # If we're rendering mid-track (e.g. user opened the page after
        # playback already started), prime the highlight to the current
        # position straight away.
        if self._lyrics_synced:
            self._update_active_lyric(get_now_playing().position)

    def _install_lyrics_widgets(self, widgets: List[QLabel],
                                starts_ms: List[int], synced: bool):
        # Wipe everything before the trailing stretch.
        while self._lyrics_layout.count() > 1:
            it = self._lyrics_layout.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._lyrics_widgets = widgets
        self._lyrics_starts_ms = starts_ms
        self._lyrics_synced = synced
        self._active_line_idx = -1
        # Each new track resets the user's "off live" state — the
        # tracking auto-scroll picks up from the new track's first line.
        self._user_off_live = False
        for i, w in enumerate(widgets):
            self._lyrics_layout.insertWidget(i, w)
        self._lyrics_scroll.verticalScrollBar().setValue(0)
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()

    def _set_lyrics_text(self, text: str, muted: bool = False):
        """Single-paragraph fallback used for status messages ("Loading…",
        "No lyrics available"). Clears any previously built per-line
        widgets so the synced highlight doesn't try to address them."""
        self._lyrics_widgets = []
        self._lyrics_starts_ms = []
        self._lyrics_synced = False
        self._active_line_idx = -1
        self._user_off_live = False
        while self._lyrics_layout.count() > 1:
            it = self._lyrics_layout.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not text:
            return
        color = TEXT_FAINT if muted else TEXT_DIM
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Qt's stylesheet parser doesn't support line-height — drop it
        # rather than letting it warn at runtime. Lyrics labels rely on
        # default leading; spacing between successive lines is handled
        # by _lyrics_layout.spacing.
        label.setStyleSheet(f"color: {color}; {type_qss(TYPE_BODY)}")
        self._lyrics_layout.insertWidget(0, label)
        self._lyrics_scroll.verticalScrollBar().setValue(0)
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()

    # Distance-from-active opacity falloff. Apple Music's lyrics view
    # is the genre reference: the active line is the loudest object on
    # the page, with surrounding lines fading by distance so the eye
    # naturally tracks the now-moment without losing the few lines
    # ahead. Index by absolute distance from the active line.
    _FALLOFF = (0.95, 0.70, 0.45, 0.28, 0.18)

    # Per-key (active_size, active_weight, active_pad, inactive_size,
    # inactive_weight, inactive_pad). Bookended by the smallest comfortable
    # readable size and a roomy desktop comfort size; "default" matches the
    # baseline shipped post-Phase-3.
    _LYRICS_SIZE_TABLE = {
        "small":   (16, 600, 4,  12, 400, 2),
        "default": (18, 600, 6,  13, 400, 3),
        "large":   (20, 600, 8,  14, 400, 4),
        "largest": (22, 700, 10, 16, 600, 5),
    }

    def _lyric_line_css(self, distance: int) -> str:
        # Pull current size choice on every call — cheap dict lookup, and
        # avoids a stale snapshot when the user changes the setting and
        # the page restyles existing lines.
        from modules.settings import get_settings
        key = get_settings().lyrics_font_size
        a_size, a_weight, a_pad, i_size, i_weight, i_pad = (
            self._LYRICS_SIZE_TABLE.get(key, self._LYRICS_SIZE_TABLE["default"])
        )
        if distance == 0:
            return (
                f"color: rgba(255,255,255,0.95); "
                f"font-size: {a_size}px; font-weight: {a_weight}; "
                f"padding: {a_pad}px 0;"
            )
        idx = min(distance, len(self._FALLOFF) - 1)
        opacity = self._FALLOFF[idx]
        return (
            f"color: rgba(255,255,255,{opacity:.2f}); "
            f"font-size: {i_size}px; font-weight: {i_weight}; "
            f"padding: {i_pad}px 0;"
        )

    def _restyle_lyrics_around(self, active: int):
        # Recolor every line by its distance from `active`. Cheap — we're
        # only running this when the active line actually changes.
        for i, w in enumerate(self._lyrics_widgets):
            w.setStyleSheet(self._lyric_line_css(abs(i - active)))

    @Slot(int)
    def _on_position_updated(self, ms: int):
        if self._lyrics_synced:
            self._update_active_lyric(ms)

    def _update_active_lyric(self, ms: int):
        if not self._lyrics_starts_ms:
            return
        # Find the latest line whose start <= ms. bisect_right gives the
        # insertion point for `ms+1`, so the active index is one less.
        idx = bisect.bisect_right(self._lyrics_starts_ms, ms) - 1
        if idx < 0:
            idx = 0
        if idx == self._active_line_idx:
            return
        self._active_line_idx = idx
        self._restyle_lyrics_around(idx)
        self._scroll_to_active_lyric(idx)

    def _scroll_to_active_lyric(self, idx: int):
        """Anchor the active line at ~38% from the top of the lyrics
        viewport so 2-3 upcoming lines stay visible — feels predictive
        rather than reactive (Apple Music pattern). Smooth 300ms ease-out
        via QPropertyAnimation on the vertical scroll bar."""
        if not (0 <= idx < len(self._lyrics_widgets)):
            return
        active = self._lyrics_widgets[idx]
        viewport = self._lyrics_scroll.viewport()
        if viewport is None or viewport.height() == 0:
            return
        # Each line widget's direct parent is the lyrics container, so
        # `pos()` is already container-relative — using mapTo() here
        # double-counts and overshoots, which knocked the active line
        # well above the visible area.
        active_y = active.pos().y()
        # Anchor a touch lower than 38%: at 0.42 the active line sits
        # comfortably near the eye-line of the viewport with the next
        # 2-3 lines below still visible. 38% on a tall pane was placing
        # the line just inside the top edge.
        target = active_y - int(viewport.height() * 0.42)
        bar = self._lyrics_scroll.verticalScrollBar()
        target = max(bar.minimum(), min(bar.maximum(), target))
        if abs(target - bar.value()) < 8:
            return  # < 8px move — skip to avoid jitter on short consecutive lines
        # Mark this scroll as auto so the valueChanged listener can tell
        # it apart from a manual user scroll. Cleared by the animation's
        # finished signal (wired in _build_left_pane).
        self._lyric_scroll_is_auto = True
        self._lyrics_anim.stop()
        self._lyrics_anim.setStartValue(bar.value())
        self._lyrics_anim.setEndValue(target)
        self._lyrics_anim.start()

    # ── Lyrics toggle + Live button ────────────────────────────────────

    def _on_lyrics_scrolled(self, _value: int):
        # If the scroll was triggered by our auto-anchor animation,
        # ignore — only user-initiated scrolls flip the off-live state.
        if self._lyric_scroll_is_auto:
            return
        if not self._lyrics_synced or not self._lyrics_widgets:
            return
        self._user_off_live = True
        self._update_live_btn_visibility()

    def _resnap_to_live(self):
        self._user_off_live = False
        self._update_live_btn_visibility()
        if 0 <= self._active_line_idx < len(self._lyrics_widgets):
            self._scroll_to_active_lyric(self._active_line_idx)

    def _update_live_btn_visibility(self):
        # Live button only makes sense when lyrics are visible, synced,
        # and the user has actively scrolled away from the active line.
        show = (
            self._show_lyrics
            and self._lyrics_synced
            and self._user_off_live
            and not self._preview_id
        )
        self._live_btn.setVisible(show)

    def _toggle_lyrics(self):
        self._show_lyrics = not self._show_lyrics
        self._update_lyrics_visibility()
        # Re-snap to active line when lyrics come back so the user
        # doesn't have to find the now-moment manually.
        if self._show_lyrics and self._lyrics_synced:
            self._user_off_live = False
            if 0 <= self._active_line_idx < len(self._lyrics_widgets):
                self._scroll_to_active_lyric(self._active_line_idx)
        self._update_live_btn_visibility()

    def _update_lyrics_visibility(self):
        # In preview mode, lyrics are always hidden (browsing, not
        # listening) and the toggle button is hidden too — nothing to
        # toggle. In live mode, the scroll area follows _show_lyrics
        # and the toggle label flips between "Show" and "Hide".
        if self._preview_id:
            self._lyrics_scroll.hide()
            self._lyrics_toggle_btn.hide()
            self._live_btn.hide()
            return
        # Hide the toggle button when the active track has no lyrics
        # at all (avoids dangling chrome with nothing to control).
        has_lyrics = bool(self._lyrics_widgets) or bool(self._lyrics_starts_ms)
        self._lyrics_toggle_btn.setVisible(has_lyrics)
        self._lyrics_toggle_btn.setText("Hide lyrics" if self._show_lyrics else "Show lyrics")
        self._lyrics_scroll.setVisible(self._show_lyrics and has_lyrics)

    # ── Heart + Play CTAs ──────────────────────────────────────────────

    def _update_cta_visibility(self):
        # Play CTA only shows in preview mode (live mode has Play in the
        # bottom transport bar). Heart shows whenever there's a target
        # to favorite (album/playlist source ID either previewed or live).
        in_preview = bool(self._preview_id)
        self._play_cta.setVisible(in_preview)
        has_fav_target = bool(self._preview_id or self.queue_mgr.context.source_id)
        self._fav_cta.setVisible(has_fav_target)

    def _on_play_preview(self):
        if not self._preview_id or not self._preview_tracks:
            return
        # Snapshot before clearing — we drop preview state *before*
        # emitting queue_play_now so the synchronously-fired
        # playback_started / queue_changed handlers see live mode and
        # refresh the page (kicker, active-track highlight, lyrics).
        tracks = list(self._preview_tracks)
        ctx = QueueContext(
            kind=self._preview_kind,
            source_id=self._preview_id,
            source_label=self._preview_meta.get("Name", ""),
        )
        self._preview_id = ""
        self._preview_meta = {}
        self._preview_tracks = []
        self._update_cta_visibility()
        self.preview_changed.emit(False)
        self.bus.queue_play_now.emit(tracks, 0, ctx)

    def _on_favorite_cta(self):
        # Favorite the current source item (album/playlist), not the
        # active track — the bottom transport bar already favorites the
        # track. This CTA is for the broader collection.
        if self._preview_id:
            target_id = self._preview_id
            cur_meta = self._preview_meta
        else:
            target_id = self.queue_mgr.context.source_id
            cur_meta = self._preview_meta  # not used in live path
        if not target_id:
            return
        cur_fav = bool(cur_meta.get("UserData", {}).get("IsFavorite", False))
        new_state = not cur_fav
        run_async(self.api.toggle_favorite, target_id, new_state)
        cur_meta.setdefault("UserData", {})["IsFavorite"] = new_state
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if new_state else icon("favorite_outline")
        )

    # ── Preview mode ───────────────────────────────────────────────────

    PREVIEW_CACHE_NAME = "preview"

    def load_preview(self, item_id: str, kind: str = "album"):
        """Show this album/playlist's tracks in preview mode without
        installing as the active queue. Click Play / a track to install.
        `kind` is "album" or "playlist" — controls the fetch endpoint
        and the QueueKind installed when preview becomes live.

        Two-phase: render from disk cache instantly if we've shown
        this item before, then refresh from the server in the
        background. New albums (no cache) still hit the network on
        first open, but every subsequent open of an already-seen
        album is instant — even across app launches."""
        if not item_id:
            return
        new_kind = (
            QueueKind.PLAYLIST if kind == "playlist" else QueueKind.ALBUM
        )
        if (item_id == self._preview_id
                and new_kind == self._preview_kind
                and self._preview_meta):
            return  # already loaded
        # Preview target changed — drop the stale cover immediately so
        # the user doesn't see the previously-playing album's artwork
        # under the new album's "Loading…" text. The new cover lands
        # via _on_preview_meta_loaded → load_image_async once the
        # meta fetch resolves.
        self._cover.clear()
        self._cover_orig = None
        self._preview_id = item_id
        self._preview_kind = new_kind
        self._preview_meta = {}
        self._preview_tracks = []
        # Stop any active-track lyric chase while previewing.
        self._user_off_live = False
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()
        self._update_cta_visibility()
        self.preview_changed.emit(True)
        # Try the disk cache for this album/playlist before showing
        # placeholders. A cache hit means the user has previewed this
        # item in a previous session — render meta + tracks
        # immediately and let the background refresh confirm.
        scope = {"kind": kind, "item_id": item_id}
        cached = disk_cache.load(self.PREVIEW_CACHE_NAME, scope)
        if cached and cached.get("meta") and cached.get("tracks") is not None:
            # Render the cached snapshot synchronously so the user
            # sees the album immediately. The fresh fetches still
            # fire below — server data wins on conflict.
            self._on_preview_meta_loaded(item_id, cached["meta"])
            self._on_preview_tracks_loaded(item_id, cached["tracks"])
        else:
            # Cold path — placeholders while we wait on the network.
            self._title.setText("Loading…")
            self._subtitle.setText("")
            self._refresh_track_list()
            self._refresh_meta_line()
        # Async fetches dispatch back to the GUI thread via signals.
        # Different endpoint per kind — playlists pull AlbumId per track
        # (cover art resolves per track, not per playlist).
        fetch_tracks = (
            self.api.get_playlist_items if new_kind == QueueKind.PLAYLIST
            else self.api.get_album_tracks
        )
        run_async(
            self.api.get_item, item_id,
            on_result=lambda meta, iid=item_id: self._preview_meta_loaded.emit(iid, meta),
            on_error=lambda _e, iid=item_id: self._preview_meta_loaded.emit(iid, None),
        )
        run_async(
            fetch_tracks, item_id,
            on_result=lambda tracks, iid=item_id: self._preview_tracks_loaded.emit(iid, tracks),
            on_error=lambda _e, iid=item_id: self._preview_tracks_loaded.emit(iid, []),
        )

    def clear_preview(self):
        """Drop preview state — show the live queue + active track."""
        if not self._preview_id:
            return
        self._preview_id = ""
        self._preview_meta = {}
        self._preview_tracks = []
        self._refresh_now_playing(get_now_playing())
        self._refresh_track_list()
        self._refresh_meta_line()
        self._update_lyrics_visibility()
        self._update_cta_visibility()
        self.preview_changed.emit(False)

    @Slot(str, object)
    def _on_preview_meta_loaded(self, item_id: str, meta: Optional[Dict]):
        # Stale callback if user has moved on to a different preview.
        if item_id != self._preview_id:
            return
        if meta is None:
            # Only show the "Couldn't load" placeholder if we don't
            # already have something on screen — a cached render
            # that's followed by a network failure should keep the
            # cached snapshot up.
            if not self._preview_meta:
                self._title.setText("Couldn't load")
            return
        self._preview_meta = meta
        # Render preview header — title is the album/playlist name,
        # subtitle is the artist (or curator for playlists).
        self._title.setText(meta.get("Name") or "Unknown")
        artist = meta.get("AlbumArtist") or ", ".join(meta.get("AlbumArtists", []) or []) or ""
        self._subtitle.setText(artist)
        # Cover load via the standard image URL helper. Match the
        # live-mode load size + DPR-scaling so this preview shares the
        # cache slot the live now-playing flow would populate for the
        # same album.
        dpr = screen_dpr(self)
        target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
        radius_phys = int(round(12 * dpr))
        server_px = max(512, target_phys)
        cover_url = self.api.get_image_url(item_id, "Primary", server_px)
        if cover_url:
            load_image_async(
                f"{item_id}|nppage", cover_url,
                target_phys, target_phys,
                self._on_cover_loaded, rounded_radius=radius_phys,
                on_error=lambda: None,
            )
        # Reflect favorited state in the heart icon.
        cur_fav = bool(meta.get("UserData", {}).get("IsFavorite", False))
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if cur_fav else icon("favorite_outline")
        )
        self._maybe_save_preview_cache()

    @Slot(str, object)
    def _on_preview_tracks_loaded(self, item_id: str, tracks: Optional[List[Dict]]):
        if item_id != self._preview_id:
            return
        self._preview_tracks = tracks or []
        self._refresh_track_list()
        self._refresh_meta_line()
        self._maybe_save_preview_cache()

    def _refresh_meta_line(self):
        """Update the "12 tracks · 47 min" line under the subtitle.
        Visible only in preview mode with at least one track loaded;
        hidden in live mode and during the cold load while tracks
        are still in flight."""
        tracks = self._preview_tracks
        if not (self._preview_id and tracks):
            self._meta_line.setVisible(False)
            return
        count = len(tracks)
        total_ticks = sum(int(t.get("RunTimeTicks") or 0) for t in tracks)
        # Compose like Apple Music's album header: "12 SONGS · 47 MIN".
        # Use SONG / TRACKS depending on kind for accuracy.
        unit = "song" if self._preview_kind != QueueKind.PLAYLIST else "track"
        count_part = f"{count} {unit}{'s' if count != 1 else ''}"
        if total_ticks <= 0:
            self._meta_line.setText(count_part.upper())
        else:
            self._meta_line.setText(
                f"{count_part}  ·  {self._format_runtime(total_ticks)}".upper()
            )
        self._meta_line.setVisible(True)

    @staticmethod
    def _format_runtime(ticks: int) -> str:
        """Album-runtime formatter: short and human. Sub-hour reads as
        minutes ("47 min"); hour-plus reads as hours+minutes ("1 hr
        23 min"). Matches the convention iTunes / Apple Music use in
        their album headers."""
        total_seconds = ticks // 10_000_000
        hours, rem = divmod(total_seconds, 3600)
        minutes = rem // 60
        if hours <= 0:
            # Round up to 1 min for any non-zero runtime so a
            # 12-second sample album doesn't read as "0 min".
            return f"{max(1, minutes)} min"
        if minutes == 0:
            return f"{hours} hr"
        return f"{hours} hr {minutes} min"

    def _maybe_save_preview_cache(self):
        """Persist the (meta, tracks) pair once both halves have landed
        from the server. Called from both _on_preview_*_loaded handlers
        — whichever fires second triggers the save. Subsequent opens
        of the same item across app launches render from this snapshot
        instantly while the fresh fetch verifies in the background."""
        if not (self._preview_id and self._preview_meta and self._preview_tracks):
            return
        kind = "playlist" if self._preview_kind == QueueKind.PLAYLIST else "album"
        scope = {"kind": kind, "item_id": self._preview_id}
        disk_cache.save(self.PREVIEW_CACHE_NAME, scope, {
            "meta": self._preview_meta,
            "tracks": self._preview_tracks,
        })
