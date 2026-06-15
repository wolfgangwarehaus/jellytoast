"""
Bottom Now Playing bar + Cast device picker dialog.
"""

import logging

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal, Slot

logger = logging.getLogger(__name__)
from PySide6.QtGui import (
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.icon_button import IconButton
from jellytoast.icons import accent_icon, icon


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


from jellytoast.async_io import run_async
from jellytoast.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_SUBHEAD,
    TYPE_TINY,
    type_qss,
)
from jellytoast.player_state import NowPlaying, PlayerBus, get_now_playing
from jellytoast.providers import get_provider
from jellytoast.ui_helpers import (
    ACCENT,
    IDLE_TEXT,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    CoverOverlayButton,
    MarqueeLabel,
    ScrubbableSlider,
    fmt_time,
    ink_alpha,
    load_image_async,
    opaque_menu,
    screen_dpr,
)
from jellytoast.volume_button import VolumeButton

# Fixed worst-case-DPR source size for the bar cover (logical 108 × 3).
# Server fetches use this constant so the L2 raw cache stays one entry
# per album across DPR drift. Paint-time scaling stays DPR-aware via
# refresh_cover. See docs/research/dpr_cache_keys.md.
_BAR_SOURCE_PX = 324


class NowPlayingBar(QWidget):
    """Persistent transport at the bottom of the main window."""

    show_now_playing_requested = Signal()
    show_mini_requested = Signal()
    cast_requested = Signal()
    cast_context_requested = Signal(QPoint)  # right-click on the cast button

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_provider()
        self._is_seeking = False
        # Coalesce the elapsed-time setText to one call per visible
        # second — position emits at ~10 Hz from mpv, but the label
        # only changes once a second. -1 forces a write on the first
        # tick.
        self._last_displayed_sec = -1
        # Cast session state — when set, the streaming-info line shows
        # "Casting to <device>" instead of the local codec/bitrate.
        self._casting = False
        self._casting_device = ""
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

        slider_style = self._slider_qss()

        icon_btn_style = self._icon_btn_qss()

        def _icon_btn(name, tooltip, size=36, icon_size=18):
            b = IconButton()
            b.setIcon(icon(name))
            # Stash the glyph name so _reapply_theme can re-issue it in
            # the new tint on a live theme switch.
            b.setProperty("_jt_icon", name)
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
        self.fav_btn = CoverOverlayButton(self.thumb, size=26, margin=6, bordered=False)
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
        self.title.setStyleSheet(f"color: {IDLE_TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;")
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
        self.mini_btn = _icon_btn("miniplayer", "Open mini player")
        self.mini_btn.clicked.connect(lambda: self.show_mini_requested.emit())

        self.cast_btn = _icon_btn("cast", "Cast")
        self.cast_btn.clicked.connect(lambda: self.cast_requested.emit())
        # Right-click → quick menu of hearted devices + Disconnect,
        # handled by the main window (it owns the cast logic).
        self.cast_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cast_btn.customContextMenuRequested.connect(
            lambda pos: self.cast_context_requested.emit(self.cast_btn.mapToGlobal(pos))
        )

        # Sleep timer — opens a duration menu on click; the icon goes
        # accent-tinted while a timer is armed and the tooltip carries
        # the live countdown. Backed by PlayerBackend's session-scoped
        # timer via the sleep_timer_* bus signals.
        self.sleep_btn = _icon_btn("moon", "Sleep timer")
        self.sleep_btn.clicked.connect(self._open_sleep_menu)
        self._sleep_deadline: float | None = None
        self._sleep_total: int = 0
        self._sleep_tick = QTimer(self)
        self._sleep_tick.setInterval(1000)
        self._sleep_tick.timeout.connect(self._refresh_sleep_tooltip)

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

        # Optional streaming-info line — "Streaming · Bit Perfect · FLAC ·
        # 1411 kbps" etc. Hidden by default; toggled by Settings → Playback.
        # Sits ABOVE the transport row so it reads as a subtle quality
        # readout rather than competing with the controls.
        #
        # Parented to the bar directly (NOT added to the center column's
        # VBox) so the line can extend beyond the center column's width
        # when codec + bitrate get verbose. The line is positioned
        # manually in ``_position_streaming_info``; mouse events pass
        # through it so it doesn't intercept clicks on the transport row.
        self.streaming_info = QLabel("", self)
        self.streaming_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.streaming_info.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} letter-spacing: 0.4px;"
        )
        self.streaming_info.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.streaming_info.setVisible(False)

        trans_row = QHBoxLayout()
        trans_row.setSpacing(8)
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_row.addStretch()
        for btn in (self.shuffle_btn, self.prev_btn, self.play_btn, self.next_btn, self.repeat_btn):
            trans_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        trans_row.addStretch()
        # Keyboard nav: Tab enters this section on play_btn (the anchor);
        # Left/Right then walks the whole bottom bar, left to right —
        # transport (shuffle…repeat) and the right cluster (mini / sleep /
        # cast / volume). The right-cluster buttons are built earlier in
        # __init__ (just laid out further down), so they're safe to wire here.
        from jellytoast.keyboard_focus import install_arrow_nav

        self._bottom_nav = install_arrow_nav(
            [
                self.shuffle_btn,
                self.prev_btn,
                self.play_btn,
                self.next_btn,
                self.repeat_btn,
                self.mini_btn,
                self.sleep_btn,
                self.cast_btn,
                self.vol_btn,
            ]
        )

        # Time labels — Qt QSS doesn't support font-variant-numeric so
        # we accept slight digit-shift as time advances (tiny at 11px).
        # min-width tuned to fit "h:mm:ss" comfortably without burning
        # extra pixels that the seek bar wants for readability.
        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;")
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
        self.tot_time.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;")
        self.tot_time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Internet-radio "LIVE" pip — shown in place of the seek bar +
        # tot_time when the active queue is INTERNET_RADIO. Composite
        # widget: a painted dot (so it visually centers against the
        # uppercase LIVE caps — the Unicode ● glyph sits on the text
        # baseline, which reads noticeably low next to all-caps text)
        # plus the LIVE / station text. The dot is shown only while
        # the station is actually streaming; pause / cold-restore
        # states drop the dot and just carry the station text. Both
        # children's colour tracks via _style_live_pip().
        self.live_pip = QWidget()
        _live_lp = QHBoxLayout(self.live_pip)
        _live_lp.setContentsMargins(0, 0, 0, 0)
        _live_lp.setSpacing(8)
        self._live_dot = QLabel(self.live_pip)
        self._live_dot.setFixedSize(8, 8)
        self._live_text = QLabel("LIVE", self.live_pip)
        _live_lp.addWidget(self._live_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        _live_lp.addWidget(self._live_text, 0, Qt.AlignmentFlag.AlignVCenter)
        _live_lp.addStretch(1)
        self._style_live_pip(ACCENT)
        self.live_pip.hide()

        prog_row = QHBoxLayout()
        # No horizontal contentsMargins — the seek bar should fill the
        # full width of the center column so the progress indicator
        # reads as a meaningful surface rather than a thin sliver.
        prog_row.setContentsMargins(0, 0, 0, 0)
        prog_row.setSpacing(8)
        prog_row.addWidget(self.cur_time)
        prog_row.addWidget(self.seek_bar, 1)
        prog_row.addWidget(self.live_pip, 1)
        prog_row.addWidget(self.tot_time)

        # streaming_info intentionally NOT added — it's a floating child
        # of self, positioned in _position_streaming_info so it can extend
        # past the center column's width.
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
        right_row.addWidget(self.mini_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.sleep_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.cast_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.vol_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.right_cluster = right
        layout.addWidget(right)

        # Initial volume from settings — VolumeButton owns the popup
        # slider, so we push the persisted value through its API and
        # let the bus syncing handle subsequent changes.
        from jellytoast.settings import get_settings

        self.vol_btn.set_initial_volume(get_settings().volume)

        # ── Connect bus ─────────────────────────────────────────────────────
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        # Unified radio rendering — jellytoast.radio_state owns the
        # parse + cover-lookup pipeline and emits ``radio_state_changed``
        # whenever any user-visible field changes. We translate the
        # RadioState into widget updates here. Seed from the current
        # snapshot in case the bar constructs mid-session.
        self.bus.radio_state_changed.connect(self._on_radio_state)
        self._is_radio = False
        self._radio_station_name: str = ""
        from jellytoast import radio_state as _radio_state

        seed = _radio_state.current()
        if seed is not None:
            self._on_radio_state(seed)
        # Settings → "Refresh album art" — re-fetch the current track's
        # cover so a server-side art update lands on the bar without
        # needing a track change. Replaying _on_started against the
        # current NowPlaying re-runs the cover URL build + load.
        self.bus.image_cache_cleared.connect(self._on_image_cache_cleared)
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
        # Reflect repeat/shuffle changes from ANY source (queue restore, MPRIS)
        # on the buttons — they previously only updated on a local click, so an
        # MPRIS-originated change moved the queue but left the buttons stale.
        self.bus.repeat_changed.connect(self._on_repeat_changed)
        self.bus.shuffle_changed.connect(self._on_shuffle_changed)
        # Seed both toggles from the PERSISTED state. QueueManager loads
        # the same settings at construction, so without this the queue
        # shuffles/repeats while the buttons render off after a restart
        # (albums silently played scrambled with shuffle visually off —
        # live find 2026-06-12). The _on_* sync slots are no-emit, so
        # seeding can't bounce back and re-permute the restored queue.
        from jellytoast.settings import get_settings

        _s = get_settings()
        if _s.shuffle:
            self._on_shuffle_changed(True)
        if (_s.repeat_mode or "off") != "off":
            self._on_repeat_changed(_s.repeat_mode)
        # vol_btn / mute icon syncing is handled inside VolumeButton.
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        # Live-accent: re-stamp the shuffle / repeat / favorite icons
        # from current state whenever the user picks a new accent in
        # Settings → Display. The icons are cached QIcon objects that
        # baked the OLD accent at construction; only re-calling
        # `accent_icon()` produces icons with the new colour.
        self.bus.theme_changed.connect(self._reapply_theme)
        # MpvController emits this when audio-bitrate stabilizes a
        # few decode-ticks into a new track. Source of truth for the
        # actual streaming codec + bitrate (raw item metadata is
        # often missing the Bitrate field, and is wrong when the
        # server is transcoding anyway).
        self.bus.streaming_info_updated.connect(
            self._on_streaming_info_updated,
        )
        # Cached last codec / kbps so the streaming-info line can be
        # re-rendered when the bit-perfect runtime state flips without
        # waiting for the next track's mpv codec report (the user might
        # toggle the master setting mid-track, or audio_quality might
        # change).
        self._last_streaming_codec: str = ""
        self._last_streaming_kbps: int = 0
        self.bus.bit_perfect_active_changed.connect(
            lambda _on: self._on_streaming_info_updated(
                self._last_streaming_codec, self._last_streaming_kbps
            )
        )
        # EQ / ReplayGain / Crossfade enable changes flip which DSP
        # segments appear in the badge. Re-render against the cached
        # codec / bitrate so the line tracks the toggle without waiting
        # for the next mpv bitrate report (~2s into the next track).
        def _rerender_streaming_info(*_args):
            self._on_streaming_info_updated(
                self._last_streaming_codec, self._last_streaming_kbps
            )

        self.bus.eq_changed.connect(_rerender_streaming_info)
        self.bus.replaygain_changed.connect(_rerender_streaming_info)
        self.bus.crossfade_changed.connect(_rerender_streaming_info)
        # Per-segment visibility toggles in Settings → Display fire this
        # signal so the badge updates live as the user ticks them.
        self.bus.streaming_info_badges_changed.connect(_rerender_streaming_info)
        # While casting, the info line shows "Casting to <device>"
        # instead of the local codec/bitrate (mpv is idle — there's no
        # local stream to describe). cast_started carries the name.
        self.bus.cast_started.connect(self._on_cast_started)
        self.bus.cast_stopped.connect(self._on_cast_stopped)
        # Streaming-info row is always-on now — kept visible from
        # construction so the codec/bitrate readout shows as soon as
        # MpvController stabilises. Cast handlers hide/restore it.
        self.streaming_info.setVisible(True)
        # Cross-DPR cover refresh — re-issue the cover load at the new
        # physical target when the user drags the window to a
        # different-scale monitor. `_on_started` is idempotent for the
        # metadata/icon setters (same values), so this is safe to
        # call repeatedly.
        self.bus.dpr_changed.connect(self._on_dpr_changed)
        # Sleep-timer state — the bar reflects what PlayerBackend owns.
        # `started` carries the initial seconds; `cancelled` / `fired`
        # both clear the armed look (a fired timer has done its job).
        self.bus.sleep_timer_started.connect(self._on_sleep_started)
        self.bus.sleep_timer_cancelled.connect(self._on_sleep_cleared)
        self.bus.sleep_timer_fired.connect(self._on_sleep_cleared)

    # ── Sleep timer ─────────────────────────────────────────────────────────

    # Preset durations offered in the menu, in minutes. "End of track"
    # is handled separately because it's a mode, not a duration.
    _SLEEP_PRESETS = (15, 30, 45, 60, 90)

    def _open_sleep_menu(self):
        """Pop the sleep-timer duration menu under the moon button.
        Built fresh each open so the active-timer state (the Cancel
        row + its live countdown) is always current."""
        menu = opaque_menu(self)
        active = self._sleep_deadline is not None
        # All presets use ``end_of_track`` — the timer counts down X
        # minutes, then waits for the current song to finish before
        # actually stopping (no mid-song cut). PlayerBackend adds a
        # fade-out over the song's last few seconds automatically
        # when bit-perfect is off; when bit-perfect is on the song
        # plays out at full volume (the lock forbids the fade). The
        # tooltip below describes which path the user will hit.
        bp_active = self.bus.bit_perfect_active
        preset_tip = (
            "Finishes the current song after the timer fires (no fade)."
            if bp_active
            else "Finishes the current song after the timer fires, "
                 "fading out over its last seconds."
        )

        for minutes in self._SLEEP_PRESETS:
            label = f"{minutes} minutes" if minutes < 60 else (
                "1 hour" if minutes == 60 else f"{minutes // 60} h {minutes % 60} min"
                if minutes % 60 else f"{minutes // 60} hours"
            )
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(active and self._sleep_total == minutes * 60)
            act.setToolTip(preset_tip)
            act.triggered.connect(
                lambda _=False, m=minutes:
                    self.bus.sleep_timer_requested.emit(m * 60, "end_of_track")
            )

        menu.addSeparator()
        eot = menu.addAction("Stop after current track")
        eot.triggered.connect(
            lambda: self.bus.sleep_timer_requested.emit(0, "end_of_track")
        )

        if active:
            menu.addSeparator()
            remaining = self._sleep_remaining()
            cancel = menu.addAction(f"Cancel timer  ({fmt_time(remaining * 1000)} left)")
            cancel.triggered.connect(
                lambda: self.bus.sleep_timer_cancel_requested.emit()
            )

        menu.exec(self.sleep_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    def _sleep_remaining(self) -> int:
        """Whole seconds left on the armed timer, or 0 if none."""
        if self._sleep_deadline is None:
            return 0
        import time

        return max(0, int(round(self._sleep_deadline - time.monotonic())))

    @Slot(int)
    def _on_sleep_started(self, seconds: int):
        import time

        self._sleep_total = int(seconds)
        self.sleep_btn.setIcon(accent_icon("moon"))
        # `_sleep_deadline` is non-None whenever a timer is armed — the
        # menu reads it to decide whether to show the Cancel row. A
        # 0-second timer is the "stop after current track" mode: armed,
        # but with no countdown to tick.
        self._sleep_deadline = time.monotonic() + max(0, seconds)
        if seconds > 0:
            self._sleep_tick.start()
            self._refresh_sleep_tooltip()
        else:
            self._sleep_tick.stop()
            self.sleep_btn.setToolTip("Sleep timer — stops after this track")

    @Slot()
    def _on_sleep_cleared(self):
        self._sleep_deadline = None
        self._sleep_total = 0
        self._sleep_tick.stop()
        self.sleep_btn.setIcon(icon("moon"))
        self.sleep_btn.setToolTip("Sleep timer")

    @Slot()
    def _refresh_sleep_tooltip(self):
        remaining = self._sleep_remaining()
        if remaining <= 0:
            self._sleep_tick.stop()
            return
        text = f"Sleep timer — {fmt_time(remaining * 1000)} left"
        self.sleep_btn.setToolTip(text)
        # A tooltip that's already on-screen doesn't re-read the text set via
        # setToolTip() — it stays frozen until the next hover. While the button
        # is hovered, push the new text into our custom popup each tick so the
        # countdown updates live. refresh_text re-uses the visible popup (no
        # re-blur / flicker), or shows it if this is the first tick.
        if self.sleep_btn.underMouse():
            from jellytoast.custom_tooltip import ToolTipPopup

            ToolTipPopup.instance().refresh_text(self.sleep_btn, text)

    def _on_dpr_changed(self):
        np = get_now_playing()
        if np.item_id:
            self._on_started(np)

    def _slider_qss(self) -> str:
        """Seek-bar QSS — ink-on-dim track. Bakes ink_alpha() + TEXT,
        so rebuilt on a live theme switch (see `_reapply_theme`)."""
        return f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {ink_alpha(0.16)};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ink_alpha(0.85)};
                border-radius: 2px;
            }}
            QSlider::add-page:horizontal {{
                background: {ink_alpha(0.10)};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -4px 0;
                background: {TEXT}; border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {TEXT};
            }}
        """

    def _icon_btn_qss(self) -> str:
        """Transport icon-button QSS — the background pill. Reads the
        WASH_* / ACCENT tokens FRESH from the module (not the stale
        top-level imports, which refresh_theme reassigns) so a live
        accent/theme switch actually re-tints the focus ring + hover."""
        from jellytoast import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent; border: 1px solid transparent; border-radius: 8px;
            }}
            QPushButton:hover {{ background: {_u.WASH_HOVER}; }}
            QPushButton:pressed {{ background: {_u.WASH_PRESSED}; }}
            QPushButton:focus {{
                background: {_u.WASH_HOVER}; border-color: {_u.ACCENT}; outline: none;
            }}
        """

    def _reapply_theme(self):
        """Full theme re-stamp on PlayerBus.theme_changed — every icon
        tint, text colour, button + slider QSS, so a live light↔dark
        switch lands uniformly on the bar (accent-only picks route
        here too; the extra work is cheap)."""
        np = get_now_playing()

        # 1. Accent-state icons — favorite / shuffle / repeat / sleep.
        self.fav_btn.setIcon(
            accent_icon("favorite_filled") if np.is_favorite else icon("favorite_outline")
        )
        on = self.shuffle_btn.isChecked()
        self.shuffle_btn.setIcon(accent_icon("shuffle") if on else icon("shuffle"))
        if self._repeat_state == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif self._repeat_state == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))
        self.sleep_btn.setIcon(
            accent_icon("moon") if self._sleep_deadline is not None else icon("moon")
        )
        # Cast button is accent-tinted while a cast is live (see
        # _on_cast_started); re-issue it here so a theme switch mid-cast
        # keeps the active tint instead of reverting to the plain glyph.
        self.cast_btn.setIcon(accent_icon("cast") if self._casting else icon("cast"))

        # 2. Stable-glyph buttons — re-issue in the fresh tint. Every
        #    _icon_btn() carries a `_jt_icon` tag; shuffle / repeat /
        #    sleep are accent-state (handled above) so skip their tags.
        _accent_state = {
            self.shuffle_btn, self.repeat_btn, self.sleep_btn, self.cast_btn,
        }
        for b in self.findChildren(QPushButton):
            name = b.property("_jt_icon")
            if not name or b in _accent_state:
                continue
            b.setIcon(icon(name))
        # Play / pause glyph reflects playback state.
        self.play_btn.setIcon(
            icon("pause") if (np.item_id and not np.is_paused) else icon("play")
        )

        # 3. Button + seek-bar QSS rebuilt from the fresh tokens.
        btn_qss = self._icon_btn_qss()
        for b in (self.mini_btn, self.cast_btn, self.sleep_btn, self.shuffle_btn,
                  self.prev_btn, self.play_btn, self.next_btn, self.repeat_btn):
            b.setStyleSheet(btn_qss)
        self.seek_bar.setStyleSheet(self._slider_qss())

        # 4. Text colours — title / sub / album via _apply_text_layout
        #    (force=True so sub + album re-stamp even with no mode
        #    change), plus the standalone time / streaming labels.
        self._apply_text_layout(self.width(), force=True)
        self.streaming_info.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} letter-spacing: 0.4px;"
        )
        for lbl in (self.cur_time, self.tot_time):
            lbl.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;"
            )
        self._style_live_pip(ACCENT)

    def _style_live_pip(self, color: str):
        """Paint the LIVE dot + text in the given colour. The dot is a
        QLabel styled as a circle (background + border-radius) — the
        text label can't share the parent's QSS without inheriting the
        background fill, so each child carries its own sheet."""
        self._live_dot.setStyleSheet(
            f"QLabel {{ background: {color}; border-radius: 4px; }}"
        )
        self._live_text.setStyleSheet(
            f"color: {color}; {type_qss(TYPE_TINY)} font-weight: 700;"
            " letter-spacing: 1px;"
        )

    def _set_live_pip(self, dot_visible: bool, text: str):
        self._live_dot.setVisible(dot_visible)
        self._live_text.setText(text)

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        # Hold raw metadata so the responsive text layout can rebuild
        # the title / artist / album rows on resize without needing
        # another playback_started event. _apply_text_layout picks the
        # row count + font sizes for the current bar width.
        #
        # Radio owns title/artist via _on_radio_state (the ICY metadata);
        # a REPLAYED _on_started (dpr change / cache clear while a station
        # plays) must NOT clobber them with np's stream fields (empty or
        # wrong for a live stream). Mirror the mini-player + NP-page
        # radio guards. (The cover load below is already radio-guarded.)
        if not self._is_radio:
            self._track_title = np.title
            self._track_subtitle = np.subtitle
            self._track_album = np.album
            self._track_year = np.year
        # New track → force the next position tick to write the elapsed label
        # (the per-second diff guard would otherwise skip the first update).
        self._last_displayed_sec = -1
        self._apply_text_layout(self.width())
        # State-aware: _on_started can be REPLAYED for the same track on a
        # cache-clear / dpr-change while playback is paused — an
        # unconditional "pause" glyph would then lie about the state. Mirror
        # the _reapply_theme logic and show "play" when paused.
        self.play_btn.setIcon(
            icon("pause") if (np.item_id and not np.is_paused) else icon("play")
        )
        self._set_favorite(np.is_favorite)
        # Clear the streaming-info label until mpv reports the actual
        # codec + bitrate for THIS track. Without this, a track
        # change would briefly carry over the previous track's info
        # (and on app restart the restored np would surface a codec
        # without a bitrate, which read as broken). While casting,
        # keep the "Casting to …" line — mpv never reports a codec for
        # a track playing on the cast device, so there's nothing to
        # wait for and clearing it would just blank the indicator.
        if self._casting:
            self.streaming_info.setText(f"Casting to {self._casting_device}")
        else:
            self.streaming_info.setText("")
        self._position_streaming_info()

        image_id = np.image_id or np.item_id
        if image_id and not self._is_radio:
            # Build our OWN URL at the bar's own target size rather
            # than reusing np.thumb_url (which is sized at 600 for cast
            # / MPRIS / TV consumers). Navidrome resizes on every
            # request and caches the original full-resolution file —
            # NOT the variant — so asking for size=600 when the bar
            # is 108px makes Navidrome do ~5× the WebP/JPEG encode work
            # for an image we'd downscale away anyway. See
            # feedback_now_playing_cover_pipeline.
            #
            # Server fetch at the fixed worst-case-DPR source size
            # (324 = 108 × 3) so the L2 raw cache stays one entry per
            # album across launches — Wayland fractional-DPR drift
            # would otherwise pin a fresh raw per session. See
            # docs/research/dpr_cache_keys.md.
            target_px = max(256, int(round(108 * screen_dpr(self))))
            url = self.api.get_image_url(image_id, "Primary", _BAR_SOURCE_PX)
            load_image_async(
                f"{image_id}|npbar",
                url,
                target_px,
                target_px,
                self.set_cover_pixmap,
                rounded_radius=0,
                on_error=lambda: None,
                priority="high",
            )

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
        # the exact L1 cache slot the live cover load will hit. Server
        # fetch at the fixed source size (see _on_started).
        target_px = max(256, int(round(108 * screen_dpr(self))))
        url = self.api.get_image_url(image_id, "Primary", _BAR_SOURCE_PX)
        if not url:
            return
        load_image_async(
            f"{image_id}|npbar",
            url,
            target_px,
            target_px,
            lambda _pix: None,
            rounded_radius=0,
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
            phys_w,
            phys_h,
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
        # bl seats into the window body's rounded bottom-left corner, so
        # it tracks the host-OS window radius (RADIUS_WINDOW); the other
        # three corners use the standard card radius (10 logical).
        # Multiply radii by dpr so they read at the same logical
        # curvature after setDevicePixelRatio retags the pixmap.
        r10 = int(round(10 * dpr))
        r_body = int(round(RADIUS_WINDOW * dpr))
        scaled = _round_corners(scaled, tl=r10, tr=r10, br=r10, bl=r_body)
        scaled.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(scaled)

    @Slot(object)
    def _on_radio_state(self, state):
        """Unified radio renderer — invoked whenever ``jellytoast.radio_state``
        emits a fresh snapshot. ``state is None`` means we left radio
        mode; otherwise the dataclass carries everything the bar needs
        to repaint in one call.

        Render contract (shared with the mini player + NP page):
          • title slot ← ``state.display_title`` (song, or station as
            fallback before ICY arrives)
          • subtitle slot ← ``state.display_subtitle`` (artist, empty
            when ICY hasn't split)
          • LIVE pip ← painted dot + ``LIVE · {station}`` so the user always knows
            which station is streaming, even after a track title
            replaces the placeholder
          • cover ← ``state.display_cover_url`` (per-track MB art when
            available, station logo otherwise)
        """
        if state is None:
            # Leaving radio mode — restore the scrubber chrome. The
            # next playback_started for a normal album will repopulate
            # title / subtitle / cover via the regular _on_started
            # path; nothing for us to clear here that won't be
            # overwritten naturally.
            if self._is_radio:
                self._is_radio = False
                self._radio_station_name = ""
                self.live_pip.hide()
                self.seek_bar.show()
                self.tot_time.show()
            return

        # Entering or updating radio mode.
        first_entry = not self._is_radio
        self._is_radio = True
        self._radio_station_name = state.station_name
        if first_entry:
            self.seek_bar.hide()
            self.tot_time.hide()
            self.live_pip.show()

        # LIVE pip — gated on actual playback. The dot + "LIVE" only
        # paint while audio is streaming; pause downgrades to a dim
        # "PAUSED · station" so the radio context stays visible but
        # the badge doesn't lie about live state; stopped (cold
        # restore / inactive queue) just carries the station name.
        station = (state.station_name or "").strip()
        if state.is_live:
            text = f"LIVE  ·  {station}" if station else "LIVE"
            self._style_live_pip(ACCENT)
            self._set_live_pip(True, text)
        elif state.playback_state == "paused":
            text = f"PAUSED  ·  {station}" if station else "PAUSED"
            self._style_live_pip(TEXT_FAINT)
            self._set_live_pip(False, text)
        else:
            text = station
            self._style_live_pip(TEXT_FAINT)
            self._set_live_pip(False, text)

        # Title + subtitle rows. The bar's responsive text layout reads
        # _track_* fields; we set them and re-apply.
        self._track_title = state.display_title
        self._track_subtitle = state.display_subtitle
        self._track_album = ""
        self._track_year = ""
        self._apply_text_layout(self.width())

        # Cover — single source of truth, no need to coordinate logo
        # vs. art_url priority here (display_cover_url handles it).
        cover_url = state.display_cover_url
        if cover_url:
            self._load_radio_cover(cover_url)

    def _load_radio_cover(self, url: str) -> None:
        """Fetch ``url`` and stamp it as the bar's cover. Uses the
        same DPR-aware pipeline as the normal _on_started cover load
        so MusicBrainz art / station logos read at the same fidelity
        as album art."""
        if not url:
            return
        target_px = max(256, int(round(108 * screen_dpr(self))))
        load_image_async(
            f"radio:{url}",
            url,
            target_px,
            target_px,
            self.set_cover_pixmap,
            rounded_radius=0,
            on_error=lambda: None,
            priority="high",
        )

    @Slot()
    def _on_stopped(self):
        self._last_displayed_sec = -1
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
        # Keep the "Casting to …" line if a cast is still live (a stop
        # mid-cast shouldn't blank the only sign the audio's elsewhere).
        if not self._casting:
            self.streaming_info.setText("")
            self._position_streaming_info()

    def _on_cast_started(self, device_name: str):
        """A cast session began — the info line becomes the cast
        indicator, shown regardless of the streaming-info setting
        (where the audio is going matters more than a bitrate)."""
        self._casting = True
        self._casting_device = device_name or "device"
        self.streaming_info.setText(f"Casting to {self._casting_device}")
        self.streaming_info.setVisible(True)
        self._position_streaming_info()
        # Reflect the active cast on the cast button itself (accent tint +
        # tooltip) so it reads as "on" like shuffle/repeat do — without this
        # the button stays its inactive glyph through the whole cast session.
        self.cast_btn.setIcon(accent_icon("cast"))
        self.cast_btn.setToolTip(f"Casting to {self._casting_device}")

    def _on_cast_stopped(self):
        """Cast ended — drop the indicator and repaint the local
        codec/bitrate badge immediately from cache (instead of going
        blank until the next mpv codec report). _casting is cleared
        first so _on_streaming_info_updated's casting short-circuit
        doesn't swallow the repaint."""
        self._casting = False
        self._casting_device = ""
        self.streaming_info.setVisible(True)
        self._on_streaming_info_updated(
            self._last_streaming_codec, self._last_streaming_kbps
        )
        self.cast_btn.setIcon(icon("cast"))
        self.cast_btn.setToolTip("Cast")

    def _on_streaming_info_updated(self, codec: str, kbps: int):
        """Fired by MpvController via the bus as soon as the actual
        playback bitrate stabilizes. Reflects what's being decoded
        right now — so a Jellyfin-transcoded MP3 stream from a FLAC
        source reads "MP3 · 192 kbps", which is what the user is
        actually hearing.

        When the current track is a downloaded local blob the line
        leads with "Local playback" instead of "Streaming" — same
        codec + bitrate, but it's clear nothing is hitting the server.

        When bit-perfect mode is active AND the source is being served
        direct (audio_quality == "original") AND the user isn't casting,
        the line is prefixed with "Lossless · " — Roon's signal-path
        indicator pattern, scaled down.

        Ignored entirely while casting: the line is the "Casting to …"
        indicator then, and mpv is idle so any stray report is stale.
        """
        # Cache for bit_perfect_changed re-renders that arrive without
        # a fresh mpv codec report.
        self._last_streaming_codec = codec or ""
        self._last_streaming_kbps = int(kbps or 0)
        if self._casting:
            return
        # Build the segments left → right. Each segment is gated on a
        # per-user Display setting so the line can be tuned from
        # "exhaustive signal-chain readout" to "just the format" —
        # Settings → Display → Now-playing info.
        #
        # Prefix ("Streaming" / "Local playback") always shown — it's the
        # line's anchor. Casting routes elsewhere via the casting
        # short-circuit above. Bit Perfect supersedes the DSP segments
        # in practice because enabling bit-perfect force-disables them.
        from jellytoast.settings import get_settings as _gs

        s = _gs()
        prefix = "Local playback" if get_now_playing().is_local else "Streaming"
        segments = [prefix]
        if self.bus.bit_perfect_active and s.streaming_info_show_bit_perfect:
            segments.append("Bit Perfect")
        elif not self.bus.bit_perfect_active:
            # DSP segments — signal-chain order (RG → EQ → Crossfade).
            # ReplayGain reads as "Normalized" in the badge: matches the
            # Playback page's user-facing name ("Normalization") and
            # describes a state (the audio IS normalized) instead of the
            # underlying spec name (ReplayGain).
            if s.streaming_info_show_replaygain and (s.replaygain or "no") != "no":
                segments.append("Normalized")
            if s.streaming_info_show_eq and s.eq_enabled:
                segments.append("EQ")
            if s.streaming_info_show_crossfade and s.crossfade_enabled:
                segments.append("Crossfade")
        if codec and s.streaming_info_show_codec:
            segments.append(codec.upper())
        if kbps and kbps > 0 and s.streaming_info_show_bitrate:
            segments.append(f"{kbps} kbps")
        # If everything past the prefix is hidden + we have no codec/
        # bitrate, fall back to a blank line so we don't render a
        # stranded "Streaming" with no readout. Matches the previous
        # behaviour of returning early when ``parts`` was empty.
        if len(segments) == 1 and not (codec or (kbps and kbps > 0)):
            self.streaming_info.setText("")
            self._position_streaming_info()
            return
        self.streaming_info.setText("  ·  ".join(segments))
        self._position_streaming_info()

    @Slot()
    def _on_image_cache_cleared(self):
        """Re-trigger the cover load for the currently-playing track
        after the user clicked Settings → Refresh album art. No-op
        when nothing is playing."""
        np = get_now_playing()
        if np is None or not (np.image_id or np.item_id):
            return
        self._on_started(np)

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
        # Position fires at mpv's observer cadence (~10 Hz) but the
        # elapsed-time label only changes once per second. Skip the
        # setText (and its relayout) when the visible second hasn't
        # ticked over.
        sec = ms // 1000
        if sec != self._last_displayed_sec:
            self._last_displayed_sec = sec
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

    @Slot(str)
    def _on_repeat_changed(self, mode: str):
        # External repeat change (queue restore / MPRIS LoopStatus). Sync the
        # button without re-emitting — repeat_btn uses `clicked`, so setChecked
        # here can't re-fire _cycle_repeat. The early-return makes our own
        # _cycle_repeat's emit a harmless no-op when it bounces back.
        if mode == self._repeat_state:
            return
        self._repeat_state = mode
        if mode == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif mode == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))
        self.repeat_btn.setChecked(mode != "off")

    @Slot(bool)
    def _on_shuffle_changed(self, on: bool):
        # External shuffle change (MPRIS Shuffle). shuffle_btn uses `toggled`,
        # so block signals around setChecked to avoid re-emitting shuffle_changed.
        if on == self.shuffle_btn.isChecked():
            return
        self.shuffle_btn.blockSignals(True)
        self.shuffle_btn.setChecked(on)
        self.shuffle_btn.blockSignals(False)
        self.shuffle_btn.setIcon(accent_icon("shuffle") if on else icon("shuffle"))

    def set_cast_manager(self, cm):
        """Forward the CastManager to the volume button so its popup can
        switch to the per-speaker variant when casting to a group."""
        self.vol_btn.set_cast_manager(cm)

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
        (940, 340, 12, 44),
        (840, 310, 10, 40),
        (760, 280, 8, 36),
        (680, 240, 8, 32),
        (560, 170, 6, 24),
        (0, 140, 4, 20),
    )
    _TEXT_SPLIT_WIDTH = 1080  # below this, switch from 2-row to 3-row text
    _TEXT_HIDE_WIDTH = 680  # below this, hide all text rows

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

    def _apply_text_layout(self, bar_w: int, force: bool = False):
        """Pick the row count + font sizes for the current bar width
        and re-render title / artist / album from the stored track
        metadata. Idempotent — safe to call on every resize tick.

        ``force`` re-stamps the sub / album label styles even when the
        layout mode hasn't changed — used by `_reapply_theme` so a
        live theme switch refreshes their colours (the per-mode style
        block is otherwise skipped on a same-width call)."""
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
            f"color: {TEXT if not is_idle else IDLE_TEXT}; "
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
        if force or mode != self._text_mode:
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
                    f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600; letter-spacing: 0.1px;"
                )
                self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")
                self.album_line.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")

    def _position_streaming_info(self):
        """Place the floating streaming-info label horizontally centered
        on the bar, just below the top edge, sized to its text. The
        label lives outside the layout system so it can extend beyond
        the center column's width — at narrow bar widths the verbose
        "Streaming · Bit Perfect · FLAC · 1411 kbps" variant would
        otherwise clip. Safe to call repeatedly (idempotent in the
        steady state)."""
        info = self.streaming_info
        info.adjustSize()
        bar_w = self.width()
        w = info.sizeHint().width()
        h = info.sizeHint().height()
        # Top inset chosen to sit roughly where the line read before
        # this widget left the center VBox — visually still "above the
        # transport row", but free to overflow horizontally.
        info.setGeometry((bar_w - w) // 2, 4, w, h)
        info.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_responsive_layout(self.width())
        self._position_streaming_info()
