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
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import (
    Qt, QEvent, QObject, QSize, QTimer, QPropertyAnimation, QEasingCurve,
    Signal, Slot,
)
from PySide6.QtGui import QPixmap, QColor
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
from modules.design_tokens import (
    TYPE_TITLE, TYPE_CAPTION,
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
        self._row_widgets: List[_TrackRow] = []
        self._displayed_items_kind: str = ""  # "source" | "play"

        # Preview mode — when set, the page browses an album/playlist
        # without taking over the live queue. Click Play (or any track)
        # to install + play, which transitions back to live mode.
        # _preview_kind drives the right fetch endpoint and the
        # QueueKind installed when the user converts preview to live.
        self._preview_id: str = ""
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
        self._right_kicker.setStyleSheet(
            f"color: rgba(255,255,255,0.55); {type_qss(TYPE_MICRO)}"
        )
        # Padding on the side so the kicker aligns with the row content
        # below (rows have their own internal padding too).
        self._right_kicker.setContentsMargins(10, 4, 10, 0)
        v.addWidget(self._right_kicker)
        v.addSpacing(16)

        # Track list scroll area.
        self._list_scroll = QScrollArea(self)
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
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        self.bus.lyrics_font_size_changed.connect(self._on_lyrics_font_size_changed)
        self._lyrics_loaded.connect(self._on_lyrics_loaded)
        self._preview_meta_loaded.connect(self._on_preview_meta_loaded)
        self._preview_tracks_loaded.connect(self._on_preview_tracks_loaded)

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

    @Slot(list, int)
    def _on_queue_changed(self, _items: list, _index: int):
        # Preview mode is browsing a different list — ignore live-queue
        # mutations until the user exits preview.
        if self._preview_id:
            return
        self._refresh_track_list()

    @Slot(object)
    def _on_context_changed(self, _ctx: QueueContext):
        if self._preview_id:
            return
        self._refresh_track_list()

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        # Sync the heart icon when the live queue's source (album/
        # playlist) is favorited from elsewhere (e.g. JF Web).
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
        # Preview mode short-circuits the queue-driven path: we render
        # the previewed item's tracks in source order and only highlight
        # a row if it matches the live now-playing track (which can
        # happen when the user previews the same album they're listening
        # to).
        if self._preview_id:
            label = self._preview_meta.get("Name", "") or "Loading…"
            self._right_kicker.setText(f"BROWSING  ·  {label}")
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
        # Disc dividers only apply to ALBUM contexts — in PLAYLIST /
        # SHUFFLE / SEARCH every track has its own ParentIndexNumber
        # from its own album, so the dividers would interleave nonsensically.
        multi_disc_enabled = ctx.kind == QueueKind.ALBUM

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
        # Wipe and rebuild — both rows and any disc dividers from a
        # previous render. Stretch stays as the last layout item.
        while self._list_layout.count() > 1:
            it = self._list_layout.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._row_widgets.clear()

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
        # Pre-count tracks per disc so dividers can render "M tracks".
        disc_counts: Dict[int, int] = {}
        if multi_disc:
            for t in items:
                d = int(t.get("ParentIndexNumber") or 1)
                disc_counts[d] = disc_counts.get(d, 0) + 1

        # Insert above the trailing stretch (last item in the layout).
        insert_at = self._list_layout.count() - 1
        current_disc: Optional[int] = None
        for play_idx, item in enumerate(items):
            if multi_disc:
                disc = int(item.get("ParentIndexNumber") or 1)
                if disc != current_disc:
                    divider = _DiscDivider(
                        disc, disc_counts.get(disc, 0),
                        parent=self._list_container,
                    )
                    self._list_layout.insertWidget(insert_at, divider)
                    insert_at += 1
                    current_disc = disc
            row = _TrackRow(
                play_idx, item, show_artist, parent=self._list_container,
            )
            row.clicked.connect(self._on_row_clicked)
            row.set_current(play_idx == highlight_index)
            self._list_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._row_widgets.append(row)

        # Scroll the highlighted row into view (if any). Snapshot the
        # widget at schedule time — re-indexing _row_widgets at fire
        # time was racey: a queue change / preview->live transition
        # can rebuild the list during the 0-tick gap, leaving the
        # captured index pointing past the end. Guard the actual call
        # too because the snapshotted widget may have been deleteLater'd
        # in that same gap (C++ object gone -> RuntimeError on access).
        if 0 <= highlight_index < len(self._row_widgets):
            target_row = self._row_widgets[highlight_index]

            def _scroll_to_target(t=target_row):
                try:
                    self._list_scroll.ensureWidgetVisible(t, 0, 80)
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
        label.setStyleSheet(
            f"color: {color}; font-size: 13px; line-height: 1.6;"
        )
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
        # Cover load via the standard image URL helper.
        cover_url = self.api.get_image_url(item_id, "Primary", 600)
        if cover_url:
            load_image_async(
                f"{item_id}|nppage", cover_url,
                self.COVER_SIZE, self.COVER_SIZE,
                self._on_cover_loaded, rounded_radius=12,
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
