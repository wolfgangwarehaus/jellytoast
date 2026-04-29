"""
Floating mini player.
Two modes:
  - Compact: 360×96 — artwork + title + transport (great for audio)
  - Expanded: 320×420 — large artwork + full controls + queue peek

The mini player is frameless, always-on-top, and draggable.
"""

from PyQt6.QtCore import Qt, QPoint, QSize, QTimer, pyqtSlot
from PyQt6.QtGui import QPixmap, QFont, QColor, QPainter, QPainterPath, QCursor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
    QApplication, QFrame, QStackedWidget, QSizePolicy,
)

from modules.player_state import PlayerBus, get_now_playing, NowPlaying
from modules.ui_helpers import (
    load_image_async, fmt_time, ACCENT, ACCENT_DEEP, TEXT, TEXT_DIM, TEXT_FAINT,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _icon_button(symbol: str, size: int = 30, accent: bool = False) -> QPushButton:
    btn = QPushButton(symbol)
    btn.setFixedSize(size, size)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if accent:
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT}; color: white; border: none;
                border-radius: {size//2}px; font-size: {size//2}px; padding-left: 1px;
            }}
            QPushButton:hover {{ background: white; color: {ACCENT_DEEP}; }}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT}; border: none;
                border-radius: {size//2}px; font-size: {int(size*0.55)}px;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,0.1); }}
        """)
    return btn


# ── Compact mode ─────────────────────────────────────────────────────────────

class _CompactBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Artwork
        self.thumb = QLabel()
        self.thumb.setFixedSize(72, 72)
        self.thumb.setStyleSheet("background: rgba(255,255,255,0.05); border-radius: 8px;")
        layout.addWidget(self.thumb)

        # Center: title + sub + progress
        center = QVBoxLayout()
        center.setSpacing(2)
        center.setContentsMargins(0, 4, 0, 4)

        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 12px; font-weight: 600;")
        self.title.setMaximumWidth(180)

        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {ACCENT}; font-size: 11px;")
        self.sub.setMaximumWidth(180)

        # Progress
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 3px; background: rgba(255,255,255,0.15); border-radius: 1px;
            }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 1px; }}
            QSlider::handle:horizontal {{
                width: 8px; height: 8px; margin: -3px 0;
                background: white; border-radius: 4px;
            }}
        """)
        self.progress.sliderMoved.connect(self._on_seek)

        # Time row
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 9px;")
        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 9px;")
        time_row.addWidget(self.cur_time)
        time_row.addStretch()
        time_row.addWidget(self.tot_time)

        center.addWidget(self.title)
        center.addWidget(self.sub)
        center.addWidget(self.progress)
        center.addLayout(time_row)
        layout.addLayout(center, 1)

        # Right: prev / play / next
        controls = QHBoxLayout()
        controls.setSpacing(2)
        self.prev_btn = _icon_button("⏮", 28)
        self.play_btn = _icon_button("▶", 36, accent=True)
        self.next_btn = _icon_button("⏭", 28)
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_btn)
        layout.addLayout(controls)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))


# ── Expanded mode ────────────────────────────────────────────────────────────

class _ExpandedPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        # Big artwork
        self.cover = QLabel()
        self.cover.setFixedSize(280, 280)
        self.cover.setStyleSheet("background: rgba(255,255,255,0.05); border-radius: 12px;")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignHCenter)

        # Title + subtitle
        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet(f"color: {TEXT}; font-size: 15px; font-weight: 700;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)

        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.title)
        layout.addWidget(self.sub)

        # Progress
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 3px; background: rgba(255,255,255,0.15); border-radius: 1px;
            }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 1px; }}
            QSlider::handle:horizontal {{
                width: 10px; height: 10px; margin: -4px 0;
                background: white; border-radius: 5px;
            }}
        """)
        self.progress.sliderMoved.connect(self._on_seek)
        layout.addWidget(self.progress)

        time_row = QHBoxLayout()
        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 10px;")
        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 10px;")
        time_row.addWidget(self.cur_time)
        time_row.addStretch()
        time_row.addWidget(self.tot_time)
        layout.addLayout(time_row)

        # Controls row
        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.shuffle_btn = _icon_button("⤮", 30)
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.toggled.connect(lambda v: self.bus.shuffle_changed.emit(v))

        self.prev_btn = _icon_button("⏮", 36)
        self.play_btn = _icon_button("▶", 48, accent=True)
        self.next_btn = _icon_button("⏭", 36)
        self.repeat_btn = _icon_button("↻", 30)
        self.repeat_btn.setCheckable(True)

        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())

        # Repeat cycles off → all → one
        self._repeat_state = "off"
        self.repeat_btn.clicked.connect(self._cycle_repeat)

        controls.addStretch()
        controls.addWidget(self.shuffle_btn)
        controls.addWidget(self.prev_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.repeat_btn)
        controls.addStretch()
        layout.addLayout(controls)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))

    def _cycle_repeat(self):
        order = ["off", "all", "one"]
        idx = order.index(self._repeat_state)
        self._repeat_state = order[(idx + 1) % 3]
        symbols = {"off": "↻", "all": "🔁", "one": "🔂"}
        self.repeat_btn.setText(symbols[self._repeat_state])
        self.repeat_btn.setChecked(self._repeat_state != "off")
        self.bus.repeat_changed.emit(self._repeat_state)


# ── The mini player itself ──────────────────────────────────────────────────

class FloatingMiniPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self._drag_pos: QPoint = QPoint()
        self._mode = "compact"  # "compact" or "expanded"

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Main container with shadow + rounded background
        self.container = QFrame(self)
        self.container.setStyleSheet(f"""
            QFrame {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(15,15,30,245), stop:1 rgba(28,20,50,245));
                border-radius: 14px;
                border: 1px solid rgba(167,139,250,0.35);
            }}
        """)
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 0, 0, 0)
        self.outer_layout.addWidget(self.container)

        self.inner_layout = QVBoxLayout(self.container)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner_layout.setSpacing(0)

        # Top toolbar (drag handle + close + expand/compact toggle)
        toolbar = QWidget()
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 6, 8, 0)
        tb.setSpacing(2)

        self.drag_handle = QLabel("• • •")
        self.drag_handle.setStyleSheet(f"color: {TEXT_FAINT}; font-size: 11px; padding: 2px 6px;")
        self.drag_handle.setCursor(Qt.CursorShape.SizeAllCursor)

        tb.addWidget(self.drag_handle)
        tb.addStretch()

        self.toggle_btn = QPushButton("▢")
        self.toggle_btn.setFixedSize(20, 20)
        self.toggle_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        self.toggle_btn.setToolTip("Toggle compact / expanded")
        self.toggle_btn.clicked.connect(self.toggle_mode)
        tb.addWidget(self.toggle_btn)

        self.open_btn = QPushButton("⛶")
        self.open_btn.setFixedSize(20, 20)
        self.open_btn.setStyleSheet(self.toggle_btn.styleSheet())
        self.open_btn.setToolTip("Open main window")
        self.open_btn.clicked.connect(lambda: self.bus.open_main_window.emit())
        tb.addWidget(self.open_btn)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; }}
            QPushButton:hover {{ color: #ef4444; }}
        """)
        self.close_btn.clicked.connect(self.hide)
        tb.addWidget(self.close_btn)

        self.inner_layout.addWidget(toolbar)

        # Stacked: compact / expanded
        self.stack = QStackedWidget()
        self.compact = _CompactBar()
        self.expanded = _ExpandedPanel()
        self.stack.addWidget(self.compact)
        self.stack.addWidget(self.expanded)
        self.inner_layout.addWidget(self.stack)

        self._apply_mode_size()
        self._connect_signals()

        # Position bottom-right
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    # ── Mode switching ──────────────────────────────────────────────────────

    def toggle_mode(self):
        self._mode = "expanded" if self._mode == "compact" else "compact"
        self._apply_mode_size()

    def _apply_mode_size(self):
        if self._mode == "compact":
            self.setFixedSize(380, 120)
            self.stack.setCurrentIndex(0)
            self.toggle_btn.setText("▢")
        else:
            self.setFixedSize(320, 480)
            self.stack.setCurrentIndex(1)
            self.toggle_btn.setText("▭")

    # ── Bus signals ─────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        self.bus.playback_paused.connect(self._on_paused)
        self.bus.playback_resumed.connect(self._on_resumed)
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)

    @pyqtSlot(object)
    def _on_started(self, np: NowPlaying):
        for panel in (self.compact, self.expanded):
            panel.title.setText(np.title)
            panel.sub.setText(np.subtitle or np.year)
            panel.play_btn.setText("⏸")

        if np.thumb_url:
            load_image_async(f"{np.item_id}|mini", np.thumb_url, 72, 72,
                              self.compact.thumb.setPixmap, rounded_radius=8)
            load_image_async(f"{np.item_id}|miniexp", np.thumb_url, 280, 280,
                              self.expanded.cover.setPixmap, rounded_radius=12)

    @pyqtSlot()
    def _on_stopped(self):
        for panel in (self.compact, self.expanded):
            panel.title.setText("Nothing playing")
            panel.sub.setText("")
            panel.play_btn.setText("▶")
            panel.progress.setValue(0)
            panel.cur_time.setText("0:00")
            panel.tot_time.setText("0:00")

    @pyqtSlot()
    def _on_paused(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setText("▶")

    @pyqtSlot()
    def _on_resumed(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setText("⏸")

    @pyqtSlot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        for panel in (self.compact, self.expanded):
            if np.duration > 0 and not panel.progress.isSliderDown():
                panel.progress.setValue(int(ms / np.duration * 1000))
            panel.cur_time.setText(fmt_time(ms))

    @pyqtSlot(int)
    def _on_duration(self, ms: int):
        for panel in (self.compact, self.expanded):
            panel.tot_time.setText(fmt_time(ms))

    # ── Drag support ────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and not self._drag_pos.isNull():
            self.move(e.globalPosition().toPoint() - self._drag_pos)
