"""
Bottom Now Playing bar + Cast device picker dialog.
"""

from typing import List
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QSize, QPoint
from PySide6.QtGui import QColor, QPixmap, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QDialog, QListWidget, QListWidgetItem, QFrame,
)

from modules.icons import icon, accent_icon


def _round_corners(pix: QPixmap, tl: int, tr: int, br: int, bl: int) -> QPixmap:
    """Round individual corners of a pixmap. Each parameter is the radius
    for one corner; pass 0 for square. Used by the now-playing bar so the
    cover's bottom-left corner matches the window's body radius while the
    inside edges read as a card edge."""
    if pix.isNull():
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = pix.width(), pix.height()
    path = QPainterPath()
    path.moveTo(tl, 0)
    path.lineTo(w - tr, 0)
    if tr > 0:
        path.quadTo(w, 0, w, tr)
    else:
        path.lineTo(w, 0)
    path.lineTo(w, h - br)
    if br > 0:
        path.quadTo(w, h, w - br, h)
    else:
        path.lineTo(w, h)
    path.lineTo(bl, h)
    if bl > 0:
        path.quadTo(0, h, 0, h - bl)
    else:
        path.lineTo(0, h)
    path.lineTo(0, tl)
    if tl > 0:
        path.quadTo(0, 0, tl, 0)
    else:
        path.lineTo(0, 0)
    path.closeSubpath()
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out

from modules.player_state import PlayerBus, NowPlaying, get_now_playing
from modules.cast_manager import CastManager, CastDevice
from modules.providers import get_provider
from modules.async_io import run_async
from modules.ui_helpers import (
    load_image_async, fmt_time, ACCENT, TEXT, TEXT_DIM,
    TEXT_FAINT, ScrubbableSlider, MarqueeLabel, CoverOverlayButton,
    screen_dpr,
)
from modules.design_tokens import (
    TYPE_SUBHEAD, TYPE_BODY, TYPE_CAPTION, TYPE_TINY, TYPE_MICRO,
    font, type_qss,
)


class _VolumeSliderPopup(QFrame):
    """Floating vertical volume slider that sits above the volume button.

    Built as a child of the host main window (not a top-level Qt.Popup
    or Qt.Tool surface) — keeps positioning portable across X11 and
    Wayland (xdg-shell forbids client-set absolute positions for
    top-levels) and avoids the dismiss-on-click semantics that Qt.Popup
    forces. Hover lifecycle is owned by ``VolumeButton``: the popup
    just emits ``entered`` / ``left`` so the button can run a single
    grace timer covering both surfaces.
    """

    value_changed = Signal(int)
    entered = Signal()
    left = Signal()

    POPUP_W = 40
    POPUP_H = 135

    def __init__(self, parent: QWidget, height: int | None = None):
        super().__init__(parent)
        self.setObjectName("jtVolumePopup")
        self.setFixedSize(self.POPUP_W, height if height is not None else self.POPUP_H)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Solid background — popup is a child of the main window so
        # alpha would composite against whatever's behind, and Qt's
        # QSS border-radius clipping is reliable only with opaque fills.
        self.setStyleSheet("""
            QFrame#jtVolumePopup {
                background: rgb(20, 22, 26);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 12px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(0)

        self.slider = ScrubbableSlider(Qt.Orientation.Vertical)
        self.slider.setRange(0, 100)
        # Qt's default vertical slider already lays out top=max, so
        # invertedAppearance stays at its default (False). sub-page is
        # the area "below the handle in the direction of min", which on
        # a top=max slider is the BOTTOM half — that's the bit we want
        # to paint bright as the filled gauge.
        self.slider.setStyleSheet("""
            QSlider::groove:vertical {
                width: 4px;
                background: rgba(255,255,255,0.16);
                border-radius: 2px;
            }
            QSlider::sub-page:vertical {
                background: rgba(255,255,255,0.85);
                border-radius: 2px;
            }
            QSlider::add-page:vertical {
                background: rgba(255,255,255,0.12);
                border-radius: 2px;
            }
            QSlider::handle:vertical {
                width: 12px; height: 12px; margin: 0 -4px;
                background: #ffffff; border-radius: 6px;
            }
        """)
        self.slider.valueChanged.connect(self.value_changed.emit)
        layout.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignHCenter)
        self.hide()

    def set_value(self, v: int):
        was_blocked = self.slider.blockSignals(True)
        try:
            self.slider.setValue(v)
        finally:
            self.slider.blockSignals(was_blocked)

    def enterEvent(self, e):
        super().enterEvent(e)
        self.entered.emit()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.left.emit()


class VolumeButton(QPushButton):
    """Volume icon button with hover popup, click-to-mute, and wheel
    scroll. Replaces the old inline ``vol_btn + vol_slider`` pair.

    Tracks volume via PlayerBus (volume_state / mute_state) so the
    popup slider always reflects the current mpv-side volume even
    when changes originate elsewhere (system mixer, MPRIS, etc.).
    """

    WHEEL_STEP = 2

    def __init__(self, bus, parent=None, size: int = 36, popup_height: int | None = None):
        super().__init__(parent)
        self.bus = bus
        self._volume = 80
        self._popup_height = popup_height
        self._popup: _VolumeSliderPopup | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(180)
        self._hide_timer.timeout.connect(self._maybe_hide_popup)

        # Icon scales with the button — 18 of 36 in the bar (50%), so
        # keep that ratio when callers shrink it for the mini player.
        icon_px = max(10, int(round(size * 0.5)))
        radius_px = max(3, int(round(size * 0.22)))
        self.setIcon(icon("volume"))
        self.setIconSize(QSize(icon_px, icon_px))
        self.setFixedSize(size, size)
        self.setToolTip("Mute / unmute · scroll to adjust · hover for slider")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; border-radius: {radius_px}px; }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QPushButton:pressed {{ background: rgba(255, 255, 255, 0.16); }}
        """)
        self.clicked.connect(lambda: self.bus.mute_toggled.emit())

        # Track upstream state. Bar-construction code may push an
        # initial volume right after instantiation; bus events sync the
        # slider afterwards.
        self.bus.volume_state.connect(self._on_volume_state)
        self.bus.mute_state.connect(self._on_mute_state)

    def set_initial_volume(self, v: int):
        self._volume = max(0, min(100, int(v)))
        if self._popup is not None:
            self._popup.set_value(self._volume)

    @Slot(int)
    def _on_volume_state(self, v: int):
        self._volume = v
        if self._popup is not None:
            self._popup.set_value(v)

    @Slot(bool)
    def _on_mute_state(self, m: bool):
        self.setIcon(icon("volume_muted" if m else "volume"))

    # ── Hover lifecycle ────────────────────────────────────────────────
    def enterEvent(self, e):
        super().enterEvent(e)
        self._hide_timer.stop()
        self._show_popup()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._hide_timer.start()

    def _show_popup(self):
        # Resolve the host lazily — at construction time the button
        # isn't parented yet, so self.window() returns the button
        # itself. By first show, the bar is in the window's layout and
        # window() resolves to the top-level main window — which is
        # the only ancestor tall enough to host a 150px popup above
        # the 96px bar.
        host = self.window()
        if self._popup is None or self._popup.parent() is not host:
            if self._popup is not None:
                self._popup.setParent(host)
            else:
                self._popup = _VolumeSliderPopup(host, height=self._popup_height)
                self._popup.set_value(self._volume)
                self._popup.value_changed.connect(self.bus.volume_changed.emit)
                self._popup.entered.connect(self._hide_timer.stop)
                self._popup.left.connect(self._hide_timer.start)
        # Anchor: horizontally centered above the button, with a small
        # gap. Map the button's top-center into host coords so the
        # popup lands in the right place even when the bar's layout
        # has shifted it around horizontally.
        btn_top = self.mapTo(host, QPoint(self.width() // 2, 0))
        popup_x = btn_top.x() - self._popup.width() // 2
        popup_y = btn_top.y() - self._popup.height() - 6
        # Clamp inside the host so the popup is never partially
        # off-screen when the bar is up against a window edge.
        popup_x = max(4, min(popup_x, host.width() - self._popup.width() - 4))
        popup_y = max(4, popup_y)
        self._popup.move(popup_x, popup_y)
        self._popup.show()
        self._popup.raise_()

    def _maybe_hide_popup(self):
        if self._popup is None:
            return
        if self._popup.underMouse() or self.underMouse():
            return
        self._popup.hide()

    def wheelEvent(self, e):
        # Conventional: scroll up → louder, scroll down → quieter.
        delta = e.angleDelta().y()
        if delta == 0:
            return
        if delta > 0:
            new_vol = min(100, self._volume + self.WHEEL_STEP)
        else:
            new_vol = max(0, self._volume - self.WHEEL_STEP)
        if new_vol != self._volume:
            self.bus.volume_changed.emit(new_vol)
        e.accept()


class NowPlayingBar(QWidget):
    """Persistent transport at the bottom of the main window."""

    show_now_playing_requested = Signal()
    show_queue_requested = Signal()
    cast_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_provider()
        self._is_seeking = False
        # ``set_left_cluster_visible(False)`` (called when the
        # now-playing page is showing) hides the title/sub so the
        # full-page cover isn't duplicated by the bar. Our responsive
        # code also toggles title/sub visibility on resize; track the
        # page-suppression state so the two don't fight.
        self._left_suppressed = False
        # Track metadata is held as instance vars so the responsive
        # layout can re-render the same playing track in either
        # "combined" (2-row) or "split" (3-row) mode without needing
        # a fresh playback_started event.
        self._track_title = ""
        self._track_subtitle = ""
        self._track_album = ""
        self._track_year = ""
        self._text_mode: str | None = None  # combined / split / hide

        self.setFixedHeight(108)
        self.setObjectName("npbar")
        # Transparent — the host window paints its translucent body
        # underneath, so the bar inherits that frosted look. The descendant
        # rule clears child container backgrounds (QLabels, plain QWidget
        # holders) that would otherwise paint opaque from GLOBAL_STYLE.
        # QPushButtons/QSliders have their own per-widget stylesheets that
        # take precedence and remain styled.
        self.setStyleSheet("""
            QWidget#npbar { background: transparent; }
            QWidget#npbar QWidget { background: transparent; }
            QWidget#npbar QLabel { background: transparent; }
        """)

        # White-on-dim slider — overrides the global ACCENT-colored
        # QSlider rule. Used for both seek and volume bars. Groove
        # bumped to 4px so the seek bar reads as a substantial
        # progress indicator rather than a hair-thin track.
        slider_style = """
            QSlider::groove:horizontal {
                height: 4px;
                background: rgba(255,255,255,0.16);
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: rgba(255,255,255,0.85);
                border-radius: 2px;
            }
            QSlider::add-page:horizontal {
                background: rgba(255,255,255,0.10);
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 12px; height: 12px; margin: -4px 0;
                background: #ffffff; border-radius: 6px;
            }
            QSlider::handle:horizontal:hover {
                background: #ffffff;
            }
        """

        # Shared style for transport icon buttons. The icon itself
        # handles dim→bright on hover (via the icon registry's two-state
        # QIcon); this stylesheet only paints the button background pill.
        icon_btn_style = """
            QPushButton {
                background: transparent; border: none; border-radius: 8px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.10); }
            QPushButton:pressed { background: rgba(255, 255, 255, 0.16); }
        """

        def _icon_btn(name, tooltip, size=36, icon_size=18):
            b = QPushButton()
            b.setIcon(icon(name))
            b.setIconSize(QSize(icon_size, icon_size))
            b.setFixedSize(size, size)
            b.setToolTip(tooltip)
            b.setStyleSheet(icon_btn_style)
            return b

        layout = QHBoxLayout(self)
        # Left margin = 0 so the cover sits flush in the bottom-left
        # corner of the window. Right margin = 0 too so the heart and
        # mini-player flanks land symmetric around the bar's true
        # geometric center; the volume-slider's edge breathing room is
        # provided by an internal margin on the right cluster instead.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # ── Left cluster: thumbnail + title/artist + utility icons ──────────
        # Click target for "expand the now-playing detail page". The
        # cover hosts a hover-revealed heart overlay (CoverOverlayButton)
        # so the favorite control no longer eats horizontal space in the
        # bar layout. Mini-player / cast / volume icons live to the
        # right of the title text — moved over from the old right
        # cluster so a snapped window doesn't clip them off-screen. A
        # right-side spacer (built later) mirrors this cluster's width
        # so the seek bar's centerline stays aligned with the play
        # button above it.
        left = QWidget()
        left.setFixedWidth(380)
        left_layout = QHBoxLayout(left)
        # Small left padding on the *info* side via spacing so the title
        # text doesn't visually butt up against the cover's right edge —
        # at narrow widths the marquee head was reading like it was
        # painted onto the album art.
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(18)

        # Thumb is a QLabel parented inside its own QFrame so the heart
        # overlay can attach as a positioned child. The QLabel paints
        # the artwork; CoverOverlayButton sits on top, anchored to
        # bottom-right, only visible while the cover is hovered.
        self.thumb = QLabel()
        self.thumb.setFixedSize(108, 108)
        self.thumb.setStyleSheet("background: transparent;")
        self._cover_orig: QPixmap | None = None
        self.fav_btn = CoverOverlayButton(self.thumb, size=26, margin=6)
        self.fav_btn.setIcon(icon("favorite_outline"))
        self.fav_btn.setIconSize(QSize(14, 14))
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.clicked.connect(self._toggle_favorite)

        # Title above artist, tight (2px gap), vertically centered against
        # the cover art. Wrapping in another QVBoxLayout with stretches
        # above/below would also work; AlignVCenter on the QLabels +
        # AddStretch guarantees the same look without extra widgets.
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.addStretch(1)
        # MarqueeLabel scrolls when the text exceeds its width — covers
        # the squeeze case where a snapped window narrows the left
        # cluster enough that "Artist · Album (Anniversary edition…)"
        # would otherwise get cut off mid-word. Stays static when the
        # text fits.
        self.title = MarqueeLabel("Nothing Playing")
        # Idle title color matches the inactive icon color
        # (icons.ICON_DIM = #a8a8a8) so "Nothing Playing" reads at
        # the same visual weight as the transport buttons next to
        # it. _apply_text_mode flips back to TEXT on an active track.
        self.title.setStyleSheet(
            f"color: #a8a8a8; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
        )
        self.sub = MarqueeLabel("")
        self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        # Third row, used only in narrow ("split") mode where each of
        # title / artist / album lives on its own line so none of them
        # have to marquee. Hidden at wide widths where artist+album
        # share the sub line.
        self.album_line = MarqueeLabel("")
        self.album_line.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self.album_line.setVisible(False)
        info.addWidget(self.title)
        info.addWidget(self.sub)
        info.addWidget(self.album_line)
        info.addStretch(1)

        left_layout.addWidget(self.thumb)
        left_layout.addLayout(info, 1)

        # Mini-player / cast / volume buttons are built here so the
        # NowPlayingBar exposes them as instance attributes; they're
        # added to the right cluster further down.
        self.queue_btn = _icon_btn("miniplayer", "Open mini player")
        self.queue_btn.setCheckable(True)
        self.queue_btn.clicked.connect(lambda: self.show_queue_requested.emit())

        self.cast_btn = _icon_btn("cast", "Cast")
        self.cast_btn.clicked.connect(lambda: self.cast_requested.emit())

        # VolumeButton owns its popup and tracks volume_state /
        # mute_state on the bus. The popup's host (main window) is
        # resolved lazily on first show via self.window().
        self.vol_btn = VolumeButton(self.bus)

        # Click-to-open is scoped to the cover thumb only — moving it
        # off the whole-cluster handler means clicks on the title /
        # subtitle / utility icons no longer trip an unwanted
        # show_now_playing_requested. The bottom-left corner exclusion
        # is still needed so a press right on the window's resize hit
        # zone bubbles to the host instead of opening the page.
        _CORNER_RESIZE_BOX = 16

        def _on_thumb_press(e):
            if e.button() != Qt.MouseButton.LeftButton:
                e.ignore()
                return
            x = e.position().x()
            y = e.position().y()
            if x <= _CORNER_RESIZE_BOX and y >= self.thumb.height() - _CORNER_RESIZE_BOX:
                e.ignore()
                return
            self.show_now_playing_requested.emit()
        self.thumb.mousePressEvent = _on_thumb_press
        # Exposed so the host can blank the cover/title while the
        # now-playing page is showing. The cluster's responsive width
        # (set in _apply_responsive_layout) stays reserved regardless
        # of child visibility — keeps the seek bar centered.
        self.left_cluster = left
        layout.addWidget(left)

        # ── Center column: transport above progress, both centered ──────────
        # Stretches above and below the two rows make the cluster sit
        # vertically in the bar (not glued to the top). Spacing between
        # the rows is tight (6px) so they read as one control surface.
        center = QVBoxLayout()
        # Small horizontal padding so the title text has breathing room
        # before the shuffle button on the left, and the seek-bar tail
        # doesn't run flush into the right cluster's mini-player icon.
        center.setContentsMargins(12, 6, 12, 6)
        center.setSpacing(6)
        center.addStretch(1)

        self.shuffle_btn = _icon_btn("shuffle", "Shuffle")
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.toggled.connect(self._on_shuffle_toggled)

        self.prev_btn = _icon_btn("prev", "Previous (Ctrl+Left)")
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())

        # Play is the primary control — slightly larger than the others
        # so the eye lands on it first.
        self.play_btn = _icon_btn("play", "Play / Pause (Space)", size=44, icon_size=22)
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())

        self.next_btn = _icon_btn("next", "Next (Ctrl+Right)")
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())

        self.repeat_btn = _icon_btn("repeat", "Repeat")
        self.repeat_btn.setCheckable(True)
        self._repeat_state = "off"
        self.repeat_btn.clicked.connect(self._cycle_repeat)

        # Optional streaming-info line — "Streaming FLAC · 1411 kbps"
        # etc. Hidden by default; toggled by Settings → Playback. Sits
        # ABOVE the transport row so it reads as a subtle quality
        # readout rather than competing with the controls.
        self.streaming_info = QLabel("")
        self.streaming_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.streaming_info.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} "
            "letter-spacing: 0.4px;"
        )
        self.streaming_info.setVisible(False)

        trans_row = QHBoxLayout()
        trans_row.setSpacing(8)
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_row.addStretch()
        for btn in (self.shuffle_btn, self.prev_btn, self.play_btn,
                    self.next_btn, self.repeat_btn):
            trans_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        trans_row.addStretch()

        # Time labels — Qt QSS doesn't support font-variant-numeric so
        # we accept slight digit-shift as time advances (tiny at 11px).
        # min-width tuned to fit "h:mm:ss" comfortably without burning
        # extra pixels that the seek bar wants for readability.
        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;"
        )
        self.cur_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # ScrubbableSlider gives click-to-jump in addition to drag-to-
        # scrub; sliderPressed/Released still fire so the existing
        # _is_seeking gate keeps working.
        self.seek_bar = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setRange(0, 1000)
        self.seek_bar.setStyleSheet(slider_style)
        self.seek_bar.sliderPressed.connect(lambda: setattr(self, "_is_seeking", True))
        self.seek_bar.sliderReleased.connect(self._on_seek_release)

        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;"
        )
        self.tot_time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        prog_row = QHBoxLayout()
        # No horizontal contentsMargins — the seek bar should fill the
        # full width of the center column so the progress indicator
        # reads as a meaningful surface rather than a thin sliver.
        prog_row.setContentsMargins(0, 0, 0, 0)
        prog_row.setSpacing(8)
        prog_row.addWidget(self.cur_time)
        prog_row.addWidget(self.seek_bar, 1)
        prog_row.addWidget(self.tot_time)

        center.addWidget(self.streaming_info)
        center.addLayout(trans_row)
        center.addLayout(prog_row)
        center.addStretch(1)
        layout.addLayout(center, 1)

        # ── Right cluster: utility icons (mini / cast / volume) ─────────────
        # Right-aligned inside a fixed-width slot that mirrors the
        # left cluster's width — keeps the seek bar's centerline
        # directly under the play button above it. The internal
        # leading stretch + addWidget order pushes the three buttons
        # against the bar's right edge with a small inner margin so
        # the volume popup has breathing room to anchor above the
        # right-most icon.
        right = QWidget()
        right.setFixedWidth(380)
        right_row = QHBoxLayout(right)
        # Right margin keeps the volume icon away from the window's
        # right edge — at narrow widths the icon used to sit nearly
        # flush against the window border.
        # Generous right inset (48 px) so the volume icon stays clear
        # of the window's right edge even on narrow / VNC-clipped
        # displays where the rightmost pixels can sit off-screen. The
        # right cluster's leading stretch absorbs the extra space, so
        # the seek bar's centering is unaffected.
        right_row.setContentsMargins(0, 0, 48, 0)
        right_row.setSpacing(8)
        right_row.addStretch(1)
        right_row.addWidget(self.queue_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.cast_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.vol_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.right_cluster = right
        layout.addWidget(right)

        # Initial volume from settings — VolumeButton owns the popup
        # slider, so we push the persisted value through its API and
        # let the bus syncing handle subsequent changes.
        from modules.settings import get_settings
        self.vol_btn.set_initial_volume(get_settings().volume)

        # ── Connect bus ─────────────────────────────────────────────────────
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        # Cover-art prefetch: queue_manager fires this with the
        # next-up NowPlaying every time the queue advances (and on
        # shuffle reorders). We warm our own cache slot so the next
        # track-change is a memory-cache hit instead of a fresh
        # network round-trip — same idea as mpv's audio prefetch.
        self.bus.queue_prefetch_request.connect(self._prefetch_cover)
        self.bus.playback_paused.connect(lambda: self.play_btn.setIcon(icon("play")))
        self.bus.playback_resumed.connect(lambda: self.play_btn.setIcon(icon("pause")))
        self.bus.playback_restored.connect(self._on_restored)
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)
        # vol_btn / mute icon syncing is handled inside VolumeButton.
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        # Live-accent: re-stamp the shuffle / repeat / favorite icons
        # from current state whenever the user picks a new accent in
        # Settings → Display. The icons are cached QIcon objects that
        # baked the OLD accent at construction; only re-calling
        # `accent_icon()` produces icons with the new colour.
        self.bus.theme_changed.connect(self._reapply_accent)
        # Streaming-info live toggle. Settings → Playback emits this
        # so the user doesn't have to restart to flip the indicator
        # on/off. _on_streaming_info_visibility handles both flips.
        self.bus.streaming_info_changed.connect(
            self._on_streaming_info_visibility,
        )
        # MpvController emits this when audio-bitrate stabilizes a
        # few decode-ticks into a new track. Source of truth for the
        # actual streaming codec + bitrate (raw item metadata is
        # often missing the Bitrate field, and is wrong when the
        # server is transcoding anyway).
        self.bus.streaming_info_updated.connect(
            self._on_streaming_info_updated,
        )
        # Seed initial visibility from the persisted setting.
        try:
            self.streaming_info.setVisible(get_settings().show_streaming_info)
        except Exception:
            pass
        # Cross-DPR cover refresh — re-issue the cover load at the new
        # physical target when the user drags the window to a
        # different-scale monitor. `_on_started` is idempotent for the
        # metadata/icon setters (same values), so this is safe to
        # call repeatedly.
        self.bus.dpr_changed.connect(self._on_dpr_changed)

    def _on_dpr_changed(self):
        np = get_now_playing()
        if np.item_id:
            self._on_started(np)

    def _reapply_accent(self):
        """Rebuild every accent-state icon from current state. Called
        on PlayerBus.theme_changed so a fresh accent pick flows through
        even when shuffle / repeat / favorite haven't been toggled
        since."""
        np = get_now_playing()
        # Favorite — same logic as `_set_favorite`.
        self.fav_btn.setIcon(
            accent_icon("favorite_filled") if np.is_favorite
            else icon("favorite_outline")
        )
        # Shuffle — re-evaluate from the button's checked state since
        # we don't keep a separate flag here (the toggled signal
        # already kept the button in sync with the queue state).
        on = self.shuffle_btn.isChecked()
        self.shuffle_btn.setIcon(
            accent_icon("shuffle") if on else icon("shuffle")
        )
        # Repeat — three-state, tracked in self._repeat_state.
        if self._repeat_state == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif self._repeat_state == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        # Hold raw metadata so the responsive text layout can rebuild
        # the title / artist / album rows on resize without needing
        # another playback_started event. _apply_text_layout picks the
        # row count + font sizes for the current bar width.
        self._track_title = np.title
        self._track_subtitle = np.subtitle
        self._track_album = np.album
        self._track_year = np.year
        self._apply_text_layout(self.width())
        self.play_btn.setIcon(icon("pause"))
        self._set_favorite(np.is_favorite)
        # Clear the streaming-info label until mpv reports the actual
        # codec + bitrate for THIS track. Without this, a track
        # change would briefly carry over the previous track's info
        # (and on app restart the restored np would surface a codec
        # without a bitrate, which read as broken).
        self.streaming_info.setText("")

        image_id = np.image_id or np.item_id
        if image_id:
            # Build our OWN URL at the bar's own target size rather
            # than reusing np.thumb_url (which is sized at 600 for cast
            # / MPRIS / TV consumers). Navidrome resizes on every
            # request and caches the original full-resolution file —
            # NOT the variant — so asking for size=600 when the bar
            # is 108px makes Navidrome do ~5× the WebP/JPEG encode work
            # for an image we'd downscale away anyway. See
            # feedback_now_playing_cover_pipeline. The 256 floor stays
            # sharp at 1× and 2×; at 3+× the DPR multiplier on the
            # thumb's logical size takes over so 4K Retina users get a
            # crisp source instead of an upscale.
            target_px = max(256, int(round(108 * screen_dpr(self))))
            url = self.api.get_image_url(image_id, "Primary", target_px)
            load_image_async(f"{image_id}|npbar", url, target_px, target_px,
                              self.set_cover_pixmap, rounded_radius=0,
                              on_error=lambda: None,
                              priority="high")

    @Slot(object)
    def _prefetch_cover(self, np):
        """Warm our cover cache slot for the next-up track. Called
        when queue_manager fires queue_prefetch_request — typically
        triggered on every track advance and on queue mutations."""
        if np is None:
            return
        image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
        if not image_id:
            return
        # Same DPR-aware target as _on_started so the prefetch warms
        # the exact cache slot the live cover load will hit.
        target_px = max(256, int(round(108 * screen_dpr(self))))
        url = self.api.get_image_url(image_id, "Primary", target_px)
        if not url:
            return
        load_image_async(
            f"{image_id}|npbar", url, target_px, target_px,
            lambda _pix: None, rounded_radius=0,
            on_error=lambda: None,
        )

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        if self._cover_orig is None or self._cover_orig.isNull():
            return
        s = self.thumb.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        # HiDPI: render the cover at physical pixels (logical × dpr) so
        # the QLabel paints at logical size using a full-resolution
        # texture instead of an upscaled logical-sized pixmap. Without
        # this, on a 2× display the painter would downscale a 108-pixel
        # pixmap to 216 physical pixels at paint time — visibly soft.
        dpr = screen_dpr(self)
        phys_w = max(s.width(), int(round(s.width() * dpr)))
        phys_h = max(s.height(), int(round(s.height() * dpr)))
        scaled = self._cover_orig.scaled(
            phys_w, phys_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # KeepAspectRatioByExpanding may return a pixmap larger than the
        # target on one axis for non-square source art. Center-crop to
        # the exact physical target BEFORE rounding so the corner curves
        # bake at the right edges; otherwise the QLabel's logical clip
        # hides them and the user sees square corners instead.
        if scaled.width() != phys_w or scaled.height() != phys_h:
            cx = max(0, (scaled.width() - phys_w) // 2)
            cy = max(0, (scaled.height() - phys_h) // 2)
            scaled = scaled.copy(cx, cy, phys_w, phys_h)
        # bl=14 logical seats into the window body's rounded bottom-left
        # corner; the other three corners use the standard card radius
        # (10 logical). Multiply radii by dpr so they read at the same
        # logical curvature after setDevicePixelRatio retags the pixmap.
        r10 = int(round(10 * dpr))
        r14 = int(round(14 * dpr))
        scaled = _round_corners(scaled, tl=r10, tr=r10, br=r10, bl=r14)
        scaled.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(scaled)

    @Slot()
    def _on_stopped(self):
        self._track_title = ""
        self._track_subtitle = ""
        self._track_album = ""
        self._track_year = ""
        self._apply_text_layout(self.width())
        self._cover_orig = None
        self.thumb.setPixmap(QPixmap())
        self.play_btn.setIcon(icon("play"))
        self._set_favorite(False)
        self.seek_bar.setValue(0)
        self.cur_time.setText("0:00")
        self.tot_time.setText("0:00")
        self.streaming_info.setText("")

    def _on_streaming_info_visibility(self, visible: bool):
        """Toggle the streaming-info label on user setting change.
        Wired to PlayerBus.streaming_info_changed."""
        self.streaming_info.setVisible(bool(visible))

    def _on_streaming_info_updated(self, codec: str, kbps: int):
        """Fired by MpvController via the bus as soon as the actual
        playback bitrate stabilizes. Reflects what's being decoded
        right now — so a Jellyfin-transcoded MP3 stream from a FLAC
        source reads "MP3 · 192 kbps", which is what the user is
        actually hearing."""
        parts = []
        if codec:
            parts.append(codec.upper())
        if kbps and kbps > 0:
            parts.append(f"{kbps} kbps")
        if not parts:
            self.streaming_info.setText("")
            return
        self.streaming_info.setText("Streaming " + "  ·  ".join(parts))

    @Slot(object)
    def _on_restored(self, np: NowPlaying):
        """Render the launch-time resume state: track + saved position
        + duration, paused. Same UI as _on_started but the play icon
        stays as 'play' (not 'pause') because mpv hasn't loaded yet."""
        self._on_started(np)
        # _on_started flipped the icon to pause — override back to play.
        self.play_btn.setIcon(icon("play"))
        self._on_duration(np.duration)
        self._on_position(np.position)

    @Slot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        if not self._is_seeking and np.duration > 0:
            self.seek_bar.setValue(int(ms / np.duration * 1000))
        self.cur_time.setText(fmt_time(ms))

    @Slot(int)
    def _on_duration(self, ms: int):
        self.tot_time.setText(fmt_time(ms))

    def _on_seek_release(self):
        self._is_seeking = False
        np = get_now_playing()
        if np.duration > 0:
            ms = int(self.seek_bar.value() / 1000 * np.duration)
            self.bus.seek_requested.emit(ms)

    def _cycle_repeat(self):
        order = ["off", "all", "one"]
        idx = order.index(self._repeat_state)
        self._repeat_state = order[(idx + 1) % 3]
        # off=outline, all=accent-tinted repeat, one=accent-tinted repeat-one
        if self._repeat_state == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif self._repeat_state == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))
        self.repeat_btn.setChecked(self._repeat_state != "off")
        self.bus.repeat_changed.emit(self._repeat_state)

    def _on_shuffle_toggled(self, on: bool):
        self.shuffle_btn.setIcon(accent_icon("shuffle") if on else icon("shuffle"))
        self.bus.shuffle_changed.emit(on)

    def set_left_cluster_visible(self, visible: bool):
        """Hide the cover/title/artist while leaving the cluster widget
        in the layout (so the responsive width still reserves space and
        the seek bar stays centered). The mini-player / cast / volume
        utility icons stay visible and clickable — the user still wants
        mute/cast/mini-player one click away even when the now-playing
        page is showing its own copy of the cover and title.

        The cover click-handler is scoped to the thumb itself, so
        hiding the thumb is enough to suppress the show_now_playing
        emit — no setEnabled gymnastics required.

        Sets ``_left_suppressed`` so the responsive resize logic
        doesn't try to re-show the title/sub on its next pass."""
        self._left_suppressed = not visible
        self.thumb.setVisible(visible)
        self.title.setVisible(visible)
        self.sub.setVisible(visible)
        self.album_line.setVisible(False)  # _apply_text_layout will re-enable in split mode
        # Re-run the responsive pass when un-suppressing so the
        # text-hide / split breakpoints (if applicable at the current
        # width) are honoured instead of leaving title/sub un-hidden.
        if visible:
            self._apply_responsive_layout(self.width())

    def _toggle_favorite(self):
        np = get_now_playing()
        if not np.item_id:
            return
        new_state = not np.is_favorite
        run_async(self.api.toggle_favorite, np.item_id, new_state)
        np.is_favorite = new_state
        self.bus.favorite_toggled.emit(np.item_id, new_state)

    def _set_favorite(self, fav: bool):
        # Filled accent-colored heart when favorited; outline otherwise.
        self.fav_btn.setIcon(accent_icon("favorite_filled") if fav else icon("favorite_outline"))

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id == item_id:
            self._set_favorite(fav)

    # ── Responsive layout ───────────────────────────────────────────────────
    # Left cluster carries cover + title; right cluster carries
    # mini-player / cast / volume. Cluster widths track in lockstep so
    # the seek bar's centerline stays under the play button — the
    # load-bearing alignment cue for the bar.
    #
    # Cluster width grows / shrinks with the bar to keep the title
    # text legible; main HBox spacing tightens at narrow widths to
    # buy back pixels for the seek bar. The right-cluster inset grows
    # at narrow widths so the volume/cast/mini-player trio doesn't sit
    # flush against the window border on phone-sized surfaces.
    #
    # Text presentation has three modes driven by bar width:
    #   - combined (bar >= _TEXT_SPLIT_WIDTH): 2 rows — title above
    #     "Artist · Album". The classic wide-window look.
    #   - split    (_TEXT_HIDE_WIDTH ≤ bar < _TEXT_SPLIT_WIDTH): 3 rows
    #     — title, artist, album each on their own line. Fonts step
    #     down a tier so 3 lines feel calm rather than crammed, and
    #     each individual line is short enough to avoid marquee scroll.
    #   - hide     (bar < _TEXT_HIDE_WIDTH): cover only, all text rows
    #     hidden. The cover still opens the now-playing page on click,
    #     so the full title is one tap away.
    _BREAKPOINTS = (
        # (min bar width, cluster width, main spacing, right inset)
        # Cluster widths are biased toward the *title* side at wider
        # ranges: we'd rather shrink the seek bar than crush "Artist ·
        # Album" into illegibility. Below the text-hide threshold the
        # left cluster shrinks aggressively so the seek bar / transport
        # row get the horizontal room they need.
        (1200, 380, 16, 48),
        (1080, 360, 14, 48),
        (940,  340, 12, 44),
        (840,  310, 10, 40),
        (760,  280,  8, 36),
        (680,  240,  8, 32),
        (560,  170,  6, 24),
        (0,    140,  4, 20),
    )
    _TEXT_SPLIT_WIDTH = 1080  # below this, switch from 2-row to 3-row text
    _TEXT_HIDE_WIDTH = 680    # below this, hide all text rows

    def _apply_responsive_layout(self, bar_w: int):
        cluster_w, spacing, right_inset = 380, 16, 48
        for min_w, cw, sp, ri in self._BREAKPOINTS:
            if bar_w >= min_w:
                cluster_w, spacing, right_inset = cw, sp, ri
                break
        if self.left_cluster.width() != cluster_w:
            self.left_cluster.setFixedWidth(cluster_w)
        if self.right_cluster.width() != cluster_w:
            self.right_cluster.setFixedWidth(cluster_w)
        if self.layout().spacing() != spacing:
            self.layout().setSpacing(spacing)
        right_layout = self.right_cluster.layout()
        cur_margins = right_layout.contentsMargins()
        if cur_margins.right() != right_inset:
            right_layout.setContentsMargins(0, 0, right_inset, 0)
        self._apply_text_layout(bar_w)

    def _apply_text_layout(self, bar_w: int):
        """Pick the row count + font sizes for the current bar width
        and re-render title / artist / album from the stored track
        metadata. Idempotent — safe to call on every resize tick."""
        # Host owns visibility while the now-playing page is showing;
        # set_left_cluster_visible will re-trigger this when un-suppressing.
        if self._left_suppressed:
            return

        if bar_w < self._TEXT_HIDE_WIDTH:
            mode = "hide"
        elif bar_w < self._TEXT_SPLIT_WIDTH:
            mode = "split"
        else:
            mode = "combined"

        # Visibility — always update because the host may have flipped
        # things off in suppression and we're un-suppressing now.
        self.title.setVisible(mode != "hide")
        self.sub.setVisible(mode != "hide")
        self.album_line.setVisible(mode == "split")

        if mode == "hide":
            self._text_mode = mode
            return

        # Text content per mode. Title always carries the song name (or
        # the placeholder) so the row is never blank when visible.
        is_idle = not bool(self._track_title)
        self.title.setText(self._track_title or "Nothing Playing")
        # Idle title matches the inactive icon color (#a8a8a8 — see
        # icons.ICON_DIM) so the placeholder visually pairs with the
        # transport buttons next to it instead of competing with real
        # track names for the eye.
        self.title.setStyleSheet(
            f"color: {TEXT if not is_idle else '#a8a8a8'}; "
            f"{type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
        )
        if mode == "combined":
            bits = [b for b in (self._track_subtitle, self._track_album) if b]
            self.sub.setText("  ·  ".join(bits) or self._track_year or "")
        else:  # split
            self.sub.setText(self._track_subtitle or self._track_year or "")
            self.album_line.setText(self._track_album or "")

        # Font sizes — restyle only on mode change. Sub/album use raw
        # font-size in split mode (11px) instead of TYPE_CAPTION (12px)
        # to give the 3-row stack a calmer, more compact rhythm.
        if mode != self._text_mode:
            self._text_mode = mode
            if mode == "combined":
                self.title.setStyleSheet(
                    f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
                )
                self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
            else:  # split — step down one size tier on both rows.
                # Title overrides TYPE_BODY's 400 weight to 600 so the
                # split-mode title still reads as the heading of the stack.
                self.title.setStyleSheet(
                    f"color: {TEXT}; {type_qss(TYPE_BODY)} "
                    "font-weight: 600; letter-spacing: 0.1px;"
                )
                self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")
                self.album_line.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_responsive_layout(self.width())


# ── Cast dialog ──────────────────────────────────────────────────────────────

class CastDialog(QDialog):
    """Frameless frosted dialog matching the settings + main window. Auto-
    scans on open; devices appear live as discovery callbacks fire. The
    Rescan button is kept as a manual escape hatch but the user shouldn't
    need it for the common path."""

    BODY_RADIUS = 14
    # After this long with no devices, the "Scanning…" placeholder
    # flips to "No devices found" so the dialog doesn't sit in a
    # forever-loading state on networks with nothing castable.
    SCAN_GIVEUP_MS = 6000

    # Cross-thread bridge: pychromecast's get_chromecasts() and zeroconf's
    # ServiceBrowser fire their callbacks on plain Python threads with no
    # Qt event loop. Re-emitting through a signal hands off to the GUI
    # thread automatically (Qt::AutoConnection picks queued mode for
    # cross-thread connections), which a bare QTimer.singleShot can't do
    # because the timer would land in the worker thread that has no
    # event loop running.
    _devices_changed = Signal(list)

    def __init__(self, cast_manager: CastManager, parent=None):
        super().__init__(parent)
        self.cast_manager = cast_manager
        self.selected_device: CastDevice | None = None
        self.setWindowTitle("Cast")
        self.setFixedSize(440, 480)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtCastDialog")
        self.setModal(True)

        from modules.ui_helpers import GLOBAL_STYLE, DIALOG_BODY_COLOR
        self._dialog_body_color = DIALOG_BODY_COLOR
        # GLOBAL_STYLE provides QListWidget/QPushButton baselines; we
        # override per-list and per-button below to keep the cast card
        # aesthetic consistent with the settings dialog.
        self.setStyleSheet(GLOBAL_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(20, 6, 20, 16)
        v.setSpacing(10)

        # Active-cast banner — visible only when a cast session is live.
        # Shows "Casting to {name}" + a Disconnect button that kills the
        # session. Hidden otherwise so the dialog reads as a picker.
        self._active_banner = self._build_active_banner()
        self._apply_banner_qss()
        v.addWidget(self._active_banner)

        v.addWidget(self._section_header("Available devices"))

        sub = QLabel(
            "Pick a Chromecast or AirPlay receiver on your network."
        )
        sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # Scanning state — visible while we wait for the first device to
        # come back. Replaced by the device list as soon as one shows up.
        self._scanning_label = QLabel("Scanning your network…")
        self._scanning_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            "background: rgba(255,255,255,0.04);"
            "border-radius: 8px; padding: 14px 16px;"
        )
        self._scanning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._scanning_label)

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSpacing(0)
        # 18 px icons next to each row — large enough to read the
        # cast/airplay glyph distinction at a glance, small enough to
        # keep each list item to a single text-line height.
        self.list.setIconSize(QSize(18, 18))
        self.list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline-style: none;
                padding: 2px;
            }}
            QListWidget::item {{
                color: {TEXT};
                padding: 7px 14px;
                border-radius: 6px;
                margin: 1px 0;
            }}
            QListWidget::item:hover {{
                background: rgba(255,255,255,0.05);
            }}
            QListWidget::item:selected {{
                background: rgba(255,255,255,0.10);
                color: {TEXT};
            }}
        """)
        self.list.hide()  # hidden until first device lands
        v.addWidget(self.list, 1)

        # Bottom action row: Rescan on the left, Cancel + Cast on the
        # right. All three share a consistent transparent-default /
        # grey-box-on-hover language so the dialog reads as a calm
        # bottom strip rather than three differently-weighted controls.
        # Cast is distinguished by accent-colored text (and dims when
        # disabled), not by a different hover treatment.
        action_btn_css = f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QPushButton:pressed {{ background: rgba(255, 255, 255, 0.16); }}
            QPushButton:disabled {{ color: rgba(255, 255, 255, 0.30); }}
        """
        # Cast-button QSS is built from current accent — extracted into
        # _cast_btn_qss() so _reapply_accent can re-stamp it when the
        # user picks a new accent in Settings.
        cast_btn_css = self._cast_btn_qss()

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.scan_btn = QPushButton("Rescan")
        self.scan_btn.setStyleSheet(action_btn_css)
        self.scan_btn.clicked.connect(self.scan)
        btns.addWidget(self.scan_btn)
        # Forget paired device — only enabled when the selected list
        # item is an AirPlay 2 receiver with stored credentials. Clears
        # the credentials so the next cast attempt re-launches the
        # pairing dialog. Lives next to Rescan because both are "fix
        # the list" actions; Cancel / Cast are the dialog's primary
        # decision pair.
        self.forget_btn = QPushButton("Forget")
        self.forget_btn.setStyleSheet(action_btn_css)
        self.forget_btn.setEnabled(False)
        self.forget_btn.setToolTip(
            "Clear stored pairing credentials for the selected "
            "AirPlay 2 device so it can be re-paired."
        )
        self.forget_btn.clicked.connect(self._on_forget_clicked)
        btns.addWidget(self.forget_btn)
        btns.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(action_btn_css)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)

        self.cast_btn = QPushButton("Cast")
        self.cast_btn.setStyleSheet(cast_btn_css)
        self.cast_btn.setEnabled(False)
        self.cast_btn.clicked.connect(self.accept)
        btns.addWidget(self.cast_btn)
        v.addLayout(btns)

        outer.addWidget(body, 1)

        self.list.itemSelectionChanged.connect(self._on_select)
        # Live updates as devices are discovered — saves the user from
        # having to click rescan + wait. The callback fires on the
        # discovery thread; emitting our signal there hands off to the
        # GUI thread (queued connection) before _render_devices runs.
        self._devices_changed.connect(self._render_devices)
        self.cast_manager.set_devices_callback(self._devices_changed.emit)
        # Pull whatever's already in the cache, then start a fresh
        # discovery so the list stays current. Banner reflects current
        # active_cast immediately so the user can disconnect without
        # waiting for the discovery callback.
        # Scan-give-up timer — flips the scanning label to the
        # "No devices found" empty state if nothing has landed by
        # SCAN_GIVEUP_MS. Reset on every scan() / kept off when
        # devices arrive.
        self._scan_giveup_timer = QTimer(self)
        self._scan_giveup_timer.setSingleShot(True)
        self._scan_giveup_timer.setInterval(self.SCAN_GIVEUP_MS)
        self._scan_giveup_timer.timeout.connect(self._on_scan_giveup)

        self._render_devices(self.cast_manager.get_all_devices())
        self._refresh_active_banner()
        self.scan()

        # Live-accent: rebuild the banner stylesheet + restamp the
        # Cast button color when the user picks a new accent. Both
        # bake the accent at construction; without this they'd freeze
        # at whatever was active when the dialog opened.
        PlayerBus.get().theme_changed.connect(self._reapply_accent)

    # ── Title bar ──────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtCastTitle")
        tb.setStyleSheet("""
            QWidget#jtCastTitle { background: transparent; }
            QWidget#jtCastTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        cast_glyph = QLabel()
        cast_glyph.setPixmap(icon("cast").pixmap(QSize(18, 18)))
        h.addWidget(cast_glyph)

        title = QLabel("Cast to device")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        h.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: rgba(239,68,68,0.85); color: white; }}
        """)
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def _section_header(self, text: str) -> QLabel:
        # font(TYPE_MICRO) handles uppercase + letter-spacing via QFont,
        # so we pass mixed-case text here — Qt's QSS doesn't actually
        # honor text-transform/letter-spacing, only QFont does.
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    # ── Device discovery ───────────────────────────────────────────────
    def scan(self):
        # Show the scanning placeholder if nothing is rendered yet — if
        # we already have devices from a previous scan, leave them
        # visible while a fresh discovery runs in the background.
        if self.list.count() == 0:
            self._scanning_label.setText("Scanning your network…")
            self._scanning_label.show()
            self.list.hide()
            self._scan_giveup_timer.start()
        self.cast_manager.discover_all()

    def _render_devices(self, devices: List[CastDevice]):
        # Preserve selection across re-renders so a freshly-arriving
        # device doesn't deselect what the user just clicked.
        prev_uuid = (
            self.selected_device.uuid if self.selected_device else None
        )
        self.list.clear()
        if not devices:
            # Leave the label alone — it's either "Scanning…" (in
            # progress) or "No devices found" (give-up timer fired).
            # Clearing the list still matters because devices may
            # have been REMOVED from the cache.
            return
        # Devices arrived — stop the give-up timer so the empty
        # state doesn't flip in over a now-populated list.
        self._scan_giveup_timer.stop()
        self._scanning_label.hide()
        self.list.show()
        for dev in devices:
            is_chromecast = dev.device_type == "chromecast"
            kind = "Chromecast" if is_chromecast else "AirPlay"
            glyph = icon("cast" if is_chromecast else "airplay")
            # Single-line label keeps each row to one font height instead
            # of two — fits more devices in the same dialog. The glyph
            # prepended to the row gives mixed-network scans an
            # at-a-glance Chromecast / AirPlay distinction.
            item = QListWidgetItem(glyph, f"{dev.name}   ·   {kind}")
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self.list.addItem(item)
            if prev_uuid and dev.uuid == prev_uuid:
                self.list.setCurrentItem(item)
        # Banner state can change as devices come and go (active_cast
        # may have just been discovered with full metadata).
        self._refresh_active_banner()

    @Slot()
    def _on_scan_giveup(self):
        """SCAN_GIVEUP_MS elapsed without any device showing up — flip
        the scanning placeholder to a 'No devices found' empty state
        so the dialog reads as 'done scanning, network empty' instead
        of 'forever loading'."""
        if self.list.count() > 0:
            return
        self._scanning_label.setText(
            "No devices found on your network.\n"
            "Try Rescan, or check that your devices are awake."
        )
        self._scanning_label.show()
        self.list.hide()

    # ── Active-cast banner ─────────────────────────────────────────────
    def _build_active_banner(self) -> QWidget:
        w = QFrame()
        w.setObjectName("castActiveBanner")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 10, 8, 10)
        h.setSpacing(10)

        text_wrap = QVBoxLayout()
        text_wrap.setContentsMargins(0, 0, 0, 0)
        text_wrap.setSpacing(1)
        # Mixed-case text + font(TYPE_MICRO) — QFont applies the uppercase
        # transform and letter-spacing that QSS would silently ignore.
        kicker = QLabel("Casting to")
        kicker.setFont(font(TYPE_MICRO))
        kicker.setStyleSheet(f"color: {TEXT_FAINT};")
        text_wrap.addWidget(kicker)
        self._active_label = QLabel("")
        self._active_label.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)}"
        )
        text_wrap.addWidget(self._active_label)
        h.addLayout(text_wrap, 1)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setObjectName("ghost")
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        h.addWidget(self._disconnect_btn)

        w.hide()
        return w

    def _refresh_active_banner(self):
        active = self.cast_manager.active_cast
        if active is None:
            self._active_banner.hide()
            return
        kind = "Chromecast" if active.device_type == "chromecast" else "AirPlay"
        self._active_label.setText(f"{active.name}   ·   {kind}")
        self._active_banner.show()

    def _apply_banner_qss(self):
        """Apply the active-cast banner stylesheet from the CURRENT
        accent — split out so _reapply_accent can re-stamp it on
        theme_changed without rebuilding the whole banner widget."""
        from modules.theme import get_active_theme as _gt, _hex_to_rgb as _hr
        _ar, _ag, _ab = _hr(_gt().accent)
        self._active_banner.setStyleSheet(f"""
            QFrame#castActiveBanner {{
                background: rgba({_ar},{_ag},{_ab},0.14);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.25);
                border-radius: 8px;
            }}
        """)

    def _cast_btn_qss(self) -> str:
        """QSS for the primary Cast action button — accent-coloured
        text, transparent body. Re-callable so _reapply_accent can
        push a fresh stylesheet when the user picks a new accent."""
        from modules.ui_helpers import ACCENT as _ACCENT
        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {_ACCENT};
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QPushButton:pressed {{ background: rgba(255, 255, 255, 0.16); }}
            QPushButton:disabled {{ color: rgba(255, 255, 255, 0.30); }}
        """

    def _reapply_accent(self):
        """Re-stamp every surface whose stylesheet baked the accent at
        construction. Wired to PlayerBus.theme_changed in __init__."""
        self._apply_banner_qss()
        if hasattr(self, "cast_btn"):
            self.cast_btn.setStyleSheet(self._cast_btn_qss())

    def _on_disconnect(self):
        # stop_cast() handles both branches (chromecast.quit_app() +
        # mc.stop(), or AirPlay POST /stop) and clears active_cast.
        self.cast_manager.stop_cast()
        # Tell the rest of the app the cast session ended so the
        # NowPlayingBar / mini player can drop any cast indicators.
        try:
            from modules.player_state import PlayerBus
            PlayerBus.get().cast_stopped.emit()
        except Exception:
            pass
        self._refresh_active_banner()

    def _on_select(self):
        sel = self.list.selectedItems()
        if not sel:
            self.forget_btn.setEnabled(False)
            return
        dev = sel[0].data(Qt.ItemDataRole.UserRole)
        if not dev:
            self.forget_btn.setEnabled(False)
            return
        self.selected_device = dev
        self.cast_btn.setEnabled(True)
        # Enable Forget only for AirPlay 2 receivers that have stored
        # credentials. Chromecasts and AirPlay 1 devices don't pair, so
        # Forget would be a no-op for them.
        forget_eligible = False
        try:
            from modules import airplay2 as _ap2
            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                if _ap2.get_stored_credentials(ap2_dev.identifier):
                    forget_eligible = True
        except Exception:
            pass
        self.forget_btn.setEnabled(forget_eligible)

    def _on_forget_clicked(self):
        dev = self.selected_device
        if dev is None:
            return
        try:
            from modules import airplay2 as _ap2
            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                _ap2.forget_credentials(ap2_dev.identifier)
                # Reflect immediately — the button should grey out
                # since the credentials we were storing are gone.
                self.forget_btn.setEnabled(False)
        except Exception as e:
            print(f"[CastDialog] forget_credentials failed: {e}")

    def paintEvent(self, e):
        # Rounded card body, matching the settings dialog. The custom
        # titlebar is part of the same surface, so the rounded rect
        # spans the full window.
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            path = QPainterPath()
            path.addRoundedRect(
                0.0, 0.0, float(self.width()), float(self.height()),
                self.BODY_RADIUS, self.BODY_RADIUS,
            )
            p.setBrush(QColor(*self._dialog_body_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
