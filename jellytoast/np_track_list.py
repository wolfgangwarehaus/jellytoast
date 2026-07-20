"""
Now-playing page track list — model / delegate / view (MVC stack).

Extracted from ``now_playing_page.py``. ``_TracksModel`` holds the queue's
displayed items + per-row presentation state (current track, play index,
download / drag / hover flags) behind a role API; ``_TrackDelegate`` paints
each row (index column, title / subtitle, duration, disc dividers) with cached
fonts / metrics; ``_TracksListView`` is the ``QListView`` wiring drag-reorder,
context menus, keyboard navigation and the scroll / hover plumbing.

``NowPlayingPage`` (in ``now_playing_page.py``) owns an instance of each and
re-imports them, so ``from jellytoast.now_playing_page import _TracksModel`` /
``_TrackDelegate`` / ``_TracksListView`` still resolves.
"""

from typing import Dict, List, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QModelIndex,
    QPoint,
    QPropertyAnimation,
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
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_TINY,
    rad,
)
from jellytoast.player_state import (
    PlayerBus,
)
from jellytoast.theme import ink_rgb
from jellytoast.ui_helpers import (
    fmt_duration_ticks,
)


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
    KindRole = Qt.ItemDataRole.UserRole + 4  # "track" | "disc"
    DiscInfoRole = Qt.ItemDataRole.UserRole + 5  # (disc_num, count)
    PlayIndexRole = Qt.ItemDataRole.UserRole + 6  # int or -1
    IsDragGhostRole = Qt.ItemDataRole.UserRole + 7  # bool
    SuppressHoverRole = Qt.ItemDataRole.UserRole + 8  # bool
    AnimYOffsetRole = Qt.ItemDataRole.UserRole + 9  # float px
    IsPressedRole = Qt.ItemDataRole.UserRole + 10  # bool — mouse-down, pre-drag-threshold
    IsKbCursorRole = Qt.ItemDataRole.UserRole + 11  # bool — keyboard arrow-key cursor row
    IsDownloadedRole = Qt.ItemDataRole.UserRole + 12  # bool — track blob on disk

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
        # Track ids whose blob is in state ``complete`` for the active
        # server identity. Seeded on every set_state; patched off the
        # bus's download_progress signal so the track-row indicator
        # flips live without per-paint DB hits.
        self._downloaded_ids: set = set()
        try:
            PlayerBus.get().download_progress.connect(self._on_download_progress)
        except Exception:
            pass

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
            return self._drag_active and row == self._drag_src_row
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
        if role == self.IsDownloadedRole:
            if entry["kind"] != "track":
                return False
            it = entry.get("item") or {}
            iid = it.get("Id") or ""
            return bool(iid) and iid in self._downloaded_ids
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
            QModelIndex(),
            src_row,
            src_row,
            QModelIndex(),
            bmr_dest,
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

    def play_index_of_entry(self, src_row: int) -> int:
        """Returns the original play_index of a track entry at src_row,
        snapshotted BEFORE move_track is called (which re-numbers
        play indices)."""
        if 0 <= src_row < len(self._entries):
            e = self._entries[src_row]
            if e["kind"] == "track":
                return e["play_index"]
        return -1

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

    def id_at(self, row: int) -> str:
        """The track Id at a display row (or '' for a divider / OOB).
        Used to map a drag-reorder back to play-order by identity in
        source-order display, where play_index is the SOURCE index."""
        if 0 <= row < len(self._entries):
            entry = self._entries[row]
            if entry["kind"] == "track":
                return (entry["item"].get("Id") or "")
        return ""

    def row_for_play_index(self, play_index: int) -> int:
        for row, entry in enumerate(self._entries):
            if entry["kind"] == "track" and entry["play_index"] == play_index:
                return row
        return -1

    def set_state(
        self,
        items: List[Dict],
        current_play_index: int,
        show_artist: bool,
        drag_enabled: bool,
        multi_disc: bool = False,
    ):
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
                    new_entries.append(
                        {
                            "kind": "disc",
                            "disc_info": (d, disc_counts.get(d, 0)),
                        }
                    )
                    current_disc = d
                new_entries.append(
                    {
                        "kind": "track",
                        "item": t,
                        "play_index": play_idx,
                    }
                )
        else:
            for play_idx, t in enumerate(items):
                new_entries.append(
                    {
                        "kind": "track",
                        "item": t,
                        "play_index": play_idx,
                    }
                )
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
        # Re-seed the downloaded-id cache so the per-row indicator is
        # accurate the moment the view lays out. Cheap — one indexed
        # scan; further updates come off the download_progress signal.
        try:
            from jellytoast import offline

            self._downloaded_ids = offline.downloaded_item_ids()
        except Exception:
            self._downloaded_ids = set()
        self.endResetModel()

    def _on_download_progress(self, item_id: str, state: str, fraction: float):
        """Bus slot — patch the downloaded-id cache on complete/removed
        and refresh any row whose track item matches. Other states
        don't change "fully on disk" so the cache stays untouched."""
        if not item_id:
            return
        from jellytoast.offline import DownloadState as _DS

        if state == _DS.COMPLETE:
            if item_id in self._downloaded_ids:
                return
            self._downloaded_ids.add(item_id)
        elif state == _DS.REMOVED:
            if item_id not in self._downloaded_ids:
                return
            self._downloaded_ids.discard(item_id)
        else:
            return
        for row, entry in enumerate(self._entries):
            if entry.get("kind") != "track":
                continue
            it = entry.get("item") or {}
            if (it.get("Id") or "") == item_id:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [self.IsDownloadedRole])

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
        changed = active != self._drag_active or src_row != self._drag_src_row
        self._drag_active = active
        self._drag_src_row = src_row
        if changed and self._entries:
            top = self.index(0, 0)
            bot = self.index(len(self._entries) - 1, 0)
            self.dataChanged.emit(
                top,
                bot,
                [self.IsDragGhostRole, self.SuppressHoverRole],
            )


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_fonts()
        PlayerBus.get().theme_changed.connect(self._build_fonts)

    def _build_fonts(self):
        # Divider label — TYPE_MICRO bold.
        divider_font = QFont()
        divider_font.setPixelSize(TYPE_MICRO.size_px)
        divider_font.setBold(True)
        self._divider_font = divider_font
        self._fm_divider = QFontMetrics(divider_font)
        # Index column — JetBrains Mono caption. Bold flips per row
        # based on is_current, so cache both variants.
        mono_caption_regular = QFont("JetBrains Mono")
        mono_caption_regular.setPixelSize(TYPE_CAPTION.size_px)
        mono_caption_regular.setBold(False)
        self._idx_font_regular = mono_caption_regular
        mono_caption_bold = QFont(mono_caption_regular)
        mono_caption_bold.setBold(True)
        self._idx_font_bold = mono_caption_bold
        # Title — body, bold flip per row.
        title_regular = QFont()
        title_regular.setPixelSize(TYPE_BODY.size_px)
        title_regular.setBold(False)
        self._title_font_regular = title_regular
        self._fm_title_regular = QFontMetrics(title_regular)
        title_bold = QFont(title_regular)
        title_bold.setBold(True)
        self._title_font_bold = title_bold
        self._fm_title_bold = QFontMetrics(title_bold)
        # Subtitle — TYPE_TINY regular.
        sub_font = QFont()
        sub_font.setPixelSize(TYPE_TINY.size_px)
        sub_font.setBold(False)
        self._sub_font = sub_font
        self._fm_sub = QFontMetrics(sub_font)
        # Duration — JetBrains Mono caption, regular only.
        dur_font = QFont("JetBrains Mono")
        dur_font.setPixelSize(TYPE_CAPTION.size_px)
        dur_font.setBold(False)
        self._dur_font = dur_font

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

        # Cached on the delegate (`_build_fonts`); rebuilt on
        # `PlayerBus.theme_changed`.
        painter.setFont(self._divider_font)
        fm = self._fm_divider
        label = self.tr("Disc {0}  ·  {1} tracks").format(disc_num, count)
        label_w = fm.horizontalAdvance(label)
        label_rect = QRect(
            rect.x() + self.LEFT_PAD,
            rect.y() + 4,
            label_w + 4,
            rect.height() - 8,
        )
        # Theme-aware ink — ink_rgb() gives the (r,g,b) of the active
        # theme's foreground (white on dark, near-black on light); the
        # TEXT_FAINT QSS string can't go through QColor directly.
        ink = ink_rgb()
        painter.setPen(QColor(*ink, 140))
        painter.drawText(
            label_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )
        # Hairline from end of label text to the right edge.
        line_x = label_rect.right() + 8
        line_y = rect.center().y()
        painter.setPen(QColor(*ink, 20))
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

        # Re-read theme constants every paint so live-accent + a live
        # theme switch both flow through with a viewport().update().
        from jellytoast.ui_helpers import ACCENT as _ACCENT

        ink = ink_rgb()

        # Hover wash — subtle highlight when the cursor's over the
        # row. Suppressed while a drag is in flight so the rest of
        # the list reads as static while the user rearranges. The
        # pressed-but-not-yet-dragged state paints a deeper wash so
        # the user gets a "you're holding this" cue before the
        # startDragDistance threshold is crossed.
        if is_pressed:
            inset = rect.adjusted(self.LEFT_PAD - 4, 2, -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), rad(6), rad(6))
            painter.fillPath(path, QColor(*ink, 28))
        elif is_kb_cursor and not suppress_hover:
            # Keyboard-arrow cursor — between hover and press in
            # weight so the user knows which row Enter would play
            # without it looking like they're already clicking.
            inset = rect.adjusted(self.LEFT_PAD - 4, 2, -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), rad(6), rad(6))
            painter.fillPath(path, QColor(*ink, 18))
        elif not suppress_hover and option.state & QStyle.StateFlag.State_MouseOver:
            inset = rect.adjusted(self.LEFT_PAD - 4, 2, -(self.LEFT_PAD - 4), -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), rad(6), rad(6))
            painter.fillPath(path, QColor(*ink, 10))

        # Index column — IndexNumber when present else play-position.
        # Colour priority: current row > downloaded > idle. Both
        # "current" and "downloaded" paint accent; current also bolds.
        # The downloaded tint replaces the inline check glyph the
        # earlier slice tried — keeps the rows visually quiet but
        # still makes it obvious at-a-glance which tracks are on disk.
        is_downloaded = bool(index.data(_TracksModel.IsDownloadedRole))
        idx_n = item.get("IndexNumber") or (play_index + 1)
        idx_rect = QRect(
            rect.x() + self.LEFT_PAD,
            rect.y(),
            self.IDX_W,
            rect.height(),
        )
        painter.setFont(self._idx_font_bold if is_current else self._idx_font_regular)
        if is_current or is_downloaded:
            painter.setPen(QColor(_ACCENT))
        else:
            painter.setPen(QColor(*ink, 115))
        painter.drawText(
            idx_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            str(idx_n),
        )

        # Title + duration column geometry.
        text_x = idx_rect.right() + self.COL_GAP
        dur_x = rect.right() - self.RIGHT_PAD - self.DUR_W
        text_w = max(0, dur_x - text_x - self.COL_GAP)

        title_font = self._title_font_bold if is_current else self._title_font_regular
        fm_title = self._fm_title_bold if is_current else self._fm_title_regular
        painter.setFont(title_font)

        # Resolve subtitle text first so we know whether to split the
        # vertical space.
        sub = ""
        if show_artist:
            artists = item.get("Artists") or []
            sub = ", ".join(artists) if artists else (item.get("AlbumArtist", "") or "")
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

        title = item.get("Name") or self.tr("Unknown")
        if is_current:
            painter.setPen(QColor(_ACCENT))
        else:
            painter.setPen(QColor(*ink, 224))
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight, text_w),
        )

        if sub_rect is not None and sub:
            painter.setFont(self._sub_font)
            painter.setPen(QColor(*ink, 140))
            painter.drawText(
                sub_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._fm_sub.elidedText(sub, Qt.TextElideMode.ElideRight, text_w),
            )

        # Duration column.
        dur_ticks = item.get("RunTimeTicks", 0) or 0
        if dur_ticks:
            painter.setFont(self._dur_font)
            painter.setPen(QColor(*ink, 140))
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
    ``reorder_requested`` (the PAGE maps it to play-order + commits to the
    bus) + model.move_track to keep the visual order in sync until the
    QueueManager re-renders."""

    track_clicked = Signal(int)
    track_context_menu = Signal(int, QPoint)
    drag_state_changed = Signal(bool)
    # (src_play_index, dest_play_index, src_id, dest_id). The play_index
    # values are correct in play-order display but are SOURCE indices in
    # source-order display, so the page re-maps by Id there (mirrors the
    # context-menu remove fix) before emitting bus.queue_move_item.
    reorder_requested = Signal(int, int, str, str)

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
    EDGE_SCROLL_MIN_SPEED = 2  # px/tick at zone outer edge
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

    def __init__(self, model: _TracksModel, delegate: _TrackDelegate, parent=None):
        super().__init__(parent)
        self._model = model
        self._delegate = delegate
        self.setModel(model)
        self.setItemDelegate(delegate)
        self.setMouseTracking(True)
        # Heterogeneous heights (tracks vs disc dividers) — uniform
        # sizes can't be enabled.
        self.setUniformItemSizes(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # No selection — click = play, not select. Qt's InternalMove
        # drag isn't used (we drive the drag manually below) so we
        # don't need selectedIndexes() to be populated.
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        self._drag_src_id: str = ""  # source track Id, for source-order remap
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
        if (
            self._press_row < 0
            or self._press_pos is None
            or not (e.buttons() & Qt.MouseButton.LeftButton)
        ):
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
                if self._model.data(idx, _TracksModel.KindRole) == "track" and self._model.data(
                    idx, _TracksModel.IsCurrentRole
                ):
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
            if self._model.data(idx, _TracksModel.KindRole) == "track" and self._model.data(
                idx, _TracksModel.IsCurrentRole
            ):
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
        self._drag_src_id = self._model.id_at(src_row)
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
        self._float_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
        elif (
            cy > h - zone and self.verticalScrollBar().value() < self.verticalScrollBar().maximum()
        ):
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
                self._drag_src_row,
                target_row,
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
        speed = (
            self.EDGE_SCROLL_MIN_SPEED
            + (self.EDGE_SCROLL_MAX_SPEED - self.EDGE_SCROLL_MIN_SPEED) * eased
        )
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
                self._drag_src_row,
                target_row,
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
        if (
            self._float_label.y() != self._float_target_y
            or self._float_label.x() != self._float_target_x
        ):
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
        if 0 <= orig_row < rc and orig_row != self._drag_src_row:
            new_row = self._do_move_with_anim(
                self._drag_src_row,
                orig_row,
            )
            if new_row >= 0:
                self._drag_src_row = new_row
                self._model.set_drag_state(active=True, src_row=new_row)
        self._teardown_drag(snap_float=False, commit=False)

    def _teardown_drag(self, snap_float: bool, commit: bool):
        final_row = self._drag_src_row
        src_play_orig = getattr(self, "_drag_src_play_orig", -1)
        src_id = self._drag_src_id  # captured before the reset below
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
        self._drag_src_id = ""
        self.drag_state_changed.emit(False)
        self._press_row = -1
        self._press_pos = None
        # Snap the float to its smooth-follow target so the fade
        # lands cleanly at the new slot. Cancel skips this so the
        # float fades where the user released, not at the restored
        # slot — feels more like "release and undo".
        if snap_float and self._float_label is not None:
            self._float_label.move(
                self._float_target_x,
                self._float_target_y,
            )
        self._start_drop_animation()
        if not commit:
            return
        if final_row < 0 or src_play_orig < 0:
            return
        dest_play = self._model.play_index_at(final_row)
        if dest_play < 0 or dest_play == src_play_orig:
            return
        # Hand the move to the PAGE, which knows the display mode + the
        # play-order. In play-order display the play_index values above are
        # the real play-order indices and the page passes them through;
        # in SOURCE-order display they're source indices, so the page
        # re-maps by Id — src by its own Id, dest by the track the drop
        # landed AFTER (the row above final_row in the post-drag display;
        # empty = dropped at the very top → play-order 0). Mirrors the
        # context-menu remove fix instead of mis-feeding source indices to
        # QueueManager.move_item as if they were play-order.
        anchor_id = self._model.id_at(final_row - 1) if final_row > 0 else ""
        self.reorder_requested.emit(src_play_orig, dest_play, src_id, anchor_id)

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
                    if self._model.data(pidx, _TracksModel.KindRole) == "track":
                        return prev
            else:
                for nxt in range(row + 1, rc):
                    nidx = self._model.index(nxt, 0)
                    if self._model.data(nidx, _TracksModel.KindRole) == "track":
                        return nxt
            return -1
        # Cursor outside any row — past the bottom of the last row →
        # land on the last track row.
        for row in range(rc - 1, -1, -1):
            ridx = self._model.index(row, 0)
            if self._model.data(ridx, _TracksModel.KindRole) == "track":
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
        from jellytoast.ui_helpers import ACCENT as _ACCENT

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
            inset_x,
            float(inset_y),
            w - 2 * inset_x,
            h - 2 * inset_y,
        )

        # Resolve accent → RGB triplet.
        from jellytoast.theme import _hex_to_rgb

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
            path.addRoundedRect(inner, float(rad(6)), float(rad(6)))
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
