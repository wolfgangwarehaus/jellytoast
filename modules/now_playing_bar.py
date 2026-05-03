"""
Bottom Now Playing bar + Cast device picker dialog.
"""

from typing import List
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QSize
from PySide6.QtGui import QColor, QPixmap, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QSlider,
    QDialog, QListWidget, QListWidgetItem, QFrame, QSizePolicy,
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
from modules.jellyfin_api import get_api
from modules.async_io import run_async
from modules.ui_helpers import (
    load_image_async, fmt_time, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM,
    TEXT_FAINT, BORDER, BG_PANEL,
)
from modules.design_tokens import (
    TYPE_SUBHEAD, TYPE_BODY, TYPE_CAPTION, TYPE_MICRO, font, type_qss,
)


class NowPlayingBar(QWidget):
    """Persistent transport at the bottom of the main window."""

    show_now_playing_requested = Signal()
    show_queue_requested = Signal()
    cast_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_api()
        self._is_seeking = False

        self.setFixedHeight(96)
        self.setObjectName("npbar")
        # Transparent — the host window paints its translucent body
        # underneath, so the bar inherits that frosted look. The descendant
        # rule clears child container backgrounds (QLabels, plain QWidget
        # holders) that would otherwise paint opaque from GLOBAL_STYLE.
        # QPushButtons/QSliders have their own per-widget stylesheets that
        # take precedence and remain styled.
        self.setStyleSheet(f"""
            QWidget#npbar {{ background: transparent; }}
            QWidget#npbar QWidget {{ background: transparent; }}
            QWidget#npbar QLabel {{ background: transparent; }}
        """)

        # White-on-dim slider — overrides the global ACCENT-colored
        # QSlider rule. Used for both seek and volume bars.
        slider_style = """
            QSlider::groove:horizontal {
                height: 3px;
                background: rgba(255,255,255,0.16);
                border-radius: 1px;
            }
            QSlider::sub-page:horizontal {
                background: rgba(255,255,255,0.85);
                border-radius: 1px;
            }
            QSlider::add-page:horizontal {
                background: rgba(255,255,255,0.10);
                border-radius: 1px;
            }
            QSlider::handle:horizontal {
                width: 11px; height: 11px; margin: -4px 0;
                background: #ffffff; border-radius: 5px;
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

        def _icon_btn(name, tooltip, size=32, icon_size=16):
            b = QPushButton()
            b.setIcon(icon(name))
            b.setIconSize(QSize(icon_size, icon_size))
            b.setFixedSize(size, size)
            b.setToolTip(tooltip)
            b.setStyleSheet(icon_btn_style)
            return b

        layout = QHBoxLayout(self)
        # Left margin = 0 so the cover sits flush in the bottom-left
        # corner of the window. Right margin gives the volume slider
        # some breathing room before the window edge.
        layout.setContentsMargins(0, 0, 20, 0)
        layout.setSpacing(16)

        # ── Left column: thumbnail + title/artist + heart ───────────────────
        # Whole column is the click target for "expand the now-playing
        # detail page" — that's why mousePressEvent is wired on it.
        left = QWidget()
        left.setFixedWidth(380)
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)

        self.thumb = QLabel()
        self.thumb.setFixedSize(96, 96)
        self.thumb.setStyleSheet("background: transparent;")
        self._cover_orig: QPixmap | None = None

        # Title above artist, tight (2px gap), vertically centered against
        # the cover art. Wrapping in another QVBoxLayout with stretches
        # above/below would also work; AlignVCenter on the QLabels +
        # AddStretch guarantees the same look without extra widgets.
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.addStretch(1)
        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
        )
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        info.addWidget(self.title)
        info.addWidget(self.sub)
        info.addStretch(1)

        # Favorite — sits at the END of the title/artist row, vertically
        # centered against the text. Same icon-button styling as the
        # transport cluster so the whole bar reads as one button family.
        self.fav_btn = QPushButton()
        self.fav_btn.setIcon(icon("favorite_outline"))
        self.fav_btn.setIconSize(QSize(16, 16))
        self.fav_btn.setFixedSize(32, 32)
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.setStyleSheet(icon_btn_style)
        self.fav_btn.clicked.connect(self._toggle_favorite)

        left_layout.addWidget(self.thumb)
        left_layout.addLayout(info, 1)
        left_layout.addWidget(self.fav_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        # Click-to-open the now-playing page, but skip the press if it
        # lands in the bottom-left corner — the host window uses that
        # corner for diagonal resize and the cluster used to swallow
        # those clicks. Match the host's CORNER_MARGIN (16px) so the
        # exclusion zone lines up with the resize hit zone.
        _CORNER_RESIZE_BOX = 16

        def _on_left_press(e):
            if e.button() != Qt.MouseButton.LeftButton:
                e.ignore()
                return
            x = e.position().x()
            y = e.position().y()
            if x <= _CORNER_RESIZE_BOX and y >= left.height() - _CORNER_RESIZE_BOX:
                # Let the press bubble to the host window's resize-edge
                # detector by leaving the event unaccepted.
                e.ignore()
                return
            self.show_now_playing_requested.emit()
        left.mousePressEvent = _on_left_press
        # Exposed so the host can blank it while the now-playing page is
        # showing. The cluster's `setFixedWidth(380)` slot stays in the
        # layout regardless of child visibility — that's what keeps the
        # center column visually centered when the cluster is "hidden."
        # Calling `left.hide()` instead would collapse the slot and shift
        # the transport controls left.
        self.left_cluster = left
        layout.addWidget(left)

        # ── Center column: transport above progress, both centered ──────────
        # Stretches above and below the two rows make the cluster sit
        # vertically in the bar (not glued to the top). Spacing between
        # the rows is tight (6px) so they read as one control surface.
        center = QVBoxLayout()
        center.setContentsMargins(0, 6, 0, 6)
        center.setSpacing(6)
        center.addStretch(1)

        self.shuffle_btn = _icon_btn("shuffle", "Shuffle")
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.toggled.connect(self._on_shuffle_toggled)

        self.prev_btn = _icon_btn("prev", "Previous (Ctrl+Left)")
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())

        # Play is the primary control — slightly larger than the others
        # so the eye lands on it first.
        self.play_btn = _icon_btn("play", "Play / Pause (Space)", size=40, icon_size=20)
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())

        self.next_btn = _icon_btn("next", "Next (Ctrl+Right)")
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())

        self.repeat_btn = _icon_btn("repeat", "Repeat")
        self.repeat_btn.setCheckable(True)
        self._repeat_state = "off"
        self.repeat_btn.clicked.connect(self._cycle_repeat)

        trans_row = QHBoxLayout()
        trans_row.setSpacing(4)
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_row.addStretch()
        for btn in (self.shuffle_btn, self.prev_btn, self.play_btn,
                    self.next_btn, self.repeat_btn):
            trans_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        trans_row.addStretch()

        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(
            f"color: {TEXT_FAINT}; font-size: 11px; min-width: 38px;"
            "font-variant-numeric: tabular-nums;"
        )
        self.cur_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.seek_bar = QSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setRange(0, 1000)
        self.seek_bar.setStyleSheet(slider_style)
        self.seek_bar.sliderPressed.connect(lambda: setattr(self, "_is_seeking", True))
        self.seek_bar.sliderReleased.connect(self._on_seek_release)

        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(
            f"color: {TEXT_FAINT}; font-size: 11px; min-width: 38px;"
            "font-variant-numeric: tabular-nums;"
        )
        self.tot_time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        prog_row = QHBoxLayout()
        prog_row.setContentsMargins(40, 0, 40, 0)
        prog_row.setSpacing(10)
        prog_row.addWidget(self.cur_time)
        prog_row.addWidget(self.seek_bar, 1)
        prog_row.addWidget(self.tot_time)

        center.addLayout(trans_row)
        center.addLayout(prog_row)
        center.addStretch(1)
        layout.addLayout(center, 1)

        # ── Right column: queue + volume ────────────────────────────────────
        # Cast button removed — it lives in the top bar now (avoids
        # duplicate controls and frees space here for the volume slider).
        # Pop out the floating mini player. Glyph is the universal
        # picture-in-picture mark (rect with a filled inset) — the old
        # "queue" icon read as a playlist toggle, which this button
        # never was.
        self.queue_btn = _icon_btn("miniplayer", "Open mini player")
        self.queue_btn.setCheckable(True)
        self.queue_btn.clicked.connect(lambda: self.show_queue_requested.emit())

        self.vol_btn = _icon_btn("volume", "Mute / Unmute")
        self.vol_btn.clicked.connect(lambda: self.bus.mute_toggled.emit())

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setStyleSheet(slider_style)
        self.vol_slider.valueChanged.connect(lambda v: self.bus.volume_changed.emit(v))

        right = QWidget()
        # Match the left cluster's 380px so the center column lands on
        # the bar's true horizontal centerline. Asymmetric side columns
        # were pushing the transport buttons ~80px right of center even
        # though they were AlignCenter inside the stretch column.
        right.setFixedWidth(380)
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(6)
        right_row.addStretch()
        right_row.addWidget(self.queue_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.vol_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.vol_slider, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(right)

        # Initial volume from settings
        from modules.settings import get_settings
        self.vol_slider.setValue(get_settings().volume)

        # ── Connect bus ─────────────────────────────────────────────────────
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(lambda: self.play_btn.setIcon(icon("play")))
        self.bus.playback_resumed.connect(lambda: self.play_btn.setIcon(icon("pause")))
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)
        self.bus.volume_state.connect(self.vol_slider.setValue)
        self.bus.mute_state.connect(
            lambda m: self.vol_btn.setIcon(icon("volume_muted" if m else "volume"))
        )
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        self.title.setText(np.title)
        self.sub.setText(np.subtitle or np.year)
        self.play_btn.setIcon(icon("pause"))
        self._set_favorite(np.is_favorite)

        if np.thumb_url:
            # Higher-res load — the cover is now 96×96 and we re-clip the
            # right corners ourselves, so we want a sharp source pixmap.
            load_image_async(f"{np.item_id}|npbar", np.thumb_url, 400, 400,
                              self.set_cover_pixmap, rounded_radius=0)

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
        # KeepAspectRatioByExpanding may return a pixmap larger than `s`
        # for non-square source art. We MUST center-crop to `s` before
        # rounding, otherwise _round_corners bakes the curves at the
        # oversized pixmap's edges and the QLabel's clip rect (which is
        # `s`) hides them — the user sees square corners instead.
        if scaled.size() != s:
            cx = max(0, (scaled.width() - s.width()) // 2)
            cy = max(0, (scaled.height() - s.height()) // 2)
            scaled = scaled.copy(cx, cy, s.width(), s.height())
        # bl=14 seats into the window body's rounded bottom-left corner;
        # the other three corners use the standard card radius (10) so the
        # whole cover reads as a tile rather than a half-rounded slab.
        scaled = _round_corners(scaled, tl=10, tr=10, br=10, bl=14)
        self.thumb.setPixmap(scaled)

    @Slot()
    def _on_stopped(self):
        self.title.setText("Nothing playing")
        self.sub.setText("")
        self._cover_orig = None
        self.thumb.setPixmap(QPixmap())
        self.play_btn.setIcon(icon("play"))
        self._set_favorite(False)
        self.seek_bar.setValue(0)
        self.cur_time.setText("0:00")
        self.tot_time.setText("0:00")

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
        """Hide the cover/title/artist/favorite cluster's contents while
        keeping the cluster widget itself in the layout. The parent has
        a fixed 380px width, so its slot stays reserved regardless of
        child visibility — that keeps the center transport column
        visually centered in the bar even when the cluster is hidden
        (e.g. while the now-playing page is showing the same info on
        its own left pane)."""
        self.thumb.setVisible(visible)
        self.title.setVisible(visible)
        self.sub.setVisible(visible)
        self.fav_btn.setVisible(visible)
        # Block click-through too — without this the empty area still
        # accepts clicks and re-fires show_now_playing_requested.
        self.left_cluster.setEnabled(visible)

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


# ── Cast dialog ──────────────────────────────────────────────────────────────

class CastDialog(QDialog):
    """Frameless frosted dialog matching the settings + main window. Auto-
    scans on open; devices appear live as discovery callbacks fire. The
    Rescan button is kept as a manual escape hatch but the user shouldn't
    need it for the common path."""

    BODY_RADIUS = 14

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

        from modules.ui_helpers import GLOBAL_STYLE, DIALOG_BODY_COLOR, enable_kde_blur
        self._dialog_body_color = DIALOG_BODY_COLOR
        self._enable_kde_blur = enable_kde_blur
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
        self.list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline: none;
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
        cast_btn_css = f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {ACCENT};
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QPushButton:pressed {{ background: rgba(255, 255, 255, 0.16); }}
            QPushButton:disabled {{ color: rgba(255, 255, 255, 0.30); }}
        """

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.scan_btn = QPushButton("Rescan")
        self.scan_btn.setStyleSheet(action_btn_css)
        self.scan_btn.clicked.connect(self.scan)
        btns.addWidget(self.scan_btn)
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
        self._render_devices(self.cast_manager.get_all_devices())
        self._refresh_active_banner()
        self.scan()

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
                border: none; font-size: 12px;
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
        self.cast_manager.discover_all()

    def _render_devices(self, devices: List[CastDevice]):
        # Preserve selection across re-renders so a freshly-arriving
        # device doesn't deselect what the user just clicked.
        prev_uuid = (
            self.selected_device.uuid if self.selected_device else None
        )
        self.list.clear()
        if not devices:
            return
        self._scanning_label.hide()
        self.list.show()
        for dev in devices:
            kind = "Chromecast" if dev.device_type == "chromecast" else "AirPlay"
            # Single-line label keeps each row to one font height instead
            # of two — fits more devices in the same dialog.
            item = QListWidgetItem(f"{dev.name}   ·   {kind}")
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self.list.addItem(item)
            if prev_uuid and dev.uuid == prev_uuid:
                self.list.setCurrentItem(item)
        # Banner state can change as devices come and go (active_cast
        # may have just been discovered with full metadata).
        self._refresh_active_banner()

    # ── Active-cast banner ─────────────────────────────────────────────
    def _build_active_banner(self) -> QWidget:
        w = QFrame()
        w.setObjectName("castActiveBanner")
        w.setStyleSheet(f"""
            QFrame#castActiveBanner {{
                background: rgba(0,164,220,0.14);
                border: 1px solid rgba(0,164,220,0.25);
                border-radius: 8px;
            }}
        """)
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
        if sel:
            dev = sel[0].data(Qt.ItemDataRole.UserRole)
            if dev:
                self.selected_device = dev
                self.cast_btn.setEnabled(True)

    def paintEvent(self, e):
        # Frosted rounded body, matching the settings dialog. The
        # custom titlebar is part of the same surface, so the rounded
        # rect spans the full window.
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

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(50, lambda: self._enable_kde_blur(self))
