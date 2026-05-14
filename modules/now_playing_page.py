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
    Qt, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, QTimer,
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
    QGraphicsOpacityEffect, QStackedWidget,
    QAbstractItemView, QListView, QMenu, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem,
)

from modules.player_state import (
    PlayerBus, NowPlaying, get_now_playing, QueueKind, QueueContext,
)
from modules.ui_helpers import (
    load_image_async, fmt_duration_ticks, install_song_context_menu,
    ACCENT, TEXT, TEXT_DIM, TEXT_FAINT, screen_dpr, EmptyState,
)
from modules.design_tokens import (
    TYPE_TITLE, TYPE_BODY, TYPE_CAPTION, TYPE_TINY,
    TYPE_MICRO, BTN_PRIMARY, font, type_qss, button_qss,
    SPACE_SM, SPACE_MD, SPACE_LG,
)
from modules.icons import icon, accent_icon, ICON_DIM, ICON_BRIGHT
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

    Drag-reorder is driven from the view (see _TracksListView), not
    from Qt's QDrag/InternalMove framework — the view needs to lock
    the floating widget horizontally and tint it accent/opaque, which
    QDrag can't do. As the user drags, the view calls
    :meth:`move_track` so the surrounding rows visually part to
    preview the drop slot. At drop time the view emits
    ``PlayerBus.queue_move_item`` so the QueueManager commits
    authoritatively."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    IsCurrentRole = Qt.ItemDataRole.UserRole + 2
    ShowArtistRole = Qt.ItemDataRole.UserRole + 3
    KindRole = Qt.ItemDataRole.UserRole + 4    # "track" | "disc"
    DiscInfoRole = Qt.ItemDataRole.UserRole + 5  # (disc_num, count)
    PlayIndexRole = Qt.ItemDataRole.UserRole + 6  # int or -1
    IsDragGhostRole = Qt.ItemDataRole.UserRole + 7  # bool
    SuppressHoverRole = Qt.ItemDataRole.UserRole + 8  # bool
    AnimYOffsetRole = Qt.ItemDataRole.UserRole + 9  # float px
    IsPressedRole = Qt.ItemDataRole.UserRole + 10  # bool — mouse-down, pre-drag-threshold
    IsKbCursorRole = Qt.ItemDataRole.UserRole + 11  # bool — keyboard arrow-key cursor row

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
        # Pressed row — populated on mouseDown over a track, cleared
        # on mouseUp or when drag begins. Communicates "you're
        # holding this" before the drag threshold is crossed.
        self._pressed_row: int = -1
        # Keyboard-arrow cursor row. Up/Down move it, Enter plays it.
        # Tracked separately from selection (the view uses
        # NoSelection mode so mouse clicks → play directly).
        self._kb_cursor_row: int = -1
        # Animation offsets for row-parting on move. Keyed by id(entry)
        # so values survive entry reordering. View seeds these after
        # move_track and decays them on a timer; the delegate translates
        # paint by the offset, producing a "rows slide to make room"
        # effect instead of the snap that beginMoveRows produces alone.
        self._anim_offsets: Dict[int, float] = {}

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
        if role == self.AnimYOffsetRole:
            if self._anim_offsets:
                return float(self._anim_offsets.get(id(entry), 0.0))
            return 0.0
        if role == self.IsPressedRole:
            return row == self._pressed_row
        if role == self.IsKbCursorRole:
            return row == self._kb_cursor_row
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

    def move_track(self, src_row: int, target_row: int) -> int:
        """Reorder entries so the source row ends up at ``target_row``.
        Returns the post-move row index of the source, or -1 if the
        move was rejected (out of range, onto a divider, or no-op).

        Uses beginMoveRows / endMoveRows (not beginResetModel) so the
        view doesn't full-relayout flash on every drag tick."""
        n = len(self._entries)
        if not (0 <= src_row < n):
            return -1
        src_entry = self._entries[src_row]
        if src_entry["kind"] != "track":
            return -1
        # Clamp target to a valid slot.
        if target_row < 0:
            target_row = 0
        if target_row >= n:
            target_row = n - 1
        if target_row == src_row:
            return src_row
        # Don't land directly on a divider.
        if self._entries[target_row]["kind"] == "disc":
            return -1
        # beginMoveRows takes the destination as "insert-before"
        # AS IF the source were still in place — so for a downward
        # move (target > src), beginMoveRows' dest is target + 1.
        bmr_dest = target_row + 1 if target_row > src_row else target_row
        if not self.beginMoveRows(
            QModelIndex(), src_row, src_row,
            QModelIndex(), bmr_dest,
        ):
            return -1
        entry = self._entries.pop(src_row)
        self._entries.insert(target_row, entry)
        # Re-number track play_indices in their new order so subsequent
        # drags compute correctly against the new layout.
        n = 0
        for e in self._entries:
            if e["kind"] == "track":
                e["play_index"] = n
                n += 1
        self.endMoveRows()
        return target_row

    # ── Row-parting animation ─────────────────────────────────────────
    #
    # When move_track fires during a drag, beginMoveRows snaps the
    # surrounding rows to their new positions instantly. Without
    # compensating offsets the user sees rows pop into place. The view
    # captures the affected entries' ids around a move (see
    # _TracksListView._do_move_with_anim) and seeds add_anim_offset to
    # paint each one back at its OLD position; the view ticks
    # tick_animation on a timer to decay the offsets to zero, giving
    # rows a smooth slide into their new slots.

    def snapshot_entry_ids(self) -> List[int]:
        """Return current entry ids in row order (for caller to identify
        which entry sits where before a model.move_track call)."""
        return [id(e) for e in self._entries]

    def add_anim_offset(self, entry_id: int, additional_offset_px: float):
        """Add to the running animation offset for one entry. Used by
        the view after move_track to seed the row's starting offset so
        it paints at its pre-move position."""
        cur = self._anim_offsets.get(entry_id, 0.0)
        self._anim_offsets[entry_id] = cur + additional_offset_px

    def tick_animation(self, decay: float = 0.55) -> bool:
        """Decay every active offset toward zero. Returns True if any
        offsets remain (so the caller knows to keep the timer running).
        Called from the view's 60Hz animation timer."""
        if not self._anim_offsets:
            return False
        drop: List[int] = []
        for eid in list(self._anim_offsets.keys()):
            new_off = self._anim_offsets[eid] * decay
            if abs(new_off) < 0.5:
                drop.append(eid)
            else:
                self._anim_offsets[eid] = new_off
        for eid in drop:
            del self._anim_offsets[eid]
        return bool(self._anim_offsets)

    def has_active_animation(self) -> bool:
        return bool(self._anim_offsets)

    def clear_animation(self):
        """Drop any in-progress offsets — called at drag end so the
        next layout reads as static."""
        if self._anim_offsets:
            self._anim_offsets.clear()

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
        # Old offsets keyed off prior entry dicts — drop them so the
        # post-reset view doesn't paint stale translations.
        self._anim_offsets.clear()
        # Drop any leftover keyboard cursor — the row indices are
        # different now and a stale cursor on row 4 might point at
        # a completely different track or a disc divider.
        self._kb_cursor_row = -1
        self.endResetModel()

    def set_pressed_row(self, row: int):
        """Mark a row as pressed-but-not-yet-dragged so the delegate
        can paint a deeper wash. Pass -1 to clear."""
        if row == self._pressed_row:
            return
        old = self._pressed_row
        self._pressed_row = row
        for r in (old, row):
            if 0 <= r < len(self._entries):
                idx = self.index(r, 0)
                self.dataChanged.emit(idx, idx, [self.IsPressedRole])

    def set_kb_cursor_row(self, row: int):
        """Mark a row as the keyboard-arrow cursor. The delegate
        paints a subtle wash on this row so the user can see which
        track Enter would play. Pass -1 to clear."""
        if row == self._kb_cursor_row:
            return
        old = self._kb_cursor_row
        self._kb_cursor_row = row
        for r in (old, row):
            if 0 <= r < len(self._entries):
                idx = self.index(r, 0)
                self.dataChanged.emit(idx, idx, [self.IsKbCursorRole])

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
        # Row-parting animation — when move_track fired recently, the
        # model carries a residual y-offset for each shifted entry that
        # decays to zero on a timer. Translating the painter here makes
        # the row paint at its OLD position; as the offset decays, the
        # row appears to slide into its new slot. Clipping has to come
        # off because Qt sets the painter's clip to the natural rect
        # and our translation would otherwise be clipped out.
        offset = float(index.data(_TracksModel.AnimYOffsetRole) or 0.0)
        if offset != 0.0:
            painter.save()
            painter.setClipping(False)
            painter.translate(0.0, offset)
            try:
                if kind == "disc":
                    self._paint_divider(painter, option, index)
                else:
                    self._paint_track(painter, option, index)
            finally:
                painter.restore()
            return
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
        is_pressed = bool(index.data(_TracksModel.IsPressedRole))
        is_kb_cursor = bool(index.data(_TracksModel.IsKbCursorRole))
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Re-read theme constants every paint so live-accent flows
        # through with a viewport().update().
        from modules.ui_helpers import ACCENT as _ACCENT

        # Hover wash — subtle highlight when the cursor's over the
        # row. Suppressed while a drag is in flight so the rest of
        # the list reads as static while the user rearranges. The
        # pressed-but-not-yet-dragged state paints a deeper wash so
        # the user gets a "you're holding this" cue before the
        # startDragDistance threshold is crossed.
        if is_pressed:
            inset = rect.adjusted(self.LEFT_PAD - 4, 2,
                                  -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 6, 6)
            painter.fillPath(path, QColor(255, 255, 255, 28))
        elif is_kb_cursor and not suppress_hover:
            # Keyboard-arrow cursor — between hover and press in
            # weight so the user knows which row Enter would play
            # without it looking like they're already clicking.
            inset = rect.adjusted(self.LEFT_PAD - 4, 2,
                                  -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 6, 6)
            painter.fillPath(path, QColor(255, 255, 255, 18))
        elif (not suppress_hover
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
    # Edge-scroll zone: cursor within this many pixels of the
    # viewport's top or bottom edge during drag triggers auto-scroll
    # so the user can drag past visible rows without releasing.
    EDGE_SCROLL_ZONE = 48
    EDGE_SCROLL_INTERVAL = 16  # ms — ~60Hz
    # Speed curves with cursor depth into the edge zone — quadratic
    # ease-in so brushing the zone scrolls gently and pushing right
    # to the edge ramps up quickly. Constant-speed scrolling makes
    # the slow case feel sluggish OR the fast case feel hair-trigger.
    EDGE_SCROLL_MIN_SPEED = 2   # px/tick at zone outer edge
    EDGE_SCROLL_MAX_SPEED = 18  # px/tick at viewport edge
    # Row-parting animation: each move_track seeds per-entry offsets
    # that decay to 0 on this timer, producing a slide-into-slot effect
    # in place of the snap that beginMoveRows would otherwise produce.
    # Decay 0.55 hits the < 0.5 px stop threshold from a 44 px starting
    # offset in ~6 ticks ≈ 95 ms, matching SHIFT_MS.
    ROW_ANIM_INTERVAL = 16
    ROW_ANIM_DECAY = 0.55
    # Drop fade — float opacity 1 → 0 after the user releases. Source
    # row is un-ghosted immediately so it paints normally underneath,
    # producing a cross-fade from float-card to actual-row-content.
    DROP_ANIM_MS = 140

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
        self._drag_src_row_orig: int = -1
        self._drag_src_play_orig: int = -1
        self._float_label: Optional[QLabel] = None
        # Edge auto-scroll during drag — fires on a timer when the
        # cursor sits inside the top/bottom edge zone of the viewport.
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(self.EDGE_SCROLL_INTERVAL)
        self._edge_timer.timeout.connect(self._edge_scroll_tick)
        self._edge_dir: int = 0  # -1 up, 0 idle, +1 down
        self._edge_depth: float = 0.0  # 0..1, cursor depth into the zone
        # Row-parting decay tick. Started by _do_move_with_anim, stops
        # itself once tick_animation reports no remaining offsets. Also
        # drives float-widget smooth-follow toward _float_target_y.
        self._row_anim_timer = QTimer(self)
        self._row_anim_timer.setInterval(self.ROW_ANIM_INTERVAL)
        self._row_anim_timer.timeout.connect(self._row_anim_tick)
        # Float widget target position — the source row's current
        # visualRect.x/y. The anim timer animates the float widget's
        # y toward this target; x snaps (only changes on scrollbar
        # show/hide, which is rare and abrupt). Without smooth-follow
        # the float teleports a full row-height each move, even though
        # the cursor only moved ~ε; the smoothing masks that jump.
        self._float_target_x: int = 0
        self._float_target_y: int = 0

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
                # Press feedback — the row gets a deeper wash until
                # the user either releases (it goes away on click) or
                # crosses the drag threshold (drag overrides it).
                self._model.set_pressed_row(idx.row())
            else:
                self._press_row = -1
                self._press_pos = None
                self._model.set_pressed_row(-1)
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

    def keyPressEvent(self, e):
        # Esc during drag aborts the reorder — the source slides back
        # to its original row and the float fades out, without
        # committing queue_move_item to the bus. Focus lives on the
        # list view after the mouse press, so a plain keyPressEvent
        # override is enough (no app-level event filter needed).
        if self._dragging and e.key() == Qt.Key.Key_Escape:
            self._cancel_drag()
            e.accept()
            return
        if e.key() == Qt.Key.Key_Down and not e.modifiers():
            self._move_kb_cursor(+1)
            e.accept()
            return
        if e.key() == Qt.Key.Key_Up and not e.modifiers():
            self._move_kb_cursor(-1)
            e.accept()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate_kb_cursor()
            e.accept()
            return
        super().keyPressEvent(e)

    def _move_kb_cursor(self, step: int):
        """Step the keyboard-arrow cursor by ``step`` rows, skipping
        disc-divider entries. If no cursor is set yet, seed from the
        currently-playing track so the first arrow press starts from
        a sensible position."""
        rc = self._model.rowCount()
        if rc == 0:
            return
        cur = self._model._kb_cursor_row
        if cur < 0:
            # Seed from the active track if there is one, else from
            # the first track row.
            seed = -1
            for r in range(rc):
                idx = self._model.index(r, 0)
                if (self._model.data(idx, _TracksModel.KindRole) == "track"
                        and self._model.data(idx, _TracksModel.IsCurrentRole)):
                    seed = r
                    break
            if seed < 0:
                for r in range(rc):
                    idx = self._model.index(r, 0)
                    if self._model.data(idx, _TracksModel.KindRole) == "track":
                        seed = r
                        break
            if seed < 0:
                return
            self._model.set_kb_cursor_row(seed)
            self.scrollTo(
                self._model.index(seed, 0),
                QAbstractItemView.ScrollHint.EnsureVisible,
            )
            return
        # Walk by step, skipping dividers.
        r = cur + step
        while 0 <= r < rc:
            idx = self._model.index(r, 0)
            if self._model.data(idx, _TracksModel.KindRole) == "track":
                self._model.set_kb_cursor_row(r)
                self.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)
                return
            r += step

    def _activate_kb_cursor(self):
        """Enter pressed — play the track at the keyboard cursor. If
        the cursor hasn't been seeded yet (user arrived at this view
        and pressed Enter before any arrow press), seed it the same
        way the first Up/Down would, then play. That makes Enter
        "play the album" out of the box on a freshly-opened album
        page — the keyboard parity of the Play CTA on the header."""
        if self._model._kb_cursor_row < 0:
            self._seed_kb_cursor()
        row = self._model._kb_cursor_row
        if row < 0:
            return
        play_idx = self._model.play_index_at(row)
        if play_idx >= 0:
            self.track_clicked.emit(play_idx)

    def _seed_kb_cursor(self):
        """Pick a sensible initial cursor row: prefer the active
        track, fall back to the first track row, return without
        seeding if the model is empty."""
        rc = self._model.rowCount()
        if rc == 0:
            return
        seed = -1
        for r in range(rc):
            idx = self._model.index(r, 0)
            if (self._model.data(idx, _TracksModel.KindRole) == "track"
                    and self._model.data(idx, _TracksModel.IsCurrentRole)):
                seed = r
                break
        if seed < 0:
            for r in range(rc):
                idx = self._model.index(r, 0)
                if self._model.data(idx, _TracksModel.KindRole) == "track":
                    seed = r
                    break
        if seed < 0:
            return
        self._model.set_kb_cursor_row(seed)
        self.scrollTo(
            self._model.index(seed, 0),
            QAbstractItemView.ScrollHint.EnsureVisible,
        )

    def focusInEvent(self, e):
        """Seed the keyboard cursor on first focus entry so the
        track-list highlight reads as "ready" — without this an
        arriving keyboard user sees no cursor until they press Up
        or Down, which is a confusing dead-input moment."""
        if self._model._kb_cursor_row < 0:
            self._seed_kb_cursor()
        super().focusInEvent(e)

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
        self._model.set_pressed_row(-1)

    # ── Custom drag lifecycle ─────────────────────────────────────────

    def _begin_drag(self, src_row: int):
        rect = self.visualRect(self._model.index(src_row, 0))
        if rect.width() <= 0 or rect.height() <= 0:
            return
        # Snapshot the source's ORIGINAL play_index up front — as the
        # user drags, we'll move the source row in the model so the
        # surrounding rows visually part to preview the drop slot, and
        # play_indices get renumbered each move. We need the original
        # play_index at end_drag time to emit queue_move_item with the
        # right src for the QueueManager.
        self._drag_src_play_orig = self._model.play_index_of_entry(src_row)
        self._drag_src_row_orig = src_row
        self._dragging = True
        self._drag_src_row = src_row
        # Clear the pre-drag press wash — the ghost slot takes over
        # the visual now that the drag has begun.
        self._model.set_pressed_row(-1)
        # Build the floating drag card — tinted snapshot of the source
        # row's painted content. The viewport.grab(rect) snapshot
        # already captures the delegate paint; tint it via overlay.
        card = self._make_drag_card(rect)
        self._float_label = QLabel(self.viewport())
        self._float_label.setPixmap(card)
        # Resize in LOGICAL px — card.size() returns the pixmap's raw
        # (device-pixel) size which is w*dpr × h*dpr on fractional /
        # HiDPI scaling, blowing the label up past the row's slot and
        # making it overlap the row below.
        self._float_label.resize(rect.size())
        self._float_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        # Drop shadow under the float card so it reads as "lifted"
        # off the list surface — same depth cue every modern
        # drag-reorder UI uses.
        shadow = QGraphicsDropShadowEffect(self._float_label)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 180))
        self._float_label.setGraphicsEffect(shadow)
        self._float_label.show()
        # Ghost the source row + suppress hover on every row through
        # the model state (delegate honours the flags).
        self._model.set_drag_state(active=True, src_row=src_row)
        # Hide the cursor while dragging — with snap-to-source the
        # cursor and float can be in different parts of the row, and
        # a visible cursor floating "next to" the float card reads as
        # a desync artifact. The float card IS the visual cursor for
        # the drag.
        self.viewport().setCursor(Qt.CursorShape.BlankCursor)
        self.viewport().grabMouse()
        self.drag_state_changed.emit(True)
        # Anchor the float over its source slot AT drag start with no
        # animation — the smooth-follow target is set to the same
        # value so _tick_float has nothing to chase yet. Subsequent
        # moves shift the target by a row-height; the float animates
        # in over ~120 ms instead of teleporting.
        self._float_target_x = rect.x()
        self._float_target_y = rect.y()
        self._float_label.move(rect.x(), rect.y())
        self._float_label.raise_()
        # Process the cursor's current position so any initial offset
        # past the source row (rare — only if the press was on the
        # very edge of an adjacent row) triggers an immediate move.
        pos = self.viewport().mapFromGlobal(
            self.cursor().pos() if hasattr(self, "cursor") else QPoint(0, 0)
        )
        self._update_drag(pos)

    def _update_drag(self, viewport_pos: QPoint):
        if not self._dragging or self._float_label is None:
            return
        self._float_label.raise_()
        # Edge auto-scroll — if the cursor's inside the top/bottom
        # edge zone, start the tick timer so the view scrolls under
        # the cursor and the user can drag past hidden rows.
        h = self.viewport().height()
        cy = viewport_pos.y()
        zone = self.EDGE_SCROLL_ZONE
        if cy < zone and self.verticalScrollBar().value() > 0:
            self._edge_dir = -1
            # cy=0 → depth 1 (cursor at viewport top, max speed);
            # cy=zone → depth 0 (cursor just entering zone).
            self._edge_depth = max(0.0, min(1.0, (zone - cy) / zone))
        elif (cy > h - zone
              and self.verticalScrollBar().value()
                  < self.verticalScrollBar().maximum()):
            self._edge_dir = +1
            self._edge_depth = max(0.0, min(1.0, (cy - (h - zone)) / zone))
        else:
            self._edge_dir = 0
            self._edge_depth = 0.0
        if self._edge_dir != 0:
            if not self._edge_timer.isActive():
                self._edge_timer.start()
        else:
            self._edge_timer.stop()
        # Find which row the cursor is over and move the source row
        # to that slot if it isn't already there. The visual effect:
        # all rows below the target slide down (and rows above the
        # original source slot slide up) so the empty gap follows the
        # cursor, previewing exactly where the drop will land.
        target_row = self._target_row_for_y(viewport_pos.y())
        if target_row >= 0 and target_row != self._drag_src_row:
            new_row = self._do_move_with_anim(
                self._drag_src_row, target_row,
            )
            if new_row >= 0:
                self._drag_src_row = new_row
                # Update the model's ghost-row tracker so the right
                # slot paints as the gap.
                self._model.set_drag_state(active=True, src_row=new_row)
        # Sync the float's smooth-follow target to the source row's
        # current y. This also handles the "scroll without row move"
        # case during edge auto-scroll — the slot scrolled under us,
        # so the float chases it.
        self._refresh_float_target()

    @Slot()
    def _edge_scroll_tick(self):
        if not self._dragging or self._edge_dir == 0:
            self._edge_timer.stop()
            return
        bar = self.verticalScrollBar()
        # Quadratic ease-in on depth: depth=0.5 → 25% of max-extra
        # speed; depth=1.0 → full speed. Pushing deeper into the zone
        # accelerates faster than a linear ramp would.
        t = self._edge_depth
        eased = t * t
        speed = self.EDGE_SCROLL_MIN_SPEED + (
            self.EDGE_SCROLL_MAX_SPEED - self.EDGE_SCROLL_MIN_SPEED
        ) * eased
        new_val = bar.value() + self._edge_dir * int(round(speed))
        new_val = max(0, min(new_val, bar.maximum()))
        if new_val == bar.value():
            self._edge_timer.stop()
            self._edge_dir = 0
            return
        bar.setValue(new_val)
        # Reprocess the drag using the current cursor position so the
        # hover slot updates as the view scrolls.
        from PySide6.QtGui import QCursor
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        # Recompute target without recursing through edge-scroll logic:
        target_row = self._target_row_for_y(pos.y())
        if target_row >= 0 and target_row != self._drag_src_row:
            new_row = self._do_move_with_anim(
                self._drag_src_row, target_row,
            )
            if new_row >= 0:
                self._drag_src_row = new_row
                self._model.set_drag_state(active=True, src_row=new_row)
        # Source's visualRect.y shifts every scroll tick even when no
        # move fires — refresh the float target so it tracks the slot.
        self._refresh_float_target()

    # ── Row-parting animation ─────────────────────────────────────────

    def _do_move_with_anim(self, src_row: int, target_row: int) -> int:
        """Move a row in the model and seed per-entry y-offsets so the
        rows that just shifted paint at their PRE-MOVE positions; the
        decay timer then animates the offsets to 0, sliding the rows
        into their new slots.

        Returns the source's new row index, or -1 if the move was
        rejected by model.move_track."""
        prev_ids = self._model.snapshot_entry_ids()
        new_row = self._model.move_track(src_row, target_row)
        if new_row < 0:
            return -1
        # Each move_track shifts exactly one slot's worth (the source
        # row's height) for every entry strictly between src and
        # target — including across disc-divider rows, since the
        # inserted slot is a TRACK_HEIGHT track and that's what
        # everyone else shifts past.
        h = float(_TrackDelegate.TRACK_HEIGHT)
        if target_row > src_row:
            # Source moved DOWN — entries at old rows (src, target]
            # shifted UP by one track. Seed +h so they paint at their
            # old (higher-y) position.
            shift_px = +h
            old_rows = range(src_row + 1, target_row + 1)
        elif target_row < src_row:
            # Source moved UP — entries at old rows [target, src)
            # shifted DOWN by one track. Seed -h.
            shift_px = -h
            old_rows = range(target_row, src_row)
        else:
            return new_row
        src_id = prev_ids[src_row] if 0 <= src_row < len(prev_ids) else None
        for old_r in old_rows:
            if not (0 <= old_r < len(prev_ids)):
                continue
            eid = prev_ids[old_r]
            if eid == src_id:
                continue
            self._model.add_anim_offset(eid, shift_px)
        if not self._row_anim_timer.isActive():
            self._row_anim_timer.start()
        # Force a repaint on this tick so the rows don't snap for a
        # frame between the move and the first decay step.
        self.viewport().update()
        return new_row

    @Slot()
    def _row_anim_tick(self):
        row_active = self._model.tick_animation(self.ROW_ANIM_DECAY)
        float_active = self._tick_float()
        self.viewport().update()
        if not row_active and not float_active:
            self._row_anim_timer.stop()

    def _refresh_float_target(self):
        """Recompute the float's target position from the source row's
        current visualRect. Called whenever something might have moved
        the source slot — a model move, an edge auto-scroll tick, or
        a regular mouse-move whose cursor crossed nothing (still useful
        because the viewport may have scrolled meanwhile)."""
        if self._float_label is None:
            return
        rect = self.visualRect(self._model.index(self._drag_src_row, 0))
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._float_target_x = rect.x()
        self._float_target_y = rect.y()
        # Kick the anim timer if the float isn't already at target.
        if (self._float_label.y() != self._float_target_y
                or self._float_label.x() != self._float_target_x):
            if not self._row_anim_timer.isActive():
                self._row_anim_timer.start()

    def _tick_float(self) -> bool:
        """Step the float widget toward its target. Returns True if
        the float is still in motion (timer should keep running)."""
        if self._float_label is None:
            return False
        cur_x = self._float_label.x()
        cur_y = self._float_label.y()
        # X snaps — only changes on scrollbar show/hide, which is
        # abrupt and rare; smoothing it would just look mushy.
        new_x = self._float_target_x
        # Y decays toward target using the same factor as row offsets
        # so float and rows settle on the same beat.
        dy = cur_y - self._float_target_y
        if abs(dy) < 0.5:
            new_y = self._float_target_y
            done = True
        else:
            new_y = int(round(self._float_target_y + dy * self.ROW_ANIM_DECAY))
            done = False
        if new_x != cur_x or new_y != cur_y:
            self._float_label.move(new_x, new_y)
        return not done

    def _end_drag(self):
        """Normal drag end (mouse release) — snap float into its
        final slot, fade it out, and commit the move to the queue."""
        self._teardown_drag(snap_float=True, commit=True)

    def _cancel_drag(self):
        """Esc-cancel — reverse the in-progress moves so the source
        lands back at its original row, then tear down WITHOUT
        committing. The float fades at the user's current cursor
        position (not the original slot) so the cancel reads as
        'release where you were'; rows decay back to their original
        layout via the standard row-parting animation."""
        if not self._dragging:
            return
        orig_row = self._drag_src_row_orig
        rc = self._model.rowCount()
        if (0 <= orig_row < rc
                and orig_row != self._drag_src_row):
            new_row = self._do_move_with_anim(
                self._drag_src_row, orig_row,
            )
            if new_row >= 0:
                self._drag_src_row = new_row
                self._model.set_drag_state(active=True, src_row=new_row)
        self._teardown_drag(snap_float=False, commit=False)

    def _teardown_drag(self, snap_float: bool, commit: bool):
        final_row = self._drag_src_row
        src_play_orig = getattr(self, "_drag_src_play_orig", -1)
        # Mouse/cursor + drag-state cleanup happens immediately; the
        # float widget itself sticks around briefly so the drop fade
        # can cross-fade into the now-un-ghosted source row.
        self._edge_timer.stop()
        self._edge_dir = 0
        self.viewport().releaseMouse()
        self.viewport().unsetCursor()
        # Un-ghost source so the real row paints under the fading
        # float card. Row-parting offsets are LEFT to decay naturally
        # — the row anim timer keeps running until they settle.
        self._model.set_drag_state(active=False, src_row=-1)
        self._dragging = False
        self._drag_src_row = -1
        self._drag_src_row_orig = -1
        self._drag_src_play_orig = -1
        self.drag_state_changed.emit(False)
        self._press_row = -1
        self._press_pos = None
        # Snap the float to its smooth-follow target so the fade
        # lands cleanly at the new slot. Cancel skips this so the
        # float fades where the user released, not at the restored
        # slot — feels more like "release and undo".
        if snap_float and self._float_label is not None:
            self._float_label.move(
                self._float_target_x, self._float_target_y,
            )
        self._start_drop_animation()
        if not commit:
            return
        if final_row < 0 or src_play_orig < 0:
            return
        dest_play = self._model.play_index_at(final_row)
        if dest_play < 0 or dest_play == src_play_orig:
            return
        from modules.player_state import PlayerBus
        bus = PlayerBus.get()
        bus.queue_move_item.emit(src_play_orig, dest_play)
        # Drop-at-top = play that track. The dragged track is now at
        # play-index 0; track_jumped jumps playback there.
        if dest_play == 0:
            bus.track_jumped.emit(0)

    def _start_drop_animation(self):
        """Cross-fade the float card out over DROP_ANIM_MS as the
        source row (now un-ghosted) reveals itself underneath. The
        shadow effect is replaced with an opacity effect — Qt only
        allows one QGraphicsEffect per widget, but the fade reads as
        the row landing into place, so the shadow loss is masked."""
        if self._float_label is None:
            return
        label = self._float_label
        # Detach so a subsequent drag can build a fresh float without
        # interfering with this one's lifecycle.
        self._float_label = None
        opacity = QGraphicsOpacityEffect(label)
        opacity.setOpacity(1.0)
        label.setGraphicsEffect(opacity)
        anim = QPropertyAnimation(opacity, b"opacity", self)
        anim.setDuration(self.DROP_ANIM_MS)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _finish():
            label.hide()
            label.setParent(None)
            label.deleteLater()

        anim.finished.connect(_finish)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # ── Target-row math ───────────────────────────────────────────────

    def _target_row_for_y(self, y: int) -> int:
        """Return the row the cursor is currently over — that's the
        slot the source should LAND on. Uses indexAt (any-overlap)
        rather than midpoint detection so the move fires the moment
        the float widget starts overlapping the next row, matching
        the user's "drag the track INTO that slot" intuition.

        Returns -1 when there's no valid target (cursor outside any
        row, or over a divider with no nearby track)."""
        rc = self._model.rowCount()
        if rc == 0:
            return -1
        x = self.viewport().width() // 2
        idx = self.indexAt(QPoint(x, y))
        if idx.isValid():
            row = idx.row()
            kind = self._model.data(idx, _TracksModel.KindRole)
            if kind == "track":
                return row
            # Cursor's over a disc divider — snap to the nearest
            # track on the same side.
            r = self.visualRect(idx)
            if y < r.center().y():
                for prev in range(row - 1, -1, -1):
                    pidx = self._model.index(prev, 0)
                    if (self._model.data(pidx, _TracksModel.KindRole)
                            == "track"):
                        return prev
            else:
                for nxt in range(row + 1, rc):
                    nidx = self._model.index(nxt, 0)
                    if (self._model.data(nidx, _TracksModel.KindRole)
                            == "track"):
                        return nxt
            return -1
        # Cursor outside any row — past the bottom of the last row →
        # land on the last track row.
        for row in range(rc - 1, -1, -1):
            ridx = self._model.index(row, 0)
            if (self._model.data(ridx, _TracksModel.KindRole)
                    == "track"):
                last_rect = self.visualRect(ridx)
                if y > last_rect.bottom():
                    return row
                break
        return -1

    def _make_drag_card(self, source_rect: QRect) -> QPixmap:
        """Render the floating drag card. Single rounded rectangle —
        dark base + accent wash, with the row's content painted on
        top via the delegate (NOT a viewport grab, so the hover wash
        the source row had at drag start doesn't get baked in as a
        second inner rounded shape).

        Same logical size as the row; the rounded fill is inset to
        match the delegate's hover-wash inset so the card aligns
        cleanly with the row column when it sits at rect.x()."""
        from modules.ui_helpers import ACCENT as _ACCENT
        w = source_rect.width()
        h = source_rect.height()
        dpr = float(self.viewport().devicePixelRatio() or 1.0)
        phys_w = max(1, int(round(w * dpr)))
        phys_h = max(1, int(round(h * dpr)))
        out = QPixmap(phys_w, phys_h)
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.GlobalColor.transparent)

        # Horizontal inset matches the delegate's hover-wash geometry
        # so the card sits in the same visual column as the row
        # content. Vertical inset gives the float "breathing room"
        # inside the source-ghost slot — 4 px above + 4 px below
        # reads as the card cleanly slotting between neighbors
        # rather than butting flush against them.
        d = self._delegate
        inset_x = d.LEFT_PAD - 4
        inset_y = 4
        inner = QRectF(
            inset_x, float(inset_y),
            w - 2 * inset_x, h - 2 * inset_y,
        )

        # Resolve accent → RGB triplet.
        from modules.theme import _hex_to_rgb
        try:
            r, g, b = _hex_to_rgb(_ACCENT)
        except Exception:
            r, g, b = (140, 80, 220)

        p = QPainter(out)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Clip everything to the rounded card shape — anything
            # painted outside becomes transparent, so the rest of the
            # pixmap (margins) doesn't render as a hard square.
            path = QPainterPath()
            path.addRoundedRect(inner, 6.0, 6.0)
            p.setClipPath(path)
            # Opaque-ish dark base so rows beneath don't bleed through.
            p.fillRect(inner, QColor(28, 30, 36, 235))
            # Accent wash on top.
            p.fillRect(inner, QColor(r, g, b, 50))
            # Row content via the delegate. Build a fresh style option
            # with State_MouseOver cleared so no hover highlight gets
            # baked in (which would have produced the inner-square
            # double-border effect the user reported). Rect matches
            # the inset so text vcenters inside the visible card area
            # instead of the full slot — otherwise the 4px y-inset
            # would clip the top/bottom of the title row.
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, inset_y, w, h - 2 * inset_y)
            opt.state = QStyle.StateFlag(0)
            opt.font = self.font()
            opt.fontMetrics = self.fontMetrics()
            opt.palette = self.palette()
            idx = self._model.index(self._drag_src_row, 0)
            d.paint(p, opt, idx)
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


class _DownloadButton(QPushButton):
    """A 32px download control for the album view, custom-painted to
    match the bare-icon CTA buttons next to it. Four visual states,
    driven by ``set_state`` from the page's ``download_progress`` hook:

      idle        — a download glyph (the album isn't downloaded)
      pending /   — a faint track ring; ``downloading`` overlays a
      downloading   bright accent arc filling clockwise from 12 o'clock
      complete    — a filled accent disc with a check
      failed      — the download glyph tinted red

    Click semantics are the page's call (download vs. remove vs.
    cancel) — this widget only paints + reports its ``clicked``."""

    _TIPS = {
        "idle": "Download this album",
        "pending": "Queued for download…",
        "downloading": "Downloading… (click to cancel)",
        "complete": "Downloaded — click to remove",
        "failed": "Download failed — click to retry",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; border-radius: 8px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.08); }
            QPushButton:pressed { background: rgba(255, 255, 255, 0.14); }
        """)
        self._state = "idle"
        self._fraction = 0.0
        self.setToolTip(self._TIPS["idle"])

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, fraction: float = 0.0):
        self._state = state if state in self._TIPS else "idle"
        self._fraction = max(0.0, min(1.0, fraction))
        self.setToolTip(self._TIPS[self._state])
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)   # hover / pressed background
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = 9.0
        accent = QColor(ACCENT)
        track = QColor(255, 255, 255, 64)
        glyph = QColor(255, 255, 255, 210)

        if self._state in ("downloading", "pending"):
            ring = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(track, 2.4))
            p.drawArc(ring, 0, 360 * 16)
            if self._state == "downloading" and self._fraction > 0:
                pen = QPen(accent, 2.4)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                # 12 o'clock start, sweeping clockwise (negative span).
                p.drawArc(ring, 90 * 16, -int(self._fraction * 360) * 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glyph if self._state == "downloading" else track)
            p.drawEllipse(QPointF(cx, cy), 2.0, 2.0)
        elif self._state == "complete":
            # Filled accent disc + a white down-arrow — the music-app
            # convention for "downloaded / available offline" (Spotify's
            # downloaded badge, Material's download_for_offline). Reads
            # more specifically than a generic check mark.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(QPointF(cx, cy), r, r)
            pen = QPen(QColor("#ffffff"), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy - 4.0), QPointF(cx, cy + 3.2))
            arrow = QPainterPath()
            arrow.moveTo(cx - 3.0, cy + 0.2)
            arrow.lineTo(cx, cy + 3.6)
            arrow.lineTo(cx + 3.0, cy + 0.2)
            p.drawPath(arrow)
        else:  # idle / failed → download glyph
            # Idle matches the unfilled favorite heart: muted ICON_DIM
            # at rest, brightening to ICON_BRIGHT on hover (the heart's
            # QIcon flips the same way). The dim tone also reads as
            # "not downloaded yet" next to a filled-disc downloaded
            # badge. Failed stays red regardless.
            if self._state == "failed":
                col = QColor("#e0735c")
            else:
                col = QColor(ICON_BRIGHT if self.underMouse() else ICON_DIM)
            pen = QPen(col, 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy - 5.5), QPointF(cx, cy + 2.5))
            arrow = QPainterPath()
            arrow.moveTo(cx - 3.4, cy - 1.0)
            arrow.lineTo(cx, cy + 3.0)
            arrow.lineTo(cx + 3.4, cy - 1.0)
            p.drawPath(arrow)
            p.drawLine(QPointF(cx - 4.8, cy + 5.6), QPointF(cx + 4.8, cy + 5.6))
        p.end()


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
        self._title = QLabel("Nothing Playing")
        self._title.setFont(font(TYPE_TITLE))
        # Idle styling — TEXT_DIM-equivalent so the placeholder reads
        # as inactive. _refresh_now_playing swaps to the bright
        # color when a real track lands.
        self._title.setStyleSheet("color: #a8a8a8;")
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
        # StrongFocus so keyboard users who arrive at this page via
        # Enter-on-album-tile land here — pressing Enter again then
        # starts playback. button_qss(BTN_PRIMARY) renders a visible
        # focus state via Qt's default focus rect on top of the fill.
        self._play_cta.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._play_cta.clicked.connect(self._on_play_preview)
        self._play_cta.hide()  # shown by _update_cta_visibility in preview mode
        # Down arrow on the focused Play CTA dives into the track
        # list — keyboard parity for the visual layout (Play above
        # the track rows). Focus then sits on the first track and
        # arrow keys step row-to-row from there.
        self._play_cta.keyPressEvent = self._on_play_cta_key

        self._fav_cta = self._cta_icon_btn("favorite_outline", "")
        self._fav_cta.clicked.connect(self._on_favorite_cta)

        # Download control — only meaningful in preview mode (it acts
        # on the previewed album/playlist). State + progress are pushed
        # in from the download_progress bus signal.
        self._download_cta = _DownloadButton()
        self._download_cta.clicked.connect(self._on_download_cta)

        # Balanced row: download left of the Play CTA, heart right of
        # it — the purple Play sits centered between its two flankers.
        cta_row.addWidget(self._download_cta)
        cta_row.addWidget(self._play_cta)
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
        # Stack the track list with an empty-state surface so a queue
        # that resolves to zero tracks (no current playback yet,
        # cleared "Up Next") reads as "nothing queued — go browse"
        # instead of a silent blank.
        self._tracks_empty_state = EmptyState(
            glyph="♪",
            headline="Nothing queued",
            sub="Pick an album, playlist, or song to start the queue.",
            parent=self,
        )
        self._tracks_stack = QStackedWidget()
        self._tracks_stack.setStyleSheet("background: transparent;")
        self._tracks_stack.addWidget(self._list_container)
        self._tracks_stack.addWidget(self._tracks_empty_state)
        v.addWidget(self._tracks_stack, 1)

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
        # Download progress for the album-view download CTA — filtered
        # to the previewed item in the handler.
        self.bus.download_progress.connect(self._on_download_progress)
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
        self._title.setText("Nothing Playing")
        # Re-dim the title to the idle styling (the active-track
        # path in _refresh_now_playing brightens it back).
        self._title.setStyleSheet("color: #a8a8a8;")
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
        # Brighten the title — _on_playback_stopped dims it for the
        # "Nothing Playing" idle state; an active track needs the
        # full-weight color.
        self._title.setStyleSheet("color: rgba(255, 255, 255, 0.95);")
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
            # Album previews are by-definition single-artist; playlists
            # only show artists if they actually span more than one.
            is_album = self._preview_kind == QueueKind.ALBUM
            self._populate_rows(
                self._preview_tracks,
                show_artist=(
                    False if is_album
                    else self._items_span_multiple_artists(
                        self._preview_tracks
                    )
                ),
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

        # Show the artist sub-line only when the queue actually spans
        # more than one artist. Previously this flipped on any
        # modified queue (drag-reorder, add-to-queue), which lit up
        # the sub-line under every track even when the user was just
        # reordering tracks within a single-artist album. Data-driven
        # check fires only when the queue genuinely crosses artists.
        show_artist = self._items_span_multiple_artists(items)
        # Disc dividers only apply to a *pristine* ALBUM context — once
        # the queue is reordered they no longer correspond to discs.
        multi_disc_enabled = ctx.kind == QueueKind.ALBUM and not is_modified

        self._populate_rows(items, show_artist, highlight_index, multi_disc_enabled)

    @staticmethod
    def _items_span_multiple_artists(items: List[Dict]) -> bool:
        """True iff the given track list contains tracks attributed to
        more than one AlbumArtist. Used to decide whether to render
        the per-row artist sub-line — single-artist queues hide it as
        redundant chrome."""
        seen = set()
        for t in items:
            artist = t.get("AlbumArtist") or ""
            if not artist:
                artists = t.get("Artists") or []
                if artists:
                    artist = artists[0] or ""
            seen.add((artist or "").strip().lower())
            if len(seen) > 1:
                return True
        return False

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
        # Flip the track-list ↔ empty-state surface. Preview mode
        # with no tracks reads as "this album has no tracks here yet"
        # (likely mid-fetch — we don't show empty until the load
        # actually returns nothing). Live mode with no tracks is the
        # "nothing queued" empty state.
        if not items:
            if self._preview_id:
                # Preview-mode empty: still loading or genuinely
                # empty source. Keep the list page so the rest of
                # the page (cover, lyrics) reads correctly while
                # tracks land.
                self._tracks_stack.setCurrentIndex(0)
            else:
                self._tracks_empty_state.set_state(
                    headline="Nothing queued",
                    sub="Pick an album, playlist, or song to start the queue.",
                )
                self._tracks_stack.setCurrentIndex(1)
        else:
            self._tracks_stack.setCurrentIndex(0)
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
        was_visible = self._play_cta.isVisible()
        self._play_cta.setVisible(in_preview)
        # First transition from "live → preview" auto-focuses the Play
        # button so a keyboard user who arrived here via Enter on an
        # album tile can tap Enter once more to start playback. Skip
        # if focus is already on a meaningful target (e.g. the track
        # list — user tabbed past the Play CTA on purpose) to avoid
        # yanking focus away.
        if in_preview and not was_visible:
            from PySide6.QtWidgets import QApplication
            focused = QApplication.focusWidget()
            if focused is None or not self.isAncestorOf(focused):
                self._play_cta.setFocus()
        has_fav_target = bool(self._preview_id or self.queue_mgr.context.source_id)
        self._fav_cta.setVisible(has_fav_target)

        # Download CTA — preview mode only (it acts on the previewed
        # album/playlist). Seed its state from the index; an in-flight
        # download corrects itself on the next download_progress tick.
        self._download_cta.setVisible(in_preview)
        if in_preview:
            try:
                from modules import offline
                downloaded = offline.is_downloaded(self._preview_id)
            except Exception:
                downloaded = False
            # Don't stomp a live "downloading" arc with a stale "idle".
            if self._download_cta.state() not in ("downloading", "pending"):
                self._download_cta.set_state(
                    "complete" if downloaded else "idle")

    def _on_download_cta(self):
        """Download / remove / cancel the previewed album. The action
        is read off the button's current state."""
        item_id = self._preview_id
        if not item_id:
            return
        from modules import offline
        state = self._download_cta.state()
        if state == "complete":
            # Mirror the library grid's confirm for a cascade removal.
            from PySide6.QtWidgets import QMessageBox
            name = self._preview_meta.get("Name") or "this album"
            if QMessageBox.question(
                self, "Remove download",
                f"Remove the downloaded copy of “{name}”?",
            ) != QMessageBox.StandardButton.Yes:
                return
            offline.remove(item_id)
            self._download_cta.set_state("idle")
        elif state in ("downloading", "pending"):
            # Click during a download cancels it (remove reaps the
            # partial node graph).
            offline.remove(item_id)
            self._download_cta.set_state("idle")
        else:  # idle / failed → start a download
            meta = dict(self._preview_meta or {})
            meta.setdefault("Id", item_id)
            offline.download(meta)
            self._download_cta.set_state("pending")

    @Slot(str, str, float)
    def _on_download_progress(self, item_id: str, state: str, fraction: float):
        """download_progress bus hook — only the previewed item drives
        the CTA. 'removed' falls back to idle."""
        if not self._preview_id or item_id != self._preview_id:
            return
        self._download_cta.set_state(
            "idle" if state == "removed" else state, fraction)

    def _on_play_cta_key(self, e):
        """Down arrow on the Play CTA hops focus into the track list,
        seeding its keyboard cursor on row 0 (or the active track if
        already playing). The keyboard parity for the visual layout:
        Play sits above the rows, so Down naturally walks into the
        first row from there.

        Enter/Return activates the button — a plain QPushButton only
        fires on Space (or Enter as a dialog default button), so
        without this an Enter press on the focused Play CTA does
        nothing. The keyboard user arrives here via Enter on an album
        tile and expects a second Enter to start the album."""
        if e.key() == Qt.Key.Key_Down and not e.modifiers():
            if self._list_container is not None:
                self._list_container.setFocus()
                e.accept()
                return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers():
            self._play_cta.animateClick()
            e.accept()
            return
        QPushButton.keyPressEvent(self._play_cta, e)

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

    def keyPressEvent(self, e):
        """Esc on the NP page is a two-stage dismiss: in preview
        mode it backs out to the live queue; on the live view it
        dismisses the whole page back to the previous surface
        (the host wires dismiss_requested to navigate back)."""
        if e.key() == Qt.Key.Key_Escape and not e.modifiers():
            if self._preview_id:
                self.clear_preview()
                e.accept()
                return
            self.dismiss_requested.emit()
            e.accept()
            return
        super().keyPressEvent(e)

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
