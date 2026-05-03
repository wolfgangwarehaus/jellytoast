"""
Floating mini player.
Two modes:
  - Compact: 360×96 — artwork + title + transport (great for audio)
  - Expanded: 320×420 — large artwork + full controls + queue peek

The mini player is frameless, always-on-top, and draggable.
"""

from PySide6.QtCore import Qt, QPoint, QSize, QTimer, Slot
from PySide6.QtGui import QPixmap, QFont, QColor, QPainter, QPainterPath, QCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
    QApplication, QFrame, QStackedWidget, QSizePolicy,
)

from modules.player_state import PlayerBus, get_now_playing, NowPlaying
from modules.ui_helpers import (
    load_image_async, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM, TEXT_FAINT,
    skip_taskbar_x11, enable_kde_blur, MINI_BODY_COLOR, ScrubbableSlider,
)
from modules.icons import icon, accent_icon

QWIDGETSIZE_MAX = 16777215
BODY_RADIUS = 12


class _MarqueeLabel(QLabel):
    """QLabel that scrolls its text horizontally when the text exceeds the
    label's width. Pauses briefly at the start of each cycle so the beginning
    of the title is readable before it moves.

    Pacing: 30fps repaint (smooth) at a sub-pixel speed (slow). The 0.5
    px/tick ≈ 15 px/sec — about a third of typical marquee speed, tuned
    for ambient/glanceable use rather than pulling the eye."""
    SPEED_PX_PER_TICK = 0.5
    GAP_PX = 48
    PAUSE_TICKS = 90    # ~3s at 33ms tick — longer dwell on the start
    TICK_MS = 33

    def __init__(self, parent=None):
        super().__init__(parent)
        self._marquee_text = ""
        self._marquee_offset_f = 0.0
        self._marquee_offset = 0
        self._pause = self.PAUSE_TICKS
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(self.TICK_MS)

    def setText(self, text: str):
        if text == self._marquee_text:
            return
        self._marquee_text = text or ""
        self._marquee_offset_f = 0.0
        self._marquee_offset = 0
        self._pause = self.PAUSE_TICKS
        super().setText(self._marquee_text)
        self._update_marquee_state()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_marquee_state()

    def _text_width(self) -> int:
        return self.fontMetrics().horizontalAdvance(self._marquee_text)

    def _needs_scroll(self) -> bool:
        return bool(self._marquee_text) and self._text_width() > self.width()

    def _update_marquee_state(self):
        if self._needs_scroll():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._marquee_offset_f = 0.0
            self._marquee_offset = 0
            self.update()

    def _tick(self):
        if self._pause > 0:
            self._pause -= 1
            return
        cycle = self._text_width() + self.GAP_PX
        self._marquee_offset_f = (self._marquee_offset_f + self.SPEED_PX_PER_TICK) % cycle
        # Re-pause when we wrap back to the start so the user gets another
        # readable dwell on the head of the text.
        if self._marquee_offset_f < self.SPEED_PX_PER_TICK:
            self._pause = self.PAUSE_TICKS
        self._marquee_offset = int(self._marquee_offset_f)
        self.update()

    def paintEvent(self, e):
        if not self._needs_scroll():
            super().paintEvent(e)
            return
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        fm = p.fontMetrics()
        baseline = (self.height() + fm.ascent() - fm.descent()) // 2
        text_w = fm.horizontalAdvance(self._marquee_text)
        x = -self._marquee_offset
        p.drawText(x, baseline, self._marquee_text)
        p.drawText(x + text_w + self.GAP_PX, baseline, self._marquee_text)


def _round_left_corners(pix: QPixmap, radius: int) -> QPixmap:
    """Round only the top-left and bottom-left corners (right side stays
    square so it meets the controls strip flush in compact mode)."""
    if pix.isNull() or radius <= 0:
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = pix.width(), pix.height()
    path = QPainterPath()
    path.moveTo(w, 0)
    path.lineTo(radius, 0)
    path.quadTo(0, 0, 0, radius)
    path.lineTo(0, h - radius)
    path.quadTo(0, h, radius, h)
    path.lineTo(w, h)
    path.closeSubpath()
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out


def _round_all_corners(pix: QPixmap, radius: int) -> QPixmap:
    """Return a copy of pix with all four corners rounded."""
    if pix.isNull() or radius <= 0:
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(pix.width()), float(pix.height()),
                        radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _icon_button(name: str, size: int = 30, icon_size: int | None = None,
                 accent: bool = False) -> QPushButton:
    """Mini-player transport button. Uses the shared SVG icon registry so
    every player chrome (top bar, bottom bar, mini) shares glyph geometry.
    `accent=True` paints the icon in accent (use for the play button when
    you want a primary-action emphasis).

    NoFocus so the button never receives keyboard focus — otherwise Qt's
    theme paints a blue focus ring when focus snaps here (e.g. after the
    mode toggle, focus would land on the first transport button in the
    new active stack page)."""
    btn = QPushButton()
    btn.setIcon(accent_icon(name) if accent else icon(name))
    isz = icon_size if icon_size is not None else max(14, int(size * 0.55))
    btn.setIconSize(QSize(isz, isz))
    btn.setFixedSize(size, size)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setStyleSheet("""
        QPushButton {
            background: transparent; border: none; border-radius: 8px;
        }
        QPushButton:hover { background: rgba(255, 255, 255, 0.10); }
        QPushButton:pressed { background: rgba(255, 255, 255, 0.16); }
    """)
    return btn


# ── Compact mode ─────────────────────────────────────────────────────────────

class _SubField:
    """Duck-typed setText forwarder used by _CompactBar so the parent panel
    can call panel.artist.setText(...) / panel.album.setText(...) the same
    way it does on the expanded panel — except here both feed a single
    joined "artist · album" subtitle label."""
    def __init__(self, owner, attr_name: str):
        self._owner = owner
        self._attr = attr_name

    def setText(self, text: str):
        setattr(self._owner, self._attr, text or "")
        self._owner._refresh_subtitle()


class _CompactBar(QWidget):
    """Super-compact view: tiny square album art on the left, three stacked
    rows on the right — title (own row, full width so it rarely needs to
    marquee), artist · album subtitle, and a progress bar with three
    transport buttons. Shuffle / repeat live only in the expanded view."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QHBoxLayout(self)
        # No bezel — cover fills the left side edge-to-edge.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Album art — square, full body height. Resized by
        # FloatingMiniPlayer.resizeEvent. All four corners are rounded with
        # BODY_RADIUS so the left corners match the body and the right
        # corners look like a rounded card edge against the right strip.
        self.thumb = QLabel()
        self.thumb.setFixedSize(96, 96)
        self.thumb.setStyleSheet("background: transparent;")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: QPixmap | None = None
        layout.addWidget(self.thumb)

        right = QVBoxLayout()
        right.setContentsMargins(12, 6, 12, 6)
        right.setSpacing(2)

        # Row 1: title — its own row, centered in the right strip so the
        # whole right side reads as a tidy card. Marquees only when the title
        # is too long for the strip.
        self.title = _MarqueeLabel()
        self.title.setText("Nothing playing")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 12px; font-weight: 500;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        # Subtitle holds artist + album joined with a bullet. The parent
        # writes via panel.artist.setText / panel.album.setText, so we
        # expose two duck-typed forwarders that update the joined label.
        # _MarqueeLabel scrolls slowly when artist + album exceeds the
        # narrow right strip's width (common for long album titles).
        self.subtitle = _MarqueeLabel()
        self.subtitle.setText("")
        self.subtitle.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._artist_text = ""
        self._album_text = ""
        self.artist = _SubField(self, "_artist_text")
        self.album = _SubField(self, "_album_text")

        # Equal stretches between every row so the four lines distribute
        # evenly across the strip's height.
        right.addWidget(self.title)
        right.addStretch(1)
        right.addWidget(self.subtitle)
        right.addStretch(1)

        # Row 3: minimal progress bar — narrower than the strip, hairline
        # groove, tiny handle. Left-aligned with stretch so it doesn't run
        # all the way to the right edge.
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(0)
        self.progress = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(2)
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, 1000)
        # Hairline progress: 1px groove, no visible handle. Still draggable —
        # clicking the groove jumps the value.
        self.progress.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 1px; background: rgba(255,255,255,0.10); border-radius: 0px;
            }}
            QSlider::sub-page:horizontal {{
                background: rgba(255,255,255,0.55); border-radius: 0px;
            }}
            QSlider::handle:horizontal {{
                width: 0px; height: 0px; margin: 0; background: transparent;
                border: none;
            }}
        """)
        self.progress.sliderMoved.connect(self._on_seek)
        progress_row.addStretch(1)
        progress_row.addWidget(self.progress)
        progress_row.addStretch(1)
        right.addLayout(progress_row)
        right.addStretch(1)

        # Row 4: transport buttons, centered under the progress bar. Force
        # vertical centering so prev/next align on the play button's centerline
        # (their natural cell anchors at the top otherwise).
        controls_row = QHBoxLayout()
        controls_row.setSpacing(2)
        self.prev_btn = _icon_button("prev", 30)
        self.play_btn = _icon_button("play", 36, icon_size=18)
        self.next_btn = _icon_button("next", 30)
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())
        controls_row.addStretch()
        controls_row.addWidget(self.prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addStretch()
        right.addLayout(controls_row)

        layout.addLayout(right, 1)

    def _refresh_subtitle(self):
        a = (self._artist_text or "").strip()
        b = (self._album_text or "").strip()
        if a and b:
            self.subtitle.setText(f"{a}  ·  {b}")
        else:
            self.subtitle.setText(a or b)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        if self._cover_orig is None or self._cover_orig.isNull():
            return
        s = self.thumb.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        scaled = self._cover_orig.scaled(
            s, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Non-square source art comes back oversized after Expanding —
        # center-crop to the label rect so the rounded corners we draw
        # below land at the visible edges instead of getting clipped off.
        if scaled.width() != s.width() or scaled.height() != s.height():
            x = (scaled.width() - s.width()) // 2
            y = (scaled.height() - s.height()) // 2
            scaled = scaled.copy(x, y, s.width(), s.height())
        scaled = _round_all_corners(scaled, BODY_RADIUS)
        self.thumb.setPixmap(scaled)


# ── Expanded mode ────────────────────────────────────────────────────────────

# Bar height shared by both modes. In compact the bar fills the right side
# (full window height). In expanded the bar sits below the cover with this
# height, and the window's total height is body_w + BAR_HEIGHT.
_BAR_HEIGHT = 96


class _ExpandedPanel(QWidget):
    """Cover on top, then a bar that mirrors the compact view's right
    strip exactly — title (marquee), subtitle (marquee), hairline
    scrubbable progress, three transport buttons (prev/play/next).

    No shuffle/repeat: that's intentional symmetry with the compact
    view, so toggling between modes only changes where the cover lives
    (left vs top), not the bar's grammar. The bar instance itself isn't
    shared across modes — it's a duplicated layout — but FloatingMini-
    Player's toggle anchors the window so the bar appears stationary
    during the swap."""

    BAR_HEIGHT = _BAR_HEIGHT

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Album art — full body width, square. FloatingMiniPlayer.
        # resizeEvent sets the fixed size to body_w × body_w on every
        # resize. The source pixmap is kept at high res so rescaling
        # stays crisp as the player grows.
        self.cover = QLabel()
        self.cover.setFixedSize(320, 320)
        self.cover.setStyleSheet("background: transparent;")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: QPixmap | None = None
        layout.addWidget(self.cover)

        # Bar — visually identical to compact's right strip.
        bar = QWidget()
        bar.setFixedHeight(self.BAR_HEIGHT)
        bar.setStyleSheet("background: transparent;")
        right = QVBoxLayout(bar)
        right.setContentsMargins(12, 6, 12, 6)
        right.setSpacing(2)

        self.title = _MarqueeLabel()
        self.title.setText("Nothing playing")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 12px; font-weight: 500;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        # Joined "artist · album" subtitle. Same _SubField forwarder
        # pattern as the compact view so the parent's panel.artist /
        # panel.album setters work uniformly.
        self.subtitle = _MarqueeLabel()
        self.subtitle.setText("")
        self.subtitle.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._artist_text = ""
        self._album_text = ""
        self.artist = _SubField(self, "_artist_text")
        self.album = _SubField(self, "_album_text")

        right.addWidget(self.title)
        right.addStretch(1)
        right.addWidget(self.subtitle)
        right.addStretch(1)

        # Progress — hairline 1px groove, fixed-width centered (matches
        # the compact bar's progress styling exactly).
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(0)
        self.progress = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(2)
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 1px; background: rgba(255,255,255,0.10); border-radius: 0px;
            }}
            QSlider::sub-page:horizontal {{
                background: rgba(255,255,255,0.55); border-radius: 0px;
            }}
            QSlider::handle:horizontal {{
                width: 0px; height: 0px; margin: 0; background: transparent;
                border: none;
            }}
        """)
        self.progress.sliderMoved.connect(self._on_seek)
        progress_row.addStretch(1)
        progress_row.addWidget(self.progress)
        progress_row.addStretch(1)
        right.addLayout(progress_row)
        right.addStretch(1)

        # Transport — prev / play / next, matching compact.
        controls_row = QHBoxLayout()
        controls_row.setSpacing(2)
        self.prev_btn = _icon_button("prev", 30)
        self.play_btn = _icon_button("play", 36, icon_size=18)
        self.next_btn = _icon_button("next", 30)
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())
        controls_row.addStretch()
        controls_row.addWidget(self.prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addStretch()
        right.addLayout(controls_row)

        layout.addWidget(bar)

    def _refresh_subtitle(self):
        a = (self._artist_text or "").strip()
        b = (self._album_text or "").strip()
        if a and b:
            self.subtitle.setText(f"{a}  ·  {b}")
        else:
            self.subtitle.setText(a or b)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        if self._cover_orig is None or self._cover_orig.isNull():
            return
        s = self.cover.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        scaled = self._cover_orig.scaled(
            s, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.width() != s.width() or scaled.height() != s.height():
            x = (scaled.width() - s.width()) // 2
            y = (scaled.height() - s.height()) // 2
            scaled = scaled.copy(x, y, s.width(), s.height())
        scaled = _round_all_corners(scaled, BODY_RADIUS)
        self.cover.setPixmap(scaled)


# ── The mini player itself ──────────────────────────────────────────────────

class FloatingMiniPlayer(QWidget):
    # Expanded geometry: H = W + EXPANDED_BOTTOM_DELTA, where the delta
    # is exactly the bar height — the cover takes the top W×W, the bar
    # takes W×_BAR_HEIGHT below it. Same bar height as compact's full
    # window height, so toggling only changes where the cover lives.
    EXPANDED_BOTTOM_DELTA = _BAR_HEIGHT
    # Min width is set so the popup buttons (anchored bottom-right of the
    # bar) clear the rightmost transport button at the narrowest size.
    # Math at min: bar inner = W − 24 (padding); transport = 100; right
    # stretch = (inner − 100) / 2 must exceed popup width + edge margin.
    EXPANDED_MIN_WIDTH = 300
    EXPANDED_INITIAL_WIDTH = 320
    COMPACT_SIZE = (384, _BAR_HEIGHT)
    RESIZE_HIT = 20  # px square in bottom-left corner that triggers resize

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self._mode = "compact"  # "compact" or "expanded"
        # Recursion guard for the aspect-ratio enforcement in
        # resizeEvent — calling self.resize() inside resizeEvent
        # re-enters resizeEvent, which would loop without this.
        self._aspect_adjust = False
        # Last expanded body width — restored when the user toggles back
        # into expanded mode so manual resizes survive a compact trip.
        self._last_expanded_width = self.EXPANDED_INITIAL_WIDTH
        self.setMouseTracking(True)

        # Distinct window title so KWin window rules can scope-match
        # the mini player without catching the main window. Frameless
        # so the user never sees it. Keep in sync with
        # modules.kwin_rules.MINI_PLAYER_WINDOW_TITLE.
        from modules.kwin_rules import MINI_PLAYER_WINDOW_TITLE
        self.setWindowTitle(MINI_PLAYER_WINDOW_TITLE)

        # Frameless top-level window, always on top. Pager/taskbar-skip
        # strategy is platform-split:
        #  - X11: set _NET_WM_STATE_SKIP_TASKBAR/PAGER via xprop in
        #    showEvent (skip_taskbar_x11). Plain Qt.Tool here on X11 +
        #    KDE leaves a ghost strip in some themes.
        #  - Wayland: no xprop equivalent. Qt.Tool is the standard way
        #    to ask the compositor to keep this surface out of the
        #    taskbar; KWin Wayland honors it cleanly.
        # Note: WindowStaysOnTopHint is a no-op on Wayland — xdg-shell
        # forbids apps from controlling z-order. The "Keep mini player
        # on top (Wayland)" setting installs a KWin window rule via
        # modules.kwin_rules to achieve the same effect compositor-side.
        flags = (
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        app = QApplication.instance()
        if app is not None and app.platformName() == "wayland":
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Container is a transparent layout host. The body shape is painted in
        # this widget's paintEvent so we can honor an alpha channel — Qt's
        # QSS alpha doesn't reliably reach a child QFrame.
        self.container = QFrame(self)
        self.container.setObjectName("miniContainer")
        self.container.setStyleSheet("""
            QFrame#miniContainer { background: transparent; border: none; }
            QFrame#miniContainer > QWidget,
            QFrame#miniContainer QStackedWidget,
            QFrame#miniContainer QStackedWidget > QWidget {
                background: transparent;
            }
        """)

        # No drop shadow — the body fills the entire window. Translucent body
        # alone reads better over both dark and bright wallpapers.
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 0, 0, 0)
        self.outer_layout.addWidget(self.container)

        self.inner_layout = QVBoxLayout(self.container)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner_layout.setSpacing(0)

        # Stacked: compact / expanded
        self.stack = QStackedWidget()
        self.compact = _CompactBar()
        self.expanded = _ExpandedPanel()
        self.stack.addWidget(self.compact)
        self.stack.addWidget(self.expanded)
        self.inner_layout.addWidget(self.stack)

        # Floating window-controls overlay — anchored to the bottom-right of
        # the body and only visible while the cursor is over the mini player.
        self.window_controls = QFrame(self.container)
        self.window_controls.setObjectName("winControls")
        self.window_controls.setStyleSheet("""
            QFrame#winControls { background: transparent; }
        """)
        wc_layout = QHBoxLayout(self.window_controls)
        wc_layout.setContentsMargins(0, 0, 0, 0)
        wc_layout.setSpacing(2)

        self.toggle_btn = QPushButton("▢")
        self.toggle_btn.setFixedSize(20, 20)
        self.toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.toggle_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; font-size: 11px; }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        self.toggle_btn.setToolTip("Toggle compact / expanded")
        self.toggle_btn.clicked.connect(self.toggle_mode)

        self.open_btn = QPushButton("⛶")
        self.open_btn.setFixedSize(20, 20)
        self.open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.open_btn.setStyleSheet(self.toggle_btn.styleSheet())
        self.open_btn.setToolTip("Open main window")
        self.open_btn.clicked.connect(lambda: self.bus.open_main_window.emit())

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; font-size: 11px; }}
            QPushButton:hover {{ color: #ef4444; }}
        """)
        self.close_btn.clicked.connect(self.hide)

        wc_layout.addWidget(self.toggle_btn)
        wc_layout.addWidget(self.open_btn)
        wc_layout.addWidget(self.close_btn)
        self.window_controls.adjustSize()
        self.window_controls.hide()

        self._apply_mode_size()
        self._connect_signals()

        # Initial position: bottom-right of the primary screen. Works on
        # X11; on Wayland the protocol forbids client-set absolute
        # positions and KWin will park the window wherever it likes —
        # the user can drag it from there. (The drag/resize handlers
        # below use windowHandle().startSystemMove/Resize, which the
        # compositor honors on both platforms.)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Hard-clear the alpha buffer first. WA_TranslucentBackground sets
        # WA_NoSystemBackground, so Qt won't auto-fill — without this clear,
        # stale frames or compositor artifacts can ghost above the body.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        rect = self.container.geometry()

        # Body — translucent rounded rect; alpha lets the wallpaper show through.
        body_path = QPainterPath()
        body_path.addRoundedRect(
            float(rect.x()), float(rect.y()),
            float(rect.width()), float(rect.height()),
            BODY_RADIUS, BODY_RADIUS,
        )
        p.setBrush(QColor(*MINI_BODY_COLOR))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(body_path)

        # Resize hit area is invisible — bottom-left corner of the body
        # accepts a drag to resize, no glyph needed.

    def showEvent(self, event):
        super().showEvent(event)
        # KWin needs a real X11 winId before it honors EWMH state atoms.
        QTimer.singleShot(0, lambda: skip_taskbar_x11(self))
        # Single delayed call — Qt has the final winId by 50ms. No-op on
        # native Wayland.
        QTimer.singleShot(50, lambda: enable_kde_blur(self))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == "expanded":
            # Re-enforce the aspect ratio after the compositor resizes
            # us. With startSystemResize the WM controls geometry and
            # gives us whatever width/height the user dragged to;
            # without this the cover image would either crop or float
            # in empty space when the height drifted from W + DELTA.
            expected_h = self.width() + self.EXPANDED_BOTTOM_DELTA
            if self.height() != expected_h and not self._aspect_adjust:
                self._aspect_adjust = True
                try:
                    self.resize(self.width(), expected_h)
                finally:
                    self._aspect_adjust = False
            body_w = max(1, self.width())
            self.expanded.cover.setFixedSize(body_w, body_w)
            self.expanded.refresh_cover()
            # Remember the last user-set width so we restore it on the
            # next compact→expanded toggle.
            self._last_expanded_width = body_w
        else:
            # Compact: cover is square at the body's full height (no bezel).
            body_h = max(1, self.height())
            self.compact.thumb.setFixedSize(body_h, body_h)
            self.compact.refresh_cover()
        self._position_window_controls()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.window_controls.show()
        self.window_controls.raise_()
        self._position_window_controls()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.window_controls.hide()

    def _is_resize_corner(self, pos: QPoint) -> bool:
        if self._mode != "expanded":
            return False
        return (pos.x() <= self.RESIZE_HIT and
                pos.y() >= self.height() - self.RESIZE_HIT)

    def _position_window_controls(self):
        # Bottom-right of the bar in both modes. Since toggle_mode pins
        # the window's bottom-right to a fixed screen pivot, anchoring
        # the popup here means it stays at the *exact same screen point*
        # across the compact/expanded swap — only the cover transforms
        # around it. EXPANDED_MIN_WIDTH guarantees there's enough right
        # stretch beside the centered 3-button transport so the popup
        # never collides with the next button.
        self.window_controls.adjustSize()
        cw = self.window_controls.width()
        ch = self.window_controls.height()
        x = self.container.width() - cw - 10
        y = self.container.height() - ch - 10
        self.window_controls.move(x, y)

    # ── Mode switching ──────────────────────────────────────────────────────

    def toggle_mode(self):
        # Anchor the bar's bottom-right (= the window's bottom-right in
        # both modes — bar fills the right side in compact, the bottom
        # in expanded) so the bar appears stationary across the toggle.
        # The window grows up (compact → expanded) or shrinks down
        # (expanded → compact) around this pivot, instead of the whole
        # player jumping screen positions.
        pivot = self.geometry().bottomRight()
        if self._mode == "expanded":
            self._last_expanded_width = self.width()
        self._mode = "expanded" if self._mode == "compact" else "compact"
        self._apply_mode_size()
        new_geom = self.geometry()
        new_geom.moveBottomRight(pivot)
        self.move(new_geom.topLeft())
        self._position_window_controls()

    def _apply_mode_size(self):
        if self._mode == "compact":
            # Fixed size — bar fills the entire window.
            self.setMinimumSize(0, 0)
            self.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
            self.setFixedSize(*self.COMPACT_SIZE)
            self.stack.setCurrentIndex(0)
            self.toggle_btn.setText("▢")
        else:
            # Aspect locked: H = W + bar height.
            self.setMinimumSize(
                self.EXPANDED_MIN_WIDTH,
                self.EXPANDED_MIN_WIDTH + self.EXPANDED_BOTTOM_DELTA,
            )
            self.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
            w = max(self._last_expanded_width, self.EXPANDED_MIN_WIDTH)
            self.resize(w, w + self.EXPANDED_BOTTOM_DELTA)
            self.stack.setCurrentIndex(1)
            self.toggle_btn.setText("▭")
        self._position_window_controls()

    # ── Bus signals ─────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(self._on_paused)
        self.bus.playback_resumed.connect(self._on_resumed)
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        for panel in (self.compact, self.expanded):
            panel.title.setText(np.title)
            panel.artist.setText(np.subtitle or np.year)
            panel.album.setText(np.album)
            panel.play_btn.setIcon(icon("pause"))

        if np.thumb_url:
            # Single load with one cache key, dispatched to both panels.
            # The previous code fired two parallel downloads of the same
            # URL under different cache keys (|mini and |miniexp); if one
            # raced/failed (intermittent on rapid track changes) the
            # affected panel would render an empty placeholder.
            load_image_async(
                f"{np.item_id}|mini", np.thumb_url, 800, 800,
                self._set_cover_both_panels, rounded_radius=0,
            )

    def _set_cover_both_panels(self, pix: QPixmap):
        self.compact.set_cover_pixmap(pix)
        self.expanded.set_cover_pixmap(pix)

    @Slot()
    def _on_stopped(self):
        for panel in (self.compact, self.expanded):
            panel.title.setText("Nothing playing")
            panel.artist.setText("")
            panel.album.setText("")
            panel.play_btn.setIcon(icon("play"))
            panel.progress.setValue(0)

    @Slot()
    def _on_paused(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setIcon(icon("play"))

    @Slot()
    def _on_resumed(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setIcon(icon("pause"))

    @Slot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        for panel in (self.compact, self.expanded):
            if np.duration > 0 and not panel.progress.isSliderDown():
                panel.progress.setValue(int(ms / np.duration * 1000))

    @Slot(int)
    def _on_duration(self, ms: int):
        # No time labels in either compact or expanded — progress bar only.
        pass

    # ── Drag / resize support ───────────────────────────────────────────────
    #
    # Both drag and corner-resize are delegated to the window manager via
    # QWindow.startSystemMove() / startSystemResize(). Works identically
    # on X11 and Wayland — and on Wayland it's the only way that works,
    # since QWidget.move() / setGeometry() on top-level windows is a
    # protocol no-op. The aspect ratio (H = W + EXPANDED_BOTTOM_DELTA in
    # expanded mode) is re-enforced in resizeEvent because the compositor
    # resizes freeform.

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        handle = self.windowHandle()
        if handle is None:
            return
        if self._is_resize_corner(e.position().toPoint()):
            # Bottom-left grip — top-right corner stays anchored when the
            # left and bottom edges follow the cursor.
            handle.startSystemResize(Qt.Edge.LeftEdge | Qt.Edge.BottomEdge)
        else:
            handle.startSystemMove()
