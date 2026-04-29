"""
Bottom Now Playing bar + Cast device picker dialog.
"""

import threading
from typing import List
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QSize
from PyQt6.QtGui import QColor, QPixmap, QFont
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QSlider,
    QDialog, QListWidget, QListWidgetItem, QFrame, QSizePolicy,
)

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

        self.setFixedHeight(80)
        self.setStyleSheet(f"""
            QWidget#npbar {{
                background: rgba(12,12,24,0.97);
                border-top: 1px solid {BORDER};
            }}
        """)
        self.setObjectName("npbar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(16)

        # ── Left: thumbnail + title/artist (clickable to expand) ────────────
        left = QWidget()
        left.setFixedWidth(280)
        left.setCursor(Qt.CursorShape.PointingHandCursor)
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        self.thumb = QLabel()
        self.thumb.setFixedSize(56, 56)
        self.thumb.setStyleSheet("background: rgba(255,255,255,0.06); border-radius: 6px;")

        info = QVBoxLayout()
        info.setSpacing(2)
        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 13px; font-weight: 600;")
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        info.addWidget(self.title)
        info.addWidget(self.sub)

        # Favorite button
        self.fav_btn = QPushButton("♡")
        self.fav_btn.setFixedSize(28, 28)
        self.fav_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; font-size: 16px; }}
            QPushButton:hover {{ color: {ACCENT}; }}
            QPushButton[favorite="true"] {{ color: {ACCENT}; }}
        """)
        self.fav_btn.clicked.connect(self._toggle_favorite)

        left_layout.addWidget(self.thumb)
        left_layout.addLayout(info, 1)
        left_layout.addWidget(self.fav_btn)
        left.mousePressEvent = lambda e: self.show_now_playing_requested.emit()
        layout.addWidget(left)

        # ── Center: transport + progress ────────────────────────────────────
        center = QVBoxLayout()
        center.setSpacing(2)

        # Transport row
        trans_row = QHBoxLayout()
        trans_row.setSpacing(4)
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_style = f"""
            QPushButton {{
                background: transparent; color: {TEXT}; border: none;
                font-size: 16px; min-width: 32px; max-width: 32px;
                min-height: 32px; max-height: 32px; border-radius: 16px;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.08); }}
            QPushButton:checked {{ color: {ACCENT}; }}
        """

        self.shuffle_btn = QPushButton("⤮")
        self.shuffle_btn.setStyleSheet(btn_style)
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.setToolTip("Shuffle")
        self.shuffle_btn.toggled.connect(lambda v: self.bus.shuffle_changed.emit(v))

        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setStyleSheet(btn_style)
        self.prev_btn.setToolTip("Previous (Ctrl+Left)")
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())

        # Big play button
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setStyleSheet(f"""
            QPushButton {{
                background: white; color: black; border: none;
                border-radius: 20px; font-size: 14px; padding-left: 2px;
            }}
            QPushButton:hover {{ background: {ACCENT}; color: white; }}
        """)
        self.play_btn.setToolTip("Play / Pause (Space)")
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())

        self.next_btn = QPushButton("⏭")
        self.next_btn.setStyleSheet(btn_style)
        self.next_btn.setToolTip("Next (Ctrl+Right)")
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())

        self.repeat_btn = QPushButton("↻")
        self.repeat_btn.setStyleSheet(btn_style)
        self.repeat_btn.setCheckable(True)
        self.repeat_btn.setToolTip("Repeat")
        self._repeat_state = "off"
        self.repeat_btn.clicked.connect(self._cycle_repeat)

        trans_row.addStretch()
        trans_row.addWidget(self.shuffle_btn)
        trans_row.addWidget(self.prev_btn)
        trans_row.addWidget(self.play_btn)
        trans_row.addWidget(self.next_btn)
        trans_row.addWidget(self.repeat_btn)
        trans_row.addStretch()

        # Progress row
        prog_row = QHBoxLayout()
        prog_row.setContentsMargins(40, 0, 40, 0)
        prog_row.setSpacing(10)

        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 11px; min-width: 40px;")
        self.cur_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.seek_bar = QSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setRange(0, 1000)
        self.seek_bar.sliderPressed.connect(lambda: setattr(self, "_is_seeking", True))
        self.seek_bar.sliderReleased.connect(self._on_seek_release)

        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 11px; min-width: 40px;")

        prog_row.addWidget(self.cur_time)
        prog_row.addWidget(self.seek_bar, 1)
        prog_row.addWidget(self.tot_time)

        center.addLayout(trans_row)
        center.addLayout(prog_row)
        layout.addLayout(center, 1)

        # ── Right: volume, queue, cast ──────────────────────────────────────
        right_row = QHBoxLayout()
        right_row.setSpacing(8)

        self.queue_btn = QPushButton("☰")
        self.queue_btn.setStyleSheet(btn_style)
        self.queue_btn.setToolTip("Show queue")
        self.queue_btn.setCheckable(True)
        self.queue_btn.clicked.connect(lambda: self.show_queue_requested.emit())

        self.cast_btn = QPushButton("📡")
        self.cast_btn.setStyleSheet(btn_style)
        self.cast_btn.setToolTip("Cast to device")
        self.cast_btn.clicked.connect(lambda: self.cast_requested.emit())

        self.vol_btn = QPushButton("🔊")
        self.vol_btn.setStyleSheet(btn_style)
        self.vol_btn.clicked.connect(lambda: self.bus.mute_toggled.emit())

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.valueChanged.connect(lambda v: self.bus.volume_changed.emit(v))

        right_row.addWidget(self.queue_btn)
        right_row.addWidget(self.cast_btn)
        right_row.addWidget(self.vol_btn)
        right_row.addWidget(self.vol_slider)
        layout.addLayout(right_row)

        # Initial volume from settings
        from modules.settings import get_settings
        self.vol_slider.setValue(get_settings().volume)

        # ── Connect bus ─────────────────────────────────────────────────────
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(lambda: self.play_btn.setText("▶"))
        self.bus.playback_resumed.connect(lambda: self.play_btn.setText("⏸"))
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)
        self.bus.volume_state.connect(self.vol_slider.setValue)
        self.bus.mute_state.connect(lambda m: self.vol_btn.setText("🔇" if m else "🔊"))
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)

    @pyqtSlot(object)
    def _on_started(self, np: NowPlaying):
        self.title.setText(np.title)
        self.sub.setText(np.subtitle or np.year)
        self.play_btn.setText("⏸")
        self.fav_btn.setText("♥" if np.is_favorite else "♡")
        self.fav_btn.setProperty("favorite", "true" if np.is_favorite else "false")
        self.fav_btn.setStyle(self.fav_btn.style())  # refresh

        if np.thumb_url:
            load_image_async(f"{np.item_id}|npbar", np.thumb_url, 56, 56,
                              self.thumb.setPixmap, rounded_radius=6)

    @pyqtSlot()
    def _on_stopped(self):
        self.title.setText("Nothing playing")
        self.sub.setText("")
        self.thumb.setPixmap(QPixmap())
        self.play_btn.setText("▶")
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
        self.repeat_btn.setText({"off": "↻", "all": "🔁", "one": "🔂"}[self._repeat_state])
        self.repeat_btn.setChecked(self._repeat_state != "off")
        self.bus.repeat_changed.emit(self._repeat_state)

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

    @pyqtSlot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id == item_id:
            self.fav_btn.setText("♥" if fav else "♡")


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
