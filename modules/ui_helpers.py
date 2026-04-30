"""
Shared UI helpers: theme, async image loader, formatting, common widgets.
"""

import math
import os
import shutil
import subprocess
import threading
import requests
from typing import Callable, Optional, Sequence
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QImage, QColor, QPainter, QPainterPath, QFont
from PyQt6.QtWidgets import QLabel, QWidget


# ── Theme ────────────────────────────────────────────────────────────────────

ACCENT = "#00a4dc"
ACCENT_DEEP = "#0085bd"
BG = "#101010"
BG_PANEL = "#202020"
BG_CARD = "rgba(255,255,255,0.04)"
TEXT = "#ffffff"
TEXT_DIM = "rgba(255,255,255,0.7)"
TEXT_FAINT = "rgba(255,255,255,0.4)"
BORDER = "rgba(255,255,255,0.08)"
BORDER_ACCENT = "rgba(0,164,220,0.35)"

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


# ── KDE Plasma frosted-glass blur ───────────────────────────────────────────

_XPROP_OK: Optional[bool] = None


RegionLike = "tuple[int, int, int, int] | Sequence[tuple[int, int, int, int]]"


def rounded_rect_region(x: int, y: int, w: int, h: int, r: int
                        ) -> list[tuple[int, int, int, int]]:
    """
    Approximate a rounded rectangle as a list of axis-aligned rects, suitable
    for `_KDE_NET_WM_BLUR_BEHIND_REGION`. Walks the corner quadrant 1px at a
    time and merges consecutive rows that share the same inset.
    """
    if r <= 0 or w <= 2 * r or h <= 2 * r:
        return [(x, y, w, h)]

    insets: list[int] = []
    for i in range(r):
        d = r - i - 0.5  # distance from corner center to row midline
        inset = int(math.ceil(r - math.sqrt(max(r * r - d * d, 0.0))))
        insets.append(inset)

    rects: list[tuple[int, int, int, int]] = []

    # Top corners — merge consecutive same-inset rows
    run_start = 0
    for i in range(1, r + 1):
        if i == r or insets[i] != insets[run_start]:
            inset = insets[run_start]
            rw = w - 2 * inset
            if rw > 0:
                rects.append((x + inset, y + run_start, rw, i - run_start))
            run_start = i

    # Middle (full width, no inset)
    rects.append((x, y + r, w, h - 2 * r))

    # Bottom corners (mirror of top)
    bottom_y = y + h - r
    run_start = 0
    bottom_insets = list(reversed(insets))
    for i in range(1, r + 1):
        if i == r or bottom_insets[i] != bottom_insets[run_start]:
            inset = bottom_insets[run_start]
            rw = w - 2 * inset
            if rw > 0:
                rects.append((x + inset, bottom_y + run_start, rw, i - run_start))
            run_start = i

    return rects


def enable_kde_blur(widget: QWidget, region: Optional["RegionLike"] = None):
    """
    Ask KWin to blur whatever's behind the translucent areas of `widget`.

    Sets `_KDE_NET_WM_BLUR_BEHIND_REGION`. KWin requires the cardinal count
    to be a multiple of 4 (one rect per 4 values: x, y, w, h).

    Pass `region` as `(x, y, w, h)` for a single rect, or a list of those
    tuples to approximate a non-rectangular shape (e.g. rounded corners —
    see `rounded_rect_region`). Without this, a rounded translucent body
    shows the blur's square corners poking out past its rounded edge.

    Requires `xprop` (xorg-xprop, ships with every KDE install) and X11 or
    XWayland — native Wayland sessions ignore the property.
    """
    global _XPROP_OK
    if _XPROP_OK is False:
        return
    if _XPROP_OK is None:
        _XPROP_OK = shutil.which("xprop") is not None
        if not _XPROP_OK:
            return

    try:
        wid = int(widget.winId())
    except Exception:
        return
    if wid <= 0:
        return

    if region is None:
        rects = [(0, 0, max(widget.width(), 1), max(widget.height(), 1))]
    elif isinstance(region, tuple) and len(region) == 4 and all(isinstance(v, int) for v in region):
        rects = [region]
    else:
        rects = list(region)

    parts: list[str] = []
    for rx, ry, rw, rh in rects:
        if rw <= 0 or rh <= 0:
            continue
        parts.append(f"{int(rx)},{int(ry)},{int(rw)},{int(rh)}")
    if not parts:
        return
    region_str = ",".join(parts)

    def _run():
        try:
            subprocess.run(
                ["xprop", "-id", str(wid),
                 "-f", "_KDE_NET_WM_BLUR_BEHIND_REGION", "32c",
                 "-set", "_KDE_NET_WM_BLUR_BEHIND_REGION", region_str],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def skip_taskbar_x11(widget: QWidget):
    """
    Tell EWMH-aware window managers (KWin/Mutter/i3/etc.) to keep `widget` out
    of the taskbar and pager. Uses xprop to set _NET_WM_STATE atoms.
    Silently no-ops if xprop is missing or we're on native Wayland.
    """
    global _XPROP_OK
    if _XPROP_OK is False:
        return
    if _XPROP_OK is None:
        _XPROP_OK = shutil.which("xprop") is not None
        if not _XPROP_OK:
            return

    try:
        wid = int(widget.winId())
    except Exception:
        return
    if wid <= 0:
        return

    def _run():
        try:
            subprocess.run(
                ["xprop", "-id", str(wid),
                 "-f", "_NET_WM_STATE", "32a",
                 "-set", "_NET_WM_STATE",
                 "_NET_WM_STATE_SKIP_TASKBAR,_NET_WM_STATE_SKIP_PAGER,_NET_WM_STATE_ABOVE"],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


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
    """Generate a simple JellyToast logo."""
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
