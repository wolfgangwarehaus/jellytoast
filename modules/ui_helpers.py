"""
Shared UI helpers: theme, async image loader, formatting, common widgets.
"""

import threading
import requests
from typing import Callable, Optional
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QImage, QColor, QPainter, QPainterPath, QFont
from PyQt6.QtWidgets import QLabel


# ── Theme ────────────────────────────────────────────────────────────────────

ACCENT = "#a78bfa"
ACCENT_DEEP = "#7c3aed"
BG = "#0a0a14"
BG_PANEL = "#12121f"
BG_CARD = "rgba(255,255,255,0.04)"
TEXT = "#e2e8f0"
TEXT_DIM = "rgba(226,232,240,0.55)"
TEXT_FAINT = "rgba(226,232,240,0.32)"
BORDER = "rgba(255,255,255,0.06)"
BORDER_ACCENT = "rgba(167,139,250,0.35)"

GLOBAL_STYLE = f"""
* {{
    color: {TEXT};
    font-family: 'Inter', 'Segoe UI', 'Noto Sans', sans-serif;
}}
QMainWindow, QDialog, QWidget {{
    background: {BG};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: rgba(255,255,255,0.03); width: 8px; border-radius: 4px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(167,139,250,0.4); border-radius: 4px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 8px; background: transparent; }}
QScrollBar::handle:horizontal {{
    background: rgba(167,139,250,0.4); border-radius: 4px; min-width: 24px;
}}
QLineEdit {{
    background: rgba(255,255,255,0.05);
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    color: {TEXT};
    selection-background-color: {ACCENT_DEEP};
}}
QLineEdit:focus {{ border-color: {ACCENT}; background: rgba(255,255,255,0.07); }}
QPushButton {{
    background: rgba(255,255,255,0.05);
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
}}
QPushButton:hover {{ background: rgba(167,139,250,0.15); border-color: {BORDER_ACCENT}; }}
QPushButton:pressed {{ background: rgba(167,139,250,0.3); }}
QPushButton#accent {{
    background: {ACCENT_DEEP}; border: 1px solid {ACCENT}; color: white;
}}
QPushButton#accent:hover {{ background: {ACCENT}; }}
QPushButton#ghost {{
    background: transparent; border: none;
}}
QPushButton#ghost:hover {{ background: rgba(255,255,255,0.06); }}
QComboBox {{
    background: rgba(255,255,255,0.05);
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 12px;
    min-height: 22px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_ACCENT};
    border-radius: 6px;
    selection-background-color: rgba(167,139,250,0.25);
    padding: 4px;
}}
QListWidget {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px; border-radius: 6px; margin: 1px 2px;
}}
QListWidget::item:selected {{ background: rgba(167,139,250,0.18); }}
QListWidget::item:hover {{ background: rgba(255,255,255,0.04); }}
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {ACCENT}; border-bottom: 2px solid {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 3px; background: rgba(255,255,255,0.12); border-radius: 1px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 1px; }}
QSlider::handle:horizontal {{
    width: 12px; height: 12px; margin: -5px 0;
    background: white; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
QMenu {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_ACCENT};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 24px 8px 14px; border-radius: 4px;
}}
QMenu::item:selected {{ background: rgba(167,139,250,0.2); }}
QMenu::separator {{
    height: 1px; background: {BORDER}; margin: 4px 8px;
}}
QToolTip {{
    background: {BG_PANEL}; color: {TEXT};
    border: 1px solid {BORDER_ACCENT}; padding: 4px 8px; border-radius: 4px;
}}
"""


# ── Async image loader ──────────────────────────────────────────────────────

class _ImageLoaderSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)


_image_cache: dict[str, QPixmap] = {}


def load_image_async(key: str, url: str, target_w: int, target_h: int,
                     callback: Callable[[QPixmap], None],
                     rounded_radius: int = 0):
    """
    Fetch image off-thread, scale, optionally round corners, and invoke callback
    on the Qt main thread.
    """
    cache_key = f"{key}|{target_w}x{target_h}|r={rounded_radius}"
    if cache_key in _image_cache:
        callback(_image_cache[cache_key])
        return

    signals = _ImageLoaderSignals()
    signals.loaded.connect(lambda _k, p: callback(p))

    def _work():
        pix = _fetch_pixmap(url, target_w, target_h)
        if rounded_radius > 0:
            pix = _round_corners(pix, rounded_radius)
        _image_cache[cache_key] = pix
        signals.loaded.emit(cache_key, pix)

    threading.Thread(target=_work, daemon=True).start()


def _fetch_pixmap(url: str, w: int, h: int) -> QPixmap:
    try:
        r = requests.get(url, timeout=8)
        img = QImage()
        img.loadFromData(r.content)
        if img.isNull():
            raise ValueError("invalid image")
        return QPixmap.fromImage(img).scaled(
            w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        pix = QPixmap(w, h)
        pix.fill(QColor("#1a1a2e"))
        # Subtle gradient placeholder
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor(255, 255, 255, 30))
        p.setFont(QFont("Arial", min(w, h) // 4, QFont.Weight.Bold))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
        p.end()
        return pix


def _round_corners(pix: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pix.width(), pix.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


# ── Formatting ──────────────────────────────────────────────────────────────

def fmt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_duration_ticks(ticks: int) -> str:
    return fmt_time(ticks // 10_000)


def make_app_icon(size: int = 64) -> QPixmap:
    """Generate a simple JellyPlayer logo."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(ACCENT_DEEP))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, size // 5, size // 5)
    p.setPen(QColor("white"))
    p.setFont(QFont("Arial", int(size * 0.45), QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "J")
    p.end()
    return pix
