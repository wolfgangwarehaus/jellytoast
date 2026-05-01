"""
Bottom Now Playing bar + Cast device picker dialog.
"""

import threading
from typing import List
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QSize
from PyQt6.QtGui import QColor, QPixmap, QFont, QPainter, QPainterPath
from PyQt6.QtWidgets import (
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
from modules.ui_helpers import (
    load_image_async, fmt_time, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM,
    TEXT_FAINT, BORDER, BG_PANEL,
)


class NowPlayingBar(QWidget):
    """Persistent transport at the bottom of the main window."""

    show_now_playing_requested = pyqtSignal()
    show_queue_requested = pyqtSignal()
    cast_requested = pyqtSignal()

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
            b.setCursor(Qt.CursorShape.PointingHandCursor)
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
        left.setCursor(Qt.CursorShape.PointingHandCursor)
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
            f"color: {TEXT}; font-size: 14px; font-weight: 600; "
            "letter-spacing: 0.1px;"
        )
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
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
        self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.setStyleSheet(icon_btn_style)
        self.fav_btn.clicked.connect(self._toggle_favorite)

        left_layout.addWidget(self.thumb)
        left_layout.addLayout(info, 1)
        left_layout.addWidget(self.fav_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        left.mousePressEvent = lambda e: self.show_now_playing_requested.emit()
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
        self.queue_btn = _icon_btn("queue", "Show queue")
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
        right.setFixedWidth(220)
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

    @pyqtSlot(object)
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

    @pyqtSlot()
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

    @pyqtSlot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        if not self._is_seeking and np.duration > 0:
            self.seek_bar.setValue(int(ms / np.duration * 1000))
        self.cur_time.setText(fmt_time(ms))

    @pyqtSlot(int)
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

    def _toggle_favorite(self):
        np = get_now_playing()
        if not np.item_id:
            return
        new_state = not np.is_favorite
        threading.Thread(
            target=lambda: self.api.toggle_favorite(np.item_id, new_state),
            daemon=True,
        ).start()
        np.is_favorite = new_state
        self.bus.favorite_toggled.emit(np.item_id, new_state)

    def _set_favorite(self, fav: bool):
        # Filled accent-colored heart when favorited; outline otherwise.
        self.fav_btn.setIcon(accent_icon("favorite_filled") if fav else icon("favorite_outline"))

    @pyqtSlot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id == item_id:
            self._set_favorite(fav)


# ── Cast dialog ──────────────────────────────────────────────────────────────

class CastDialog(QDialog):
    def __init__(self, cast_manager: CastManager, parent=None):
        super().__init__(parent)
        self.cast_manager = cast_manager
        self.selected_device: CastDevice = None
        self.setWindowTitle("Cast")
        self.setFixedSize(400, 380)

        from modules.ui_helpers import GLOBAL_STYLE
        self.setStyleSheet(GLOBAL_STYLE + f"""
            QDialog {{ background: {BG_PANEL}; }}
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(14)

        title = QLabel("📡  Cast to device")
        title.setStyleSheet(f"color: {TEXT}; font-size: 18px; font-weight: 800;")
        v.addWidget(title)

        sub = QLabel("Select a Chromecast or AirPlay receiver on your network.")
        sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        sub.setWordWrap(True)
        v.addWidget(sub)

        self.list = QListWidget()
        v.addWidget(self.list, 1)

        btns = QHBoxLayout()
        self.scan_btn = QPushButton("🔄  Rescan")
        self.scan_btn.clicked.connect(self.scan)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        self.cast_btn = QPushButton("Cast ▶")
        self.cast_btn.setObjectName("accent")
        self.cast_btn.setEnabled(False)
        self.cast_btn.clicked.connect(self.accept)

        btns.addWidget(self.scan_btn)
        btns.addStretch()
        btns.addWidget(cancel)
        btns.addWidget(self.cast_btn)
        v.addLayout(btns)

        self.list.itemSelectionChanged.connect(self._on_select)
        self._refresh()

    def _refresh(self):
        self.list.clear()
        devices = self.cast_manager.get_all_devices()
        if not devices:
            empty = QListWidgetItem("No devices found yet — click Rescan")
            empty.setForeground(QColor(TEXT_FAINT))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
            return
        for dev in devices:
            icon = "📺" if dev.device_type == "chromecast" else "📡"
            label = f"{icon}   {dev.name}"
            sub = "Chromecast" if dev.device_type == "chromecast" else "AirPlay"
            item = QListWidgetItem(f"{label}\n      {sub}")
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self.list.addItem(item)

    def scan(self):
        self.list.clear()
        scanning = QListWidgetItem("⏳ Scanning…")
        scanning.setForeground(QColor(ACCENT))
        scanning.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list.addItem(scanning)
        self.cast_manager.discover_all()
        QTimer.singleShot(6000, self._refresh)

    def _on_select(self):
        sel = self.list.selectedItems()
        if sel:
            dev = sel[0].data(Qt.ItemDataRole.UserRole)
            if dev:
                self.selected_device = dev
                self.cast_btn.setEnabled(True)
