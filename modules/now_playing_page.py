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
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import (
    Qt, QEvent, QObject, QSize, QTimer, QPropertyAnimation, QEasingCurve,
    Signal, Slot,
)
from PySide6.QtGui import QPixmap, QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
)

from modules.player_state import (
    PlayerBus, NowPlaying, get_now_playing, QueueKind, QueueContext,
)
from modules.ui_helpers import (
    load_image_async, fmt_duration_ticks, ACCENT, TEXT, TEXT_DIM,
    TEXT_FAINT, BORDER,
)
from modules.icons import icon
from modules.jellyfin_api import get_api


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


class _TrackRow(QFrame):
    """One track row in the right pane. Index, title (+ optional artist
    when not in an album-context view), duration. Clickable; emits the
    play-order index that should be jumped to."""
    clicked = Signal(int)

    def __init__(self, play_index: int, item: Dict, show_artist: bool,
                 parent=None):
        super().__init__(parent)
        self._play_index = play_index
        self._is_current = False
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
                    "color: rgba(255,255,255,0.55); font-size: 11px;"
                )
                text_col.addWidget(self._sub)
        layout.addLayout(text_col, 1)

        # Duration column — fixed width so all rows align on the right.
        dur_ticks = item.get("RunTimeTicks", 0) or 0
        self._dur = QLabel(fmt_duration_ticks(dur_ticks) if dur_ticks else "")
        self._dur.setFixedWidth(56)
        self._dur.setStyleSheet(
            "color: rgba(255,255,255,0.55); font-size: 12px; "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )
        self._dur.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._dur)

    @staticmethod
    def _idx_css(active: bool) -> str:
        if active:
            return (
                f"color: {ACCENT}; font-size: 12px; font-weight: 700; "
                "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
            )
        return (
            "color: rgba(255,255,255,0.45); font-size: 12px; "
            "font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;"
        )

    @staticmethod
    def _title_css(active: bool) -> str:
        if active:
            return f"color: {ACCENT}; font-size: 13px; font-weight: 600;"
        return "color: rgba(255,255,255,0.88); font-size: 13px;"

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._play_index)
        super().mousePressEvent(e)

    def set_current(self, is_current: bool):
        if is_current == self._is_current:
            return
        self._is_current = is_current
        self._title.setStyleSheet(self._title_css(active=is_current))
        self._idx.setStyleSheet(self._idx_css(active=is_current))


class NowPlayingPage(QWidget):
    """Full-screen now-playing view. Owned by JellyToastWindow; swapped
    into the content stack when the user clicks the now-playing pill
    in the transport bar."""

    # Emitted when the user wants to dismiss the page (back button).
    dismiss_requested = Signal()
    # Internal — fires from the lyrics worker thread; the auto-routed
    # queued connection delivers it on the main thread so we can touch
    # widgets safely. Without this we'd be calling QTimer.singleShot
    # from a thread that has no event loop and the callback would never
    # fire.
    _lyrics_loaded = Signal(str, object)

    # Panes split 50/50; cover sits at the top of the left pane and the
    # lyrics column owns the visual weight underneath. Apple Music's
    # macOS lyrics view is the reference — the cover anchors, lyrics
    # are the focal point.
    COVER_SIZE = 200          # square art

    def __init__(self, queue_mgr, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_api()
        self.queue_mgr = queue_mgr
        self._lyrics_cache = _LyricsCache()
        self._lyrics_loading_for: str = ""  # in-flight item_id
        self._cover_orig: Optional[QPixmap] = None
        self._row_widgets: List[_TrackRow] = []
        self._displayed_items_kind: str = ""  # "source" | "play"

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

        # Auto-hide scrollbars on both panes — they appear dim white on
        # scroll/hover and fade out after ~1s idle. Constructed last so
        # both scroll areas already have their viewports.
        self._lyrics_fader = _ScrollbarFader(self._lyrics_scroll)
        self._list_fader = _ScrollbarFader(self._list_scroll)

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

        # Title 18pt/600. Lyrics own the moment; title is the label.
        self._title = QLabel("Nothing playing")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        self._title.setStyleSheet("color: rgba(255, 255, 255, 0.95);")
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self._title)
        v.addSpacing(4)

        # Artist · Album. The bullet is rendered in a slightly fainter
        # shade than the surrounding text so the eye treats the whole
        # line as one phrase.
        self._subtitle = QLabel("")
        sub_font = QFont()
        sub_font.setPointSize(12)
        sub_font.setWeight(QFont.Weight.Medium)
        self._subtitle.setFont(sub_font)
        self._subtitle.setStyleSheet("color: rgba(255, 255, 255, 0.62);")
        self._subtitle.setTextFormat(Qt.TextFormat.RichText)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self._subtitle)
        v.addSpacing(20)

        # Lyrics scroll area — fills the remaining vertical space.
        self._lyrics_scroll = QScrollArea()
        self._lyrics_scroll.setWidgetResizable(True)
        self._lyrics_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._lyrics_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
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
        v.addWidget(self._lyrics_scroll, 1)

        # Smooth-scroll animation on the lyrics scrollbar — used by the
        # synced-lyrics auto-scroll. 300ms ease-out per the design pass.
        self._lyrics_anim = QPropertyAnimation(
            self._lyrics_scroll.verticalScrollBar(), b"value", self
        )
        self._lyrics_anim.setDuration(300)
        self._lyrics_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        return pane

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
        self._right_kicker = QLabel("UP NEXT")
        kicker_font = QFont()
        kicker_font.setPointSize(11)
        kicker_font.setWeight(QFont.Weight.DemiBold)
        self._right_kicker.setFont(kicker_font)
        self._right_kicker.setStyleSheet(
            "color: rgba(255,255,255,0.55); letter-spacing: 1.5px;"
        )
        # Padding on the side so the kicker aligns with the row content
        # below (rows have their own internal padding too).
        self._right_kicker.setContentsMargins(10, 4, 10, 0)
        v.addWidget(self._right_kicker)
        v.addSpacing(16)

        # Track list scroll area.
        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 8)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._list_scroll.setWidget(self._list_container)
        v.addWidget(self._list_scroll, 1)

        return pane

    # ── Bus wiring ──────────────────────────────────────────────────────────

    def _connect_bus(self):
        self.bus.playback_started.connect(self._on_playback_started)
        self.bus.playback_stopped.connect(self._on_playback_stopped)
        self.bus.queue_changed.connect(self._on_queue_changed)
        self.bus.queue_context_changed.connect(self._on_context_changed)
        self.bus.position_updated.connect(self._on_position_updated)
        self._lyrics_loaded.connect(self._on_lyrics_loaded)

    @Slot(object)
    def _on_playback_started(self, np: NowPlaying):
        self._refresh_now_playing(np)

    @Slot()
    def _on_playback_stopped(self):
        self._title.setText("Nothing playing")
        self._subtitle.setText("")
        self._cover.clear()
        self._cover_orig = None
        self._set_lyrics_text("")

    @Slot(list, int)
    def _on_queue_changed(self, _items: list, _index: int):
        self._refresh_track_list()

    @Slot(object)
    def _on_context_changed(self, _ctx: QueueContext):
        self._refresh_track_list()

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

        if np.thumb_url:
            load_image_async(
                f"{np.item_id}|nppage", np.thumb_url,
                self.COVER_SIZE, self.COVER_SIZE,
                self._on_cover_loaded, rounded_radius=12,
            )
        self._fetch_lyrics(np.item_id)

    def _on_cover_loaded(self, pix: QPixmap):
        self._cover_orig = pix
        if pix.isNull():
            return
        self._cover.setPixmap(
            pix.scaled(
                self.COVER_SIZE, self.COVER_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _refresh_track_list(self):
        ctx = self.queue_mgr.context
        # Single ALL-CAPS kicker. When there's a human-readable source
        # (album / playlist name) we append it after the kind label —
        # "ALBUM · 19", "PLAYLIST · Coffeehouse" — so the user has the
        # full context in one glance without a separate big title.
        kind_label, default_label = {
            QueueKind.ALBUM: ("ALBUM", "Album"),
            QueueKind.PLAYLIST: ("PLAYLIST", "Playlist"),
            QueueKind.ARTIST: ("ARTIST", "Artist"),
            QueueKind.SHUFFLE: ("LIBRARY SHUFFLE", "Library shuffle"),
            QueueKind.SEARCH: ("SEARCH RESULTS", "Search"),
            QueueKind.MANUAL: ("UP NEXT", "Up next"),
            QueueKind.INSTANT_MIX: ("INSTANT MIX", "Instant mix"),
        }.get(ctx.kind, ("UP NEXT", "Up next"))
        if ctx.source_label and ctx.source_label != default_label:
            self._right_kicker.setText(f"{kind_label}  ·  {ctx.source_label}")
        else:
            self._right_kicker.setText(kind_label)

        # Pick the right items list per the context's natural ordering.
        if ctx.kind in _SOURCE_ORDER_KINDS:
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
        # ALBUM, where every track is the same artist by definition).
        show_artist = ctx.kind != QueueKind.ALBUM

        self._populate_rows(items, show_artist, highlight_index)

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
                       highlight_index: int):
        # Wipe and rebuild. The list is short enough (album / a few
        # hundred tracks for shuffle) that full repaint is cheaper than
        # diffing — and avoids state drift between row widgets.
        for row in self._row_widgets:
            row.setParent(None)
            row.deleteLater()
        self._row_widgets.clear()

        # Insert above the trailing stretch (last item in the layout).
        insert_at = self._list_layout.count() - 1
        for play_idx, item in enumerate(items):
            row = _TrackRow(play_idx, item, show_artist)
            row.clicked.connect(self._on_row_clicked)
            row.set_current(play_idx == highlight_index)
            self._list_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._row_widgets.append(row)

        # Scroll the highlighted row into view (if any).
        if 0 <= highlight_index < len(self._row_widgets):
            QTimer.singleShot(
                0,
                lambda: self._list_scroll.ensureWidgetVisible(
                    self._row_widgets[highlight_index], 0, 80
                ),
            )

    @Slot(int)
    def _on_row_clicked(self, displayed_index: int):
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
        threading.Thread(
            target=self._lyrics_worker, args=(item_id,), daemon=True,
        ).start()

    def _lyrics_worker(self, item_id: str):
        # Off-thread network call. We hop back to the main thread via a
        # signal — Qt auto-uses QueuedConnection across threads, which
        # is safe (the worker has no event loop, so QTimer.singleShot
        # from here would silently never fire).
        try:
            payload = self.api.get_lyrics(item_id)
        except Exception:
            payload = None
        self._lyrics_loaded.emit(item_id, payload)

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
        for i, w in enumerate(widgets):
            self._lyrics_layout.insertWidget(i, w)
        self._lyrics_scroll.verticalScrollBar().setValue(0)

    def _set_lyrics_text(self, text: str, muted: bool = False):
        """Single-paragraph fallback used for status messages ("Loading…",
        "No lyrics available"). Clears any previously built per-line
        widgets so the synced highlight doesn't try to address them."""
        self._lyrics_widgets = []
        self._lyrics_starts_ms = []
        self._lyrics_synced = False
        self._active_line_idx = -1
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
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; line-height: 1.6;"
        )
        self._lyrics_layout.insertWidget(0, label)
        self._lyrics_scroll.verticalScrollBar().setValue(0)

    # Distance-from-active opacity falloff. Apple Music's lyrics view
    # is the genre reference: the active line is the loudest object on
    # the page, with surrounding lines fading by distance so the eye
    # naturally tracks the now-moment without losing the few lines
    # ahead. Index by absolute distance from the active line.
    _FALLOFF = (0.95, 0.70, 0.45, 0.28, 0.18)

    def _lyric_line_css(self, distance: int) -> str:
        if distance == 0:
            # Active: 22pt / 700 / nearly opaque. Letter-spacing on
            # active only — gives the line a touch more presence.
            return (
                "color: rgba(255,255,255,0.95); font-size: 22px; "
                "font-weight: 700; letter-spacing: 0.2px; "
                "padding: 12px 0;"
            )
        idx = min(distance, len(self._FALLOFF) - 1)
        opacity = self._FALLOFF[idx]
        # Inactive: 16pt / 500 with distance falloff.
        return (
            f"color: rgba(255,255,255,{opacity:.2f}); "
            "font-size: 16px; font-weight: 500; "
            "padding: 6px 0;"
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
        self._lyrics_anim.stop()
        self._lyrics_anim.setStartValue(bar.value())
        self._lyrics_anim.setEndValue(target)
        self._lyrics_anim.start()
